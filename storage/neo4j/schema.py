# схема графа: constraints, indexes, vector index
# индексы по user_id обеспечивают изоляцию данных между пользователями

from storage.neo4j.client import Neo4jClient


# Размерность эмбеддингов (intfloat/multilingual-e5-base = 768)
EMBEDDING_DIMENSION = 768


def init_schema(client: Neo4jClient) -> None:
    """
    Инициализирует схему графа в Neo4j:
    - Constraints (уникальность)
    - Indexes (поиск, включая user_id)
    - Vector Index (семантический поиск)
    """
    print("[Neo4j Schema] Инициализация схемы...")
    
    _create_constraints(client)
    _create_indexes(client)
    _create_vector_index(client)
    
    print("[Neo4j Schema] Схема готова")


def _create_constraints(client: Neo4jClient) -> None:
    """Создаёт constraints на уникальность."""
    constraints = [
        # Zettel.zettel_id должен быть уникальным (глобально)
        """
        CREATE CONSTRAINT zettel_id_unique IF NOT EXISTS
        FOR (z:Zettel) REQUIRE z.zettel_id IS UNIQUE
        """,
        # Source.source_id должен быть уникальным (глобально)
        """
        CREATE CONSTRAINT source_id_unique IF NOT EXISTS
        FOR (s:Source) REQUIRE s.source_id IS UNIQUE
        """,
        # Entity уникальна в паре (name, user_id)
        # В Neo4j Community нет составных constraints, используем индекс
    ]
    
    for query in constraints:
        try:
            client.execute_write(query.strip())
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"[Neo4j Schema] Предупреждение при создании constraint: {e}")


def _create_indexes(client: Neo4jClient) -> None:
    """Создаёт индексы для быстрого поиска."""
    indexes = [
        # zettel: составной индекс (user_id, luhmann_id) — основной для поиска
        """
        CREATE INDEX zettel_user_luhmann IF NOT EXISTS
        FOR (z:Zettel) ON (z.user_id, z.luhmann_id)
        """,
        # zettel: фильтрация по пользователю
        """
        CREATE INDEX zettel_user IF NOT EXISTS
        FOR (z:Zettel) ON (z.user_id)
        """,
        # zettel: фильтрация по типу мысли
        """
        CREATE INDEX zettel_type IF NOT EXISTS
        FOR (z:Zettel) ON (z.thought_type)
        """,
        
        # entity: уникальность имени внутри user_id
        """
        CREATE INDEX entity_name_user IF NOT EXISTS
        FOR (e:Entity) ON (e.name, e.user_id)
        """,
        # entity: фильтрация по пользователю
        """
        CREATE INDEX entity_user IF NOT EXISTS
        FOR (e:Entity) ON (e.user_id)
        """,
        # entity: фильтрация по типу
        """
        CREATE INDEX entity_type IF NOT EXISTS
        FOR (e:Entity) ON (e.entity_type)
        """,
        
        # source: фильтрация по пользователю
        """
        CREATE INDEX source_user IF NOT EXISTS
        FOR (s:Source) ON (s.user_id)
        """,
    ]
    
    for query in indexes:
        try:
            client.execute_write(query.strip())
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"[Neo4j Schema] Предупреждение при создании index: {e}")


def _create_vector_index(client: Neo4jClient) -> None:
    """
    Создаёт векторный индекс для семантического поиска.
    Требует neo4j 5.11+ с поддержкой vector indexes.
    
    Важно: vector index не поддерживает фильтрацию по user_id,
    поэтому фильтрация выполняется в python после поиска.
    """
    query = f"""
    CREATE VECTOR INDEX zettel_embedding IF NOT EXISTS
    FOR (z:Zettel) ON (z.embedding)
    OPTIONS {{
        indexConfig: {{
            `vector.dimensions`: {EMBEDDING_DIMENSION},
            `vector.similarity_function`: 'cosine'
        }}
    }}
    """
    
    try:
        client.execute_write(query.strip())
        print(f"[Neo4j Schema] Vector index создан (dim={EMBEDDING_DIMENSION}, cosine)")
    except Exception as e:
        error_str = str(e).lower()
        if "already exists" in error_str:
            print("[Neo4j Schema] Vector index уже существует")
        elif "vector" in error_str and "not supported" in error_str:
            print(
                "[Neo4j Schema] предупреждение: vector index не поддерживается. "
                "Требуется neo4j 5.11+ или auradb. "
                "Семантический поиск будет работать через brute-force (медленнее)."
            )
        else:
            print(f"[Neo4j Schema] Ошибка создания vector index: {e}")


def drop_all_data(client: Neo4jClient) -> None:
    """
    Удаляет все данные из графа (для тестов/отладки).
    Необратимая операция.
    """
    client.execute_write("MATCH (n) DETACH DELETE n")
    print("[Neo4j Schema] Все данные удалены")


def drop_user_data(client: Neo4jClient, user_id: str) -> None:
    """
    Удаляет все данные конкретного пользователя.
    """
    query = """
    MATCH (z:Zettel {user_id: $user_id})
    DETACH DELETE z
    """
    client.execute_write(query, {"user_id": user_id})
    
    query = """
    MATCH (e:Entity {user_id: $user_id})
    DETACH DELETE e
    """
    client.execute_write(query, {"user_id": user_id})
    
    print(f"[Neo4j Schema] Данные пользователя {user_id} удалены")


def get_stats(client: Neo4jClient) -> dict:
    """Возвращает общую статистику графа."""
    result = client.execute_read("""
        MATCH (z:Zettel) WITH count(z) as zettels
        MATCH (e:Entity) WITH zettels, count(e) as entities
        MATCH (s:Source) WITH zettels, entities, count(s) as sources
        MATCH ()-[r]->() WITH zettels, entities, sources, count(r) as relationships
        RETURN zettels, entities, sources, relationships
    """)
    
    if result:
        return result[0]
    return {"zettels": 0, "entities": 0, "sources": 0, "relationships": 0}


def get_user_stats(client: Neo4jClient, user_id: str) -> dict:
    """Возвращает статистику графа для конкретного пользователя."""
    result = client.execute_read("""
        MATCH (z:Zettel {user_id: $user_id}) WITH count(z) as zettels
        MATCH (e:Entity {user_id: $user_id}) WITH zettels, count(e) as entities
        RETURN zettels, entities
    """, {"user_id": user_id})
    
    if result:
        return {"zettels": result[0]["zettels"], "entities": result[0]["entities"]}
    return {"zettels": 0, "entities": 0}
