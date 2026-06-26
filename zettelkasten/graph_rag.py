# graphrag — поиск и генерация ответов по графу знаний
# изоляция по user_id: каждый пользователь ищет только в своём графе

import os
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from storage.neo4j.client import get_neo4j_client, Neo4jClient
from storage.neo4j.repository import ZettelRepository, ZettelNode, EntityNode
from zettelkasten.linker import LocalEmbeddingModel

load_dotenv()


# структуры данных контекста и ответа

@dataclass
class RetrievedContext:
    """Контекст, извлечённый из графа для RAG."""
    entry_points: List[ZettelNode] = field(default_factory=list)
    expanded_nodes: List[ZettelNode] = field(default_factory=list)
    entities: List[EntityNode] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    
    @property
    def all_nodes(self) -> List[ZettelNode]:
        """Все уникальные узлы."""
        seen = set()
        result = []
        for node in self.entry_points + self.expanded_nodes:
            if node.zettel_id not in seen:
                seen.add(node.zettel_id)
                result.append(node)
        return result
    
    def to_context_string(self) -> str:
        """Форматирует контекст для промпта LLM."""
        lines = []
        
        by_type: Dict[str, List[ZettelNode]] = {}
        for node in self.all_nodes:
            by_type.setdefault(node.thought_type, []).append(node)
        
        for thought_type, nodes in sorted(by_type.items()):
            lines.append(f"\n## {thought_type.upper()}")
            for node in nodes:
                lines.append(f"• [{node.luhmann_id}] {node.content}")
        
        if self.entities:
            lines.append("\n## СВЯЗАННЫЕ СУЩНОСТИ")
            for ent in self.entities[:10]:
                lines.append(f"• {ent.display_name} ({ent.entity_type}, упоминаний: {ent.mention_count})")
        
        if self.paths:
            lines.append("\n## СВЯЗИ МЕЖДУ МЫСЛЯМИ")
            for path in self.paths[:5]:
                lines.append(f"• {path}")
        
        return "\n".join(lines)


@dataclass
class RAGResponse:
    """Ответ GraphRAG системы."""
    answer: str
    context: RetrievedContext
    query: str
    user_id: str = ""
    processing_time_ms: int = 0


# извлечение контекста из графа (retrieval)

class GraphRetriever:
    """
    Retriever для graphrag.
    Все запросы к neo4j фильтруются по user_id.
    """
    
    def __init__(
        self,
        embedding_model: LocalEmbeddingModel = None,
        repository: ZettelRepository = None,
        search_limit: int = settings.graphrag_search_limit,
        context_hops: int = settings.graphrag_context_hops,
    ):
        self._embedding_model = embedding_model
        self._repository = repository
        self.search_limit = search_limit
        self.context_hops = context_hops
    
    @property
    def embedding_model(self) -> LocalEmbeddingModel:
        if self._embedding_model is None:
            self._embedding_model = LocalEmbeddingModel()
        return self._embedding_model
    
    @property
    def repository(self) -> ZettelRepository:
        if self._repository is None:
            client = get_neo4j_client()
            self._repository = ZettelRepository(client)
        return self._repository
    
    def retrieve(self, user_id: str, query: str, similarity_threshold: float = 0.3) -> RetrievedContext:
        """Выполняет graphrag retrieval для конкретного пользователя."""
        context = RetrievedContext()
        
        # шаг 1: векторный поиск точек входа в граф
        query_embedding = self.embedding_model.embed_query(query)
        candidates = self.repository.vector_search(
            user_id=user_id,
            query_embedding=query_embedding,
            limit=self.search_limit,
            similarity_threshold=similarity_threshold,
        )
        
        context.entry_points = [node for node, _ in candidates]
        
        if not context.entry_points:
            return context
        
        expanded_ids = set(node.zettel_id for node in context.entry_points)
        
        # шаг 2: расширяем контекст — родители, дети, related, сущности
        for entry in context.entry_points:
            node_context = self.repository.get_context(user_id, entry.zettel_id, hops=self.context_hops)
            
            if not node_context:
                continue
            
            if node_context.parent and node_context.parent.zettel_id not in expanded_ids:
                context.expanded_nodes.append(node_context.parent)
                expanded_ids.add(node_context.parent.zettel_id)
                context.paths.append(f"[{entry.luhmann_id}] → CHILD_OF → [{node_context.parent.luhmann_id}]")
            
            for child in node_context.children:
                if child.zettel_id not in expanded_ids:
                    context.expanded_nodes.append(child)
                    expanded_ids.add(child.zettel_id)
                    context.paths.append(f"[{child.luhmann_id}] → CHILD_OF → [{entry.luhmann_id}]")
            
            for related in node_context.related:
                if related.zettel_id not in expanded_ids:
                    context.expanded_nodes.append(related)
                    expanded_ids.add(related.zettel_id)
                    context.paths.append(f"[{entry.luhmann_id}] ↔ RELATED_TO ↔ [{related.luhmann_id}]")
            
            for entity in node_context.entities:
                if not any(e.name == entity.name for e in context.entities):
                    context.entities.append(entity)
        
        return context
    
    def retrieve_by_entity(self, user_id: str, entity_name: str, limit: int = 20) -> RetrievedContext:
        """Поиск всех мыслей пользователя, связанных с конкретной сущностью."""
        context = RetrievedContext()
        
        query = """
        MATCH (e:Entity {name: $entity_name, user_id: $user_id})<-[:MENTIONS]-(z:Zettel {user_id: $user_id})
        RETURN z, e
        ORDER BY z.created_at DESC
        LIMIT $limit
        """
        
        result = self.repository.client.execute_read(query, {
            "entity_name": entity_name.lower().replace(" ", "_"),
            "user_id": user_id,
            "limit": limit,
        })
        
        if not result:
            return context
        
        for row in result:
            node = self.repository._node_to_zettel(row["z"])
            context.entry_points.append(node)
            
            e = row["e"]
            if not any(ent.name == e["name"] for ent in context.entities):
                context.entities.append(EntityNode(
                    name=e["name"],
                    display_name=e.get("display_name", e["name"]),
                    entity_type=e.get("entity_type", "tag"),
                    user_id=e.get("user_id", ""),
                    mention_count=e.get("mention_count", 0),
                ))
        
        return context
    
    def retrieve_by_type(self, user_id: str, thought_type: str, limit: int = 20) -> RetrievedContext:
        """Поиск мыслей пользователя определённого типа."""
        context = RetrievedContext()
        
        query = """
        MATCH (z:Zettel {thought_type: $thought_type, user_id: $user_id})
        RETURN z
        ORDER BY z.created_at DESC
        LIMIT $limit
        """
        
        result = self.repository.client.execute_read(query, {
            "thought_type": thought_type,
            "user_id": user_id,
            "limit": limit,
        })
        
        for row in result:
            node = self.repository._node_to_zettel(row["z"])
            context.entry_points.append(node)
        
        return context


# генерация ответа llm по собранному контексту

class RAGGenerator:
    """Генератор ответов на основе извлечённого контекста."""
    
    SYSTEM_PROMPT = """Ты — интеллектуальный ассистент топ-менеджера, часть системы Executive Exocortex.
Отвечай на вопрос пользователя обычным, естественным, понятным языком.

ПРАВИЛА:
1. Опирайся только на факты из переданного контекста.
2. Не выдумывай информацию, которой нет в контексте.
3. Если данных недостаточно — честно скажи об этом простым текстом.
4. Не делай формальных разделов вроде "Факты/Решения/Действия/Риски", если пользователь явно не просил.
5. Можно кратко цитировать или ссылаться на карточки [luhmann_id], когда это помогает.
6. Держи ответ дружелюбным и практичным."""

    NO_CONTEXT_RESPONSE = """К сожалению, в вашей базе знаний я не нашёл информации по этому вопросу.

Возможные причины:
• Эта тема ещё не обсуждалась в ваших заметках
• Вопрос сформулирован иначе, чем информация в базе

Попробуйте:
• Переформулировать вопрос
• Использовать ключевые слова из ваших заметок
• Указать конкретные имена, проекты или даты"""

    def __init__(self, model_name: str = settings.zettel_atomizer_model_name):
        self.llm = ChatOpenAI(
            model=model_name,
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            temperature=0.3,
        )
    
    def generate(self, query: str, context: RetrievedContext) -> str:
        """Генерирует ответ на основе контекста."""
        
        if not context.all_nodes:
            return self.NO_CONTEXT_RESPONSE
        
        context_str = context.to_context_string()
        
        user_prompt = f"""КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ:
{context_str}

ВОПРОС ПОЛЬЗОВАТЕЛЯ:
{query}

Ответь на вопрос, опираясь ТОЛЬКО на предоставленный контекст."""
        
        response = self.llm.invoke([
            SystemMessage(content=self.SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ])
        
        return response.content


# фасад graphrag: retrieval + generation

class GraphRAG:
    """
    Основной класс graphrag.
    Все запросы изолированы по user_id.
    """
    
    def __init__(
        self,
        embedding_model: LocalEmbeddingModel = None,
        repository: ZettelRepository = None,
    ):
        self.retriever = GraphRetriever(
            embedding_model=embedding_model,
            repository=repository,
        )
        self.generator = RAGGenerator()
    
    def query(self, user_id: str, user_query: str, similarity_threshold: float = 0.3) -> RAGResponse:
        """Отвечает на вопрос пользователя, используя только его граф знаний."""
        import time
        start = time.time()
        
        context = self.retriever.retrieve(user_id, user_query, similarity_threshold)
        answer = self.generator.generate(user_query, context)
        
        elapsed_ms = int((time.time() - start) * 1000)
        
        return RAGResponse(
            answer=answer,
            context=context,
            query=user_query,
            user_id=user_id,
            processing_time_ms=elapsed_ms,
        )
    
    def query_entity(self, user_id: str, entity_name: str) -> RAGResponse:
        """Поиск всего, что связано с сущностью в графе пользователя."""
        import time
        start = time.time()
        
        context = self.retriever.retrieve_by_entity(user_id, entity_name)
        
        if not context.all_nodes:
            answer = f"В вашей базе знаний нет информации о сущности '{entity_name}'."
        else:
            answer = self.generator.generate(
                f"Расскажи всё, что известно о {entity_name}",
                context
            )
        
        elapsed_ms = int((time.time() - start) * 1000)
        
        return RAGResponse(
            answer=answer,
            context=context,
            query=f"entity:{entity_name}",
            user_id=user_id,
            processing_time_ms=elapsed_ms,
        )
    
    def query_actions(self, user_id: str) -> RAGResponse:
        """Возвращает все активные задачи пользователя."""
        import time
        start = time.time()
        
        context = self.retriever.retrieve_by_type(user_id, "action", limit=30)
        
        if not context.all_nodes:
            answer = "В вашей базе знаний пока нет зафиксированных задач."
        else:
            answer = self.generator.generate(
                "Перечисли все задачи и поручения, сгруппируй по исполнителям или проектам",
                context
            )
        
        elapsed_ms = int((time.time() - start) * 1000)
        
        return RAGResponse(
            answer=answer,
            context=context,
            query="type:action",
            user_id=user_id,
            processing_time_ms=elapsed_ms,
        )
    
    def query_risks(self, user_id: str) -> RAGResponse:
        """Возвращает все зафиксированные риски пользователя."""
        import time
        start = time.time()
        
        context = self.retriever.retrieve_by_type(user_id, "risk", limit=30)
        
        if not context.all_nodes:
            answer = "В вашей базе знаний пока нет зафиксированных рисков."
        else:
            answer = self.generator.generate(
                "Перечисли все риски, отсортируй по критичности",
                context
            )
        
        elapsed_ms = int((time.time() - start) * 1000)
        
        return RAGResponse(
            answer=answer,
            context=context,
            query="type:risk",
            user_id=user_id,
            processing_time_ms=elapsed_ms,
        )


# cli-демо для локального тестирования

if __name__ == "__main__":
    print("=" * 60)
    print("🧠 GraphRAG Demo (Multi-User)")
    print("=" * 60)
    
    rag = GraphRAG()
    
    # Demo user
    demo_user_id = "demo_user_123"
    
    print(f"\n📱 User ID: {demo_user_id}")
    print("Введите вопрос (или 'q' для выхода):")
    
    while True:
        try:
            query = input("\n> ").strip()
            
            if query.lower() in ("q", "quit", "exit"):
                break
            
            if not query:
                continue
            
            if query.startswith("/actions"):
                response = rag.query_actions(demo_user_id)
            elif query.startswith("/risks"):
                response = rag.query_risks(demo_user_id)
            elif query.startswith("/entity "):
                entity = query[8:].strip()
                response = rag.query_entity(demo_user_id, entity)
            else:
                response = rag.query(demo_user_id, query)
            
            print(f"\n📝 Ответ ({response.processing_time_ms}ms):\n")
            print(response.answer)
            
            if response.context.entry_points:
                print(f"\n📊 Найдено карточек: {len(response.context.all_nodes)}")
        
        except KeyboardInterrupt:
            break
    
    print("\n👋 До свидания!")
