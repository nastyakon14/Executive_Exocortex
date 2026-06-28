# storage/neo4j/client.py
# подключение к neo4j и базовые операции с cypher

import os
from pathlib import Path
from typing import Optional, Any, Dict, List
from contextlib import contextmanager

from neo4j import GraphDatabase, Driver, Session
from neo4j.exceptions import ServiceUnavailable, AuthError
from dotenv import load_dotenv

# .env всегда из корня проекта (не зависит от cwd)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

from config.settings import settings


class Neo4jClient:
    """
    Клиент для работы с Neo4j.
    Поддерживает подключение через URI (bolt:// или neo4j://).
    """

    def __init__(
        self,
        uri: str = None,
        user: str = None,
        password: str = None,
        database: str = "neo4j",
    ):
        # В ноутбуках settings может быть уже импортирован до загрузки .env.
        # Поэтому делаем fallback на os.getenv, чтобы клиент был устойчивее.
        self.uri = uri or settings.neo4j_uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or settings.neo4j_user or os.getenv("NEO4J_USER", "neo4j")
        self.password = (
            password
            if password is not None
            else (settings.neo4j_password or os.getenv("NEO4J_PASSWORD", ""))
        )
        print(self.password)
        self.database = (
            database
            or settings.neo4j_database
            or os.getenv("NEO4J_DATABASE", "neo4j")
        )

        if not self.password:
            raise ValueError(
                "NEO4J_PASSWORD не задан. Укажите пароль в .env "
                "(тот же, что используется в docker-compose: NEO4J_AUTH=neo4j/<password>)."
            )

        self._driver: Optional[Driver] = None

    def connect(self) -> None:
        """Устанавливает соединение с Neo4j."""
        if self._driver is not None:
            return

        try:
            self._driver = GraphDatabase.driver(
                self.uri,
                auth=(self.user, self.password),
            )
            self._driver.verify_connectivity()
            print(f"[Neo4j] Подключено к {self.uri}")
        except ServiceUnavailable as e:
            raise ConnectionError(
                f"[Neo4j] Сервер недоступен ({self.uri}). "
                f"Запустите: docker compose up -d neo4j. Ошибка: {e}"
            )
        except AuthError as e:
            raise ConnectionError(
                f"[Neo4j] Ошибка аутентификации для user={self.user!r}. "
                f"Пароль Neo4j задаётся только при ПЕРВОМ запуске контейнера. "
                f"Если вы меняли NEO4J_PASSWORD в .env после первого старта, "
                f"нужно сбросить данные: docker compose stop neo4j && "
                f"rm -rf storage/neo4j/data/* && docker compose up -d neo4j. "
                f"Ошибка: {e}"
            )

    def close(self) -> None:
        """Закрывает соединение."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None
            print("[Neo4j] Соединение закрыто")

    @property
    def driver(self) -> Driver:
        """Возвращает драйвер, подключаясь при необходимости."""
        if self._driver is None:
            self.connect()
        return self._driver

    @contextmanager
    def session(self):
        """Context manager для сессии Neo4j."""
        session = self.driver.session(database=self.database)
        try:
            yield session
        finally:
            session.close()

    def execute_query(
        self,
        query: str,
        parameters: Dict[str, Any] = None,
        write: bool = False,
    ) -> List[Dict[str, Any]]:
        """Выполняет Cypher-запрос и возвращает результат."""
        with self.session() as session:
            if write:
                result = session.execute_write(
                    lambda tx: list(tx.run(query, parameters or {}))
                )
            else:
                result = session.execute_read(
                    lambda tx: list(tx.run(query, parameters or {}))
                )
            return [dict(record) for record in result]

    def execute_write(self, query: str, parameters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        return self.execute_query(query, parameters, write=True)

    def execute_read(self, query: str, parameters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        return self.execute_query(query, parameters, write=False)

    def health_check(self) -> bool:
        try:
            self.driver.verify_connectivity()
            return True
        except Exception:
            return False


_client: Optional[Neo4jClient] = None


def get_neo4j_client() -> Neo4jClient:
    """Возвращает глобальный клиент Neo4j (lazy initialization)."""
    global _client
    if _client is None:
        _client = Neo4jClient()
        _client.connect()
    return _client
