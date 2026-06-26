# repository для работы с zettel-графом в neo4j
# изоляция по user_id: каждый пользователь видит только свой граф

import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field

from storage.neo4j.client import Neo4jClient


@dataclass
class ZettelNode:
    """Представление узла Zettel из графа."""
    zettel_id: str
    luhmann_id: str
    content: str
    thought_type: str
    tags: List[str]
    is_root_topic: bool
    user_id: str = ""
    embedding: Optional[List[float]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    similarity: Optional[float] = None


@dataclass
class EntityNode:
    """Представление узла Entity."""
    name: str
    display_name: str
    entity_type: str
    user_id: str = ""
    mention_count: int = 0


@dataclass
class GraphContext:
    """
    Контекст из графа для принятия решения линкером.
    Содержит кандидата + его окружение (соседи, сущности).
    """
    candidate: ZettelNode
    similarity: float
    parent: Optional[ZettelNode] = None
    children: List[ZettelNode] = field(default_factory=list)
    related: List[ZettelNode] = field(default_factory=list)
    entities: List[EntityNode] = field(default_factory=list)


class ZettelRepository:
    """
    Crud-операции с zettel-графом.
    Все запросы фильтруются по user_id.
    """
    
    def __init__(self, client: Neo4jClient):
        self.client = client
        # Кэш max_root_id по user_id
        self._max_root_id_cache: Dict[str, int] = {}
    
    # создание узлов и связей

    def create_zettel(
        self,
        user_id: str,
        content: str,
        luhmann_id: str,
        thought_type: str,
        tags: List[str],
        embedding: List[float],
        is_root_topic: bool = True,
        zettel_id: str = None,
    ) -> ZettelNode:
        """
        Создаёт новый узел Zettel (корневой или без связей).
        """
        zettel_id = zettel_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        
        query = """
        CREATE (z:Zettel {
            zettel_id: $zettel_id,
            user_id: $user_id,
            luhmann_id: $luhmann_id,
            content: $content,
            thought_type: $thought_type,
            tags: $tags,
            embedding: $embedding,
            is_root_topic: $is_root_topic,
            created_at: datetime($created_at),
            updated_at: datetime($created_at)
        })
        RETURN z
        """
        
        self.client.execute_write(query, {
            "zettel_id": zettel_id,
            "user_id": user_id,
            "luhmann_id": luhmann_id,
            "content": content,
            "thought_type": thought_type,
            "tags": tags,
            "embedding": embedding,
            "is_root_topic": is_root_topic,
            "created_at": now,
        })
        
        # каждый тег становится entity-узлом со связью mentions
        self._create_entity_links(user_id, zettel_id, tags)
        
        # сбрасываем кэш max root id при добавлении корня
        if is_root_topic and luhmann_id.isdigit():
            self._max_root_id_cache.pop(user_id, None)
        
        print(f"  [Neo4j] Создан [{luhmann_id}]: {content[:50]}...")
        
        return ZettelNode(
            zettel_id=zettel_id,
            user_id=user_id,
            luhmann_id=luhmann_id,
            content=content,
            thought_type=thought_type,
            tags=tags,
            is_root_topic=is_root_topic,
            embedding=embedding,
        )
    
    def create_child_of(
        self,
        user_id: str,
        content: str,
        luhmann_id: str,
        thought_type: str,
        tags: List[str],
        embedding: List[float],
        parent_zettel_id: str,
        zettel_id: str = None,
    ) -> ZettelNode:
        """
        Создаёт дочерний узел Zettel и связывает его с родителем через CHILD_OF.
        """
        zettel_id = zettel_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        
        query = """
        MATCH (parent:Zettel {zettel_id: $parent_id, user_id: $user_id})
        CREATE (z:Zettel {
            zettel_id: $zettel_id,
            user_id: $user_id,
            luhmann_id: $luhmann_id,
            content: $content,
            thought_type: $thought_type,
            tags: $tags,
            embedding: $embedding,
            is_root_topic: false,
            created_at: datetime($created_at),
            updated_at: datetime($created_at)
        })
        CREATE (z)-[:CHILD_OF {created_at: datetime($created_at)}]->(parent)
        RETURN z, parent.luhmann_id as parent_luhmann
        """
        
        result = self.client.execute_write(query, {
            "zettel_id": zettel_id,
            "user_id": user_id,
            "luhmann_id": luhmann_id,
            "content": content,
            "thought_type": thought_type,
            "tags": tags,
            "embedding": embedding,
            "parent_id": parent_zettel_id,
            "created_at": now,
        })
        
        self._create_entity_links(user_id, zettel_id, tags)
        
        parent_luhmann = result[0]["parent_luhmann"] if result else "?"
        print(f"  [Neo4j] Создан [{luhmann_id}] ← [{parent_luhmann}]")
        
        return ZettelNode(
            zettel_id=zettel_id,
            user_id=user_id,
            luhmann_id=luhmann_id,
            content=content,
            thought_type=thought_type,
            tags=tags,
            is_root_topic=False,
            embedding=embedding,
        )
    
    def update_zettel_content(
        self,
        user_id: str,
        zettel_id: str,
        new_content: str,
        new_embedding: List[float],
        reason: str = "",
    ) -> Optional[ZettelNode]:
        """
        Обновляет контент и эмбеддинг существующего узла (сценарий UPDATE_OF).
        Перезаписывает мысль новым текстом без служебного префикса.
        """
        now = datetime.now(timezone.utc)
        updated_content = new_content
        
        query = """
        MATCH (z:Zettel {zettel_id: $zettel_id, user_id: $user_id})
        SET z.content = $content,
            z.embedding = $embedding,
            z.updated_at = datetime($updated_at)
        RETURN z
        """
        
        result = self.client.execute_write(query, {
            "zettel_id": zettel_id,
            "user_id": user_id,
            "content": updated_content,
            "embedding": new_embedding,
            "updated_at": now.isoformat(),
        })
        
        if not result:
            print(f"  [Neo4j] ⚠️  Zettel {zettel_id} не найден для обновления")
            return None
        
        z = result[0]["z"]
        print(f"  [Neo4j] ♻️  Обновлён [{z['luhmann_id']}]")
        
        return ZettelNode(
            zettel_id=z["zettel_id"],
            user_id=z["user_id"],
            luhmann_id=z["luhmann_id"],
            content=z["content"],
            thought_type=z["thought_type"],
            tags=z["tags"],
            is_root_topic=z["is_root_topic"],
            embedding=z["embedding"],
        )
    
    def _create_entity_links(self, user_id: str, zettel_id: str, tags: List[str]) -> None:
        """Создаёт узлы Entity (привязанные к user_id) и связи MENTIONS."""
        if not tags:
            return
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Entity уникальна в рамках (name, user_id)
        query = """
        MATCH (z:Zettel {zettel_id: $zettel_id, user_id: $user_id})
        UNWIND $tags as tag
        MERGE (e:Entity {name: tag, user_id: $user_id})
        ON CREATE SET e.display_name = tag, e.entity_type = 'tag', e.mention_count = 1
        ON MATCH SET e.mention_count = e.mention_count + 1
        MERGE (z)-[:MENTIONS {created_at: datetime($created_at)}]->(e)
        """
        
        self.client.execute_write(query, {
            "zettel_id": zettel_id,
            "user_id": user_id,
            "tags": tags,
            "created_at": now,
        })
    
    # чтение узлов и метаданных

    def get_by_id(self, user_id: str, zettel_id: str) -> Optional[ZettelNode]:
        """Получает узел по zettel_id (только для данного user_id)."""
        query = """
        MATCH (z:Zettel {zettel_id: $zettel_id, user_id: $user_id})
        RETURN z
        """
        result = self.client.execute_read(query, {"zettel_id": zettel_id, "user_id": user_id})
        
        if not result:
            return None
        
        return self._node_to_zettel(result[0]["z"])
    
    def get_by_luhmann_id(self, user_id: str, luhmann_id: str) -> Optional[ZettelNode]:
        """Получает узел по luhmann_id (только для данного user_id)."""
        query = """
        MATCH (z:Zettel {luhmann_id: $luhmann_id, user_id: $user_id})
        RETURN z
        """
        result = self.client.execute_read(query, {"luhmann_id": luhmann_id, "user_id": user_id})
        
        if not result:
            return None
        
        return self._node_to_zettel(result[0]["z"])
    
    def get_max_root_id(self, user_id: str) -> int:
        """Возвращает максимальный числовой luhmann_id среди корневых узлов пользователя."""
        if user_id in self._max_root_id_cache:
            return self._max_root_id_cache[user_id]
        
        query = """
        MATCH (z:Zettel {user_id: $user_id})
        WHERE z.is_root_topic = true AND z.luhmann_id =~ '^[0-9]+$'
        RETURN max(toInteger(z.luhmann_id)) as max_id
        """
        result = self.client.execute_read(query, {"user_id": user_id})
        
        max_id = result[0]["max_id"] if result and result[0]["max_id"] else 0
        self._max_root_id_cache[user_id] = max_id
        return max_id
    
    def get_siblings(self, user_id: str, parent_luhmann_id: Optional[str]) -> List[str]:
        """
        Возвращает luhmann_id всех детей указанного родителя.
        Если parent_luhmann_id=None, возвращает корневые узлы.
        """
        if parent_luhmann_id is None:
            query = """
            MATCH (z:Zettel {user_id: $user_id})
            WHERE z.is_root_topic = true AND z.luhmann_id =~ '^[0-9]+$'
            RETURN z.luhmann_id as luhmann_id
            """
            result = self.client.execute_read(query, {"user_id": user_id})
        else:
            query = """
            MATCH (z:Zettel {user_id: $user_id})-[:CHILD_OF]->(parent:Zettel {luhmann_id: $parent_luhmann, user_id: $user_id})
            RETURN z.luhmann_id as luhmann_id
            """
            result = self.client.execute_read(query, {"user_id": user_id, "parent_luhmann": parent_luhmann_id})
        
        return [r["luhmann_id"] for r in result]
    
    def total_count(self, user_id: str) -> int:
        """Возвращает количество узлов Zettel для данного пользователя."""
        query = "MATCH (z:Zettel {user_id: $user_id}) RETURN count(z) as cnt"
        result = self.client.execute_read(query, {"user_id": user_id})
        return result[0]["cnt"] if result else 0

    def delete_zettel(self, user_id: str, zettel_id: str) -> Optional[Dict[str, Any]]:
        """
        Удаляет выбранную мысль и её дочернее поддерево.
        После удаления чистит entity без связей mentions.
        """
        query = """
        MATCH (target:Zettel {zettel_id: $zettel_id, user_id: $user_id})
        OPTIONAL MATCH (desc:Zettel {user_id: $user_id})-[:CHILD_OF*1..]->(target)
        WITH target,
             target.content AS content,
             target.luhmann_id AS luhmann_id,
             collect(DISTINCT desc) + target AS to_delete
        UNWIND to_delete AS node
        DETACH DELETE node
        RETURN luhmann_id, content, count(*) AS deleted_count
        """
        result = self.client.execute_write(query, {"zettel_id": zettel_id, "user_id": user_id})
        if not result:
            return None

        row = result[0]
        deleted_count = row.get("deleted_count", 0)
        if deleted_count <= 0:
            return None

        # Удаляем осиротевшие сущности этого пользователя.
        cleanup_query = """
        MATCH (e:Entity {user_id: $user_id})
        WHERE NOT EXISTS {
            MATCH (:Zettel {user_id: $user_id})-[:MENTIONS]->(e)
        }
        WITH collect(e) AS orphan_entities
        FOREACH (ent IN orphan_entities | DELETE ent)
        RETURN size(orphan_entities) AS removed_entities
        """
        cleanup_result = self.client.execute_write(cleanup_query, {"user_id": user_id})
        removed_entities = cleanup_result[0]["removed_entities"] if cleanup_result else 0

        # Инвалидируем кэш root-id для пользователя.
        self._max_root_id_cache.pop(user_id, None)

        return {
            "luhmann_id": row["luhmann_id"],
            "content": row["content"],
            "deleted_count": deleted_count,
            "removed_entities": removed_entities,
        }
    
    # семантический поиск по эмбеддингам (только внутри user_id)

    def vector_search(
        self,
        user_id: str,
        query_embedding: List[float],
        limit: int = 5,
        similarity_threshold: float = 0.3,
    ) -> List[Tuple[ZettelNode, float]]:
        """
        Семантический поиск по эмбеддингам для узлов данного user_id.
        """
        # vector index в neo4j не фильтрует по user_id, поэтому считаем cosine в python
        return self._vector_search_python_fallback(user_id, query_embedding, limit, similarity_threshold)
    
    def _vector_search_python_fallback(
        self,
        user_id: str,
        query_embedding: List[float],
        limit: int,
        threshold: float,
    ) -> List[Tuple[ZettelNode, float]]:
        """
        Косинусный поиск в Python с фильтрацией по user_id.
        """
        import numpy as np
        
        query = """
        MATCH (z:Zettel {user_id: $user_id})
        WHERE z.embedding IS NOT NULL
        RETURN z
        """
        result = self.client.execute_read(query, {"user_id": user_id})
        
        if not result:
            return []
        
        query_vec = np.array(query_embedding)
        query_norm = np.linalg.norm(query_vec)
        
        candidates = []
        for row in result:
            z = row["z"]
            emb = np.array(z["embedding"])
            emb_norm = np.linalg.norm(emb)
            
            if emb_norm < 1e-10 or query_norm < 1e-10:
                continue
            
            score = float(np.dot(query_vec, emb) / (query_norm * emb_norm))
            
            if score >= threshold:
                node = self._node_to_zettel(z)
                node.similarity = score
                candidates.append((node, score))
        
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:limit]
    
    # контекст вокруг узла для линкера и graphrag

    def get_context(self, user_id: str, zettel_id: str, hops: int = 1) -> Optional[GraphContext]:
        """
        Получает контекст вокруг узла: родитель, дети, связанные узлы, сущности.
        """
        main_node = self.get_by_id(user_id, zettel_id)
        if not main_node:
            return None
        
        parent = self._get_parent(user_id, zettel_id)
        children = self._get_children(user_id, zettel_id)
        related = self._get_related(user_id, zettel_id, hops)
        entities = self._get_entities(user_id, zettel_id)
        
        return GraphContext(
            candidate=main_node,
            similarity=main_node.similarity or 0.0,
            parent=parent,
            children=children,
            related=related,
            entities=entities,
        )
    
    def _get_parent(self, user_id: str, zettel_id: str) -> Optional[ZettelNode]:
        query = """
        MATCH (z:Zettel {zettel_id: $zettel_id, user_id: $user_id})-[:CHILD_OF]->(parent:Zettel {user_id: $user_id})
        RETURN parent as z
        """
        result = self.client.execute_read(query, {"zettel_id": zettel_id, "user_id": user_id})
        if not result:
            return None
        return self._node_to_zettel(result[0]["z"])
    
    def _get_children(self, user_id: str, zettel_id: str) -> List[ZettelNode]:
        query = """
        MATCH (child:Zettel {user_id: $user_id})-[:CHILD_OF]->(z:Zettel {zettel_id: $zettel_id, user_id: $user_id})
        RETURN child as z
        """
        result = self.client.execute_read(query, {"zettel_id": zettel_id, "user_id": user_id})
        return [self._node_to_zettel(r["z"]) for r in result]
    
    def _get_related(self, user_id: str, zettel_id: str, hops: int = 1) -> List[ZettelNode]:
        query = f"""
        MATCH (z:Zettel {{zettel_id: $zettel_id, user_id: $user_id}})-[:RELATED_TO*1..{hops}]-(related:Zettel {{user_id: $user_id}})
        WHERE related.zettel_id <> $zettel_id
        RETURN DISTINCT related as z
        LIMIT 10
        """
        result = self.client.execute_read(query, {"zettel_id": zettel_id, "user_id": user_id})
        return [self._node_to_zettel(r["z"]) for r in result]
    
    def _get_entities(self, user_id: str, zettel_id: str) -> List[EntityNode]:
        query = """
        MATCH (z:Zettel {zettel_id: $zettel_id, user_id: $user_id})-[:MENTIONS]->(e:Entity {user_id: $user_id})
        RETURN e
        """
        result = self.client.execute_read(query, {"zettel_id": zettel_id, "user_id": user_id})
        return [
            EntityNode(
                name=r["e"]["name"],
                display_name=r["e"].get("display_name", r["e"]["name"]),
                entity_type=r["e"].get("entity_type", "tag"),
                user_id=r["e"].get("user_id", ""),
                mention_count=r["e"].get("mention_count", 0),
            )
            for r in result
        ]
    
    # вспомогательные методы

    def _node_to_zettel(self, node_dict: dict) -> ZettelNode:
        """Конвертирует Neo4j node dict в ZettelNode."""
        return ZettelNode(
            zettel_id=node_dict["zettel_id"],
            user_id=node_dict.get("user_id", ""),
            luhmann_id=node_dict["luhmann_id"],
            content=node_dict["content"],
            thought_type=node_dict["thought_type"],
            tags=list(node_dict.get("tags", [])),
            is_root_topic=node_dict.get("is_root_topic", False),
            embedding=list(node_dict.get("embedding", [])) if node_dict.get("embedding") else None,
        )
    
    def get_graph_text(self, user_id: str, max_line_len: int = 120) -> str:
        """
        Возвращает граф пользователя в иерархическом текстовом виде.
        
        Формат:
        📌 [1]: ...
          └─ [1.1] ← [1]: ...
        """
        query = """
        MATCH (z:Zettel {user_id: $user_id})
        OPTIONAL MATCH (z)-[:CHILD_OF]->(parent:Zettel {user_id: $user_id})
        RETURN z, parent.luhmann_id as parent_luhmann
        ORDER BY z.luhmann_id
        """
        result = self.client.execute_read(query, {"user_id": user_id})

        if not result:
            return "(пусто)"
        
        # Построим дерево для красивого вывода
        nodes_by_luhmann: Dict[str, dict] = {}
        children_by_parent: Dict[str, List[str]] = {}
        
        for row in result:
            z = row["z"]
            luhmann = z["luhmann_id"]
            parent_luhmann = row["parent_luhmann"]
            
            nodes_by_luhmann[luhmann] = {
                "content": z["content"],
                "is_root": z["is_root_topic"],
                "parent_luhmann": parent_luhmann,
            }
            
            if parent_luhmann:
                children_by_parent.setdefault(parent_luhmann, []).append(luhmann)
        
        lines: List[str] = []

        # Функция для рекурсивного вывода
        def print_node(luhmann: str, depth: int = 0):
            node = nodes_by_luhmann.get(luhmann)
            if not node:
                return
            
            content = node["content"]
            parent = node["parent_luhmann"]
            
            # Обрезаем контент для вывода
            max_len = max(40, max_line_len - depth * 4)
            display_content = content[:max_len] + "..." if len(content) > max_len else content
            
            if node["is_root"]:
                lines.append(f"📌 [{luhmann}]: {display_content}")
            else:
                indent = "  " * depth
                lines.append(f"{indent}└─ [{luhmann}] ← [{parent}]: {display_content}")
            
            # Рекурсивно выводим детей
            children = children_by_parent.get(luhmann, [])
            # Сортируем детей по luhmann_id
            children.sort(key=lambda x: (len(x), x))
            for child in children:
                print_node(child, depth + 1)
        
        # Находим корневые узлы и выводим их
        roots = sorted(
            [luhmann for luhmann, node in nodes_by_luhmann.items() if node["is_root"]],
            key=lambda x: (len(x), x)
        )
        
        for root in roots:
            print_node(root)

        return "\n".join(lines)

    def print_graph(self, user_id: str) -> None:
        """
        Выводит граф пользователя в иерархическом виде:
        
        📌 [1]: Текст корневой мысли...
          └─ [1.1] ← [1]: Текст дочерней мысли...
          └─ [1.2] ← [1]: Текст другой дочерней...
            └─ [1.2a] ← [1.2]: Текст внука...
        """
        print("\n" + "═" * 70)
        print(f"🧠 ГРАФ ЗНАНИЙ (user: {user_id})")
        print("═" * 70)
        print(self.get_graph_text(user_id))
        print("═" * 70 + "\n")

    def export_graph_data(self, user_id: str) -> Dict[str, Any]:
        """
        Экспортирует полный граф пользователя для визуализации:
        zettel-узлы, entity-узлы и все связи (CHILD_OF, RELATED_TO, MENTIONS).
        """
        nodes_query = """
        MATCH (z:Zettel {user_id: $user_id})
        OPTIONAL MATCH (z)-[:CHILD_OF]->(parent:Zettel {user_id: $user_id})
        RETURN z, parent.luhmann_id as parent_luhmann
        """
        nodes_result = self.client.execute_read(nodes_query, {"user_id": user_id})

        edges_query = """
        MATCH (a:Zettel {user_id: $user_id})-[r]->(b)
        WHERE (b:Zettel AND b.user_id = $user_id) OR (b:Entity AND b.user_id = $user_id)
        RETURN a.zettel_id AS from_id,
               type(r) AS rel_type,
               CASE WHEN b:Zettel THEN b.zettel_id ELSE 'entity:' + b.name END AS to_id
        """
        edges_result = self.client.execute_read(edges_query, {"user_id": user_id})

        entities_query = """
        MATCH (z:Zettel {user_id: $user_id})-[:MENTIONS]->(e:Entity {user_id: $user_id})
        RETURN DISTINCT e
        """
        entities_result = self.client.execute_read(entities_query, {"user_id": user_id})

        zettels = []
        for row in nodes_result:
            z = row["z"]
            zettels.append({
                "zettel_id": z["zettel_id"],
                "luhmann_id": z["luhmann_id"],
                "content": z["content"],
                "thought_type": z["thought_type"],
                "tags": list(z.get("tags", [])),
                "is_root_topic": z.get("is_root_topic", False),
                "parent_luhmann": row["parent_luhmann"],
            })

        entities = []
        for row in entities_result:
            e = row["e"]
            entities.append({
                "name": e["name"],
                "display_name": e.get("display_name", e["name"]),
                "entity_type": e.get("entity_type", "tag"),
                "mention_count": e.get("mention_count", 0),
            })

        edges = []
        for row in edges_result:
            edges.append({
                "from_id": row["from_id"],
                "to_id": row["to_id"],
                "rel_type": row["rel_type"],
            })

        return {"zettels": zettels, "entities": entities, "edges": edges}

    def remove_cross_user_links(self) -> int:
        """
        Удаляет любые связи между Zettel разных пользователей.
        Возвращает количество удалённых связей.
        """
        query = """
        MATCH (a:Zettel)-[r]->(b:Zettel)
        WHERE a.user_id IS NOT NULL
          AND b.user_id IS NOT NULL
          AND a.user_id <> b.user_id
        WITH collect(r) as rels
        FOREACH (rel in rels | DELETE rel)
        RETURN size(rels) as deleted_count
        """
        result = self.client.execute_write(query)
        return result[0]["deleted_count"] if result else 0
