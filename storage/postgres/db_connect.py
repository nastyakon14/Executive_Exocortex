from __future__ import annotations
import psycopg2
from psycopg2 import errors
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from dotenv import load_dotenv
import os

load_dotenv()

user = 'postgres'
password = os.getenv('POSTGRES_PASSWORD')
db_name = 'ExoCortex_Bot'
host = 'localhost'
port = 5432

DB_CONFIG = {
    'dbname': db_name,
    'user': user,
    'password': password,
    'host': host,  
    'port': port
}

def create_database():
    """Создаёт рабочую базу данных, если её ещё нет."""
    conn = None
    try:
        # подключаемся к системной базе postgres для create database
        conn = psycopg2.connect(dbname='postgres', user=user, password=password, host=host, port=port)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        
        with conn.cursor() as cursor:
            # проверяем, существует ли база данных
            cursor.execute(f"SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{db_name}'")
            exists = cursor.fetchone()
            
            if not exists:
                cursor.execute(f'CREATE DATABASE "{db_name}"')
                print(f'База данных {db_name} успешно создана.')
            else:
                print(f'База данных {db_name} уже существует.')
    except Exception as e:
        print(f"Ошибка при проверке/создании базы данных: {e}")
    finally:
        if conn:
            conn.close()

def get_connection():
    '''Установка соединения с рабочей базой данных'''
    return psycopg2.connect(**DB_CONFIG)

def create_tables():
    """Создаёт таблицу history_messages для аудита диалогов с ботом."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS history_messages (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    message_text TEXT NOT NULL,
                    message_date TIMESTAMP NOT NULL,
                    message_type TEXT NOT NULL,
                    bot_answer TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS watch_sources (
                    id SERIAL PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    project_slug TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    watch BOOLEAN NOT NULL DEFAULT FALSE,
                    extract_child BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            ''')
            cursor.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS watch_sources_uniq
                ON watch_sources (graph_id, source_kind, source_path)
            ''')
            cursor.execute('ALTER TABLE watch_sources ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP')
            cursor.execute('ALTER TABLE watch_sources ADD COLUMN IF NOT EXISTS content_hash TEXT')
            cursor.execute('ALTER TABLE watch_sources ADD COLUMN IF NOT EXISTS last_error TEXT')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ingest_digests (
                    graph_id TEXT NOT NULL,
                    source_input TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (graph_id, source_input)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS ingest_digests_hash_idx
                ON ingest_digests (graph_id, content_hash)
            ''')
            conn.commit()
            print('Таблицы history_messages, watch_sources и ingest_digests готовы.')


def upsert_watch_source(
    graph_id: str,
    project_slug: str,
    source_kind: str,
    source_path: str,
    watch: bool,
    extract_child: bool = False,
) -> None:
    """Сохраняет флаг автообновления для директории или страницы Confluence."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                INSERT INTO watch_sources (
                    graph_id, project_slug, source_kind, source_path,
                    watch, extract_child, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (graph_id, source_kind, source_path)
                DO UPDATE SET
                    watch = EXCLUDED.watch,
                    extract_child = EXCLUDED.extract_child,
                    project_slug = EXCLUDED.project_slug,
                    updated_at = NOW()
                ''',
                (graph_id, project_slug, source_kind, source_path, bool(watch), bool(extract_child)),
            )
            conn.commit()


def set_watch_source_state(
    watch_id: int,
    watch: bool,
    content_hash: str | None = None,
) -> None:
    """Меняет флаг отслеживания и, если передан, список исключённых путей внутри источника."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                UPDATE watch_sources
                SET watch = %s,
                    content_hash = COALESCE(%s, content_hash),
                    updated_at = NOW()
                WHERE id = %s
                ''',
                (bool(watch), content_hash, watch_id),
            )
            conn.commit()


def list_watch_sources(
    watch_only: bool = True,
    graph_id: str | None = None,
    project_slug: str | None = None,
) -> list[dict]:
    """Список источников для auto-refresh."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            sql = '''
                SELECT id, graph_id, project_slug, source_kind, source_path,
                       watch, extract_child, created_at, updated_at,
                       last_synced_at, content_hash, last_error
                FROM watch_sources
                WHERE 1=1
            '''
            params: list = []
            if watch_only:
                sql += ' AND watch = TRUE'
            if graph_id:
                sql += ' AND graph_id = %s'
                params.append(graph_id)
            if project_slug:
                sql += ' AND project_slug = %s'
                params.append(project_slug)
            sql += ' ORDER BY updated_at DESC'
            cursor.execute(sql, params)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]


def mark_watch_synced(
    watch_id: int,
    content_hash: str | None = None,
    error: str = "",
    synced: bool = True,
) -> None:
    """Пишет метку успешного синка или ошибку ночного прогона."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if synced:
                cursor.execute(
                    '''
                    UPDATE watch_sources
                    SET last_synced_at = NOW(),
                        content_hash = COALESCE(%s, content_hash),
                        last_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    ''',
                    (content_hash, error or "", watch_id),
                )
            else:
                cursor.execute(
                    '''
                    UPDATE watch_sources
                    SET last_error = %s, updated_at = NOW()
                    WHERE id = %s
                    ''',
                    (error or "Ошибка автообновления", watch_id),
                )
            conn.commit()


def mark_watch_synced_path(
    graph_id: str,
    source_kind: str,
    source_path: str,
    content_hash: str | None = None,
    error: str = "",
    synced: bool = True,
) -> None:
    """Пишет метку синка по уникальному ключу источника."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if synced:
                cursor.execute(
                    '''
                    UPDATE watch_sources
                    SET last_synced_at = NOW(),
                        content_hash = COALESCE(%s, content_hash),
                        last_error = %s,
                        updated_at = NOW()
                    WHERE graph_id = %s AND source_kind = %s AND source_path = %s
                    ''',
                    (content_hash, error or "", graph_id, source_kind, source_path),
                )
            else:
                cursor.execute(
                    '''
                    UPDATE watch_sources
                    SET last_error = %s, updated_at = NOW()
                    WHERE graph_id = %s AND source_kind = %s AND source_path = %s
                    ''',
                    (error or "Ошибка автообновления", graph_id, source_kind, source_path),
                )
            conn.commit()


def delete_watch_source(watch_id: int) -> None:
    """Удаляет строку отслеживания источника."""
    if not watch_id:
        return
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('DELETE FROM watch_sources WHERE id = %s', (watch_id,))
            conn.commit()


def delete_watch_source_path(graph_id: str, source_kind: str, source_path: str) -> None:
    """Удаляет строку отслеживания по ключу источника."""
    if not graph_id or not source_kind or not source_path:
        return
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                DELETE FROM watch_sources
                WHERE graph_id = %s AND source_kind = %s AND source_path = %s
                ''',
                (graph_id, source_kind, source_path),
            )
            conn.commit()


def find_ingest_digest_by_hash(graph_id: str, content_hash: str) -> dict | None:
    """Возвращает запись, если этот текст уже встраивали в граф."""
    if not graph_id or not content_hash:
        return None
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT graph_id, source_input, content_hash, created_at
                FROM ingest_digests
                WHERE graph_id = %s AND content_hash = %s
                LIMIT 1
                ''',
                (graph_id, content_hash),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))


def get_ingest_digest(graph_id: str, source_input: str) -> str | None:
    """Хэш последнего успешного ingest для этого источника."""
    row = get_ingest_digest_meta(graph_id, source_input)
    return row["content_hash"] if row else None


def get_ingest_digest_meta(graph_id: str, source_input: str) -> dict | None:
    """Хэш и время последней успешной загрузки источника в этом проекте."""
    if not graph_id or not source_input:
        return None
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT graph_id, source_input, content_hash, created_at
                FROM ingest_digests
                WHERE graph_id = %s AND source_input = %s
                ''',
                (graph_id, source_input),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))


def find_latest_digest_by_source(source_input: str) -> dict | None:
    """Последняя загрузка этого пути или URL в любом проекте."""
    if not source_input:
        return None
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT graph_id, source_input, content_hash, created_at
                FROM ingest_digests
                WHERE source_input = %s
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                (source_input,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))


def upsert_ingest_digest(graph_id: str, source_input: str, content_hash: str) -> None:
    """Запоминает хэш текста после успешного ingest."""
    if not graph_id or not source_input or not content_hash:
        return
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                INSERT INTO ingest_digests (graph_id, source_input, content_hash, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (graph_id, source_input)
                DO UPDATE SET content_hash = EXCLUDED.content_hash, created_at = NOW()
                ''',
                (graph_id, source_input, content_hash),
            )
            conn.commit()


def delete_ingest_digest(graph_id: str, source_input: str) -> None:
    """Снимает отпечаток, когда карточки источника удалены."""
    if not graph_id or not source_input:
        return
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                DELETE FROM ingest_digests
                WHERE graph_id = %s AND source_input = %s
                ''',
                (graph_id, source_input),
            )
            conn.commit()


def _replace_stored_texts(text: str, mapping: dict[str, str]) -> str:
    updated = text or ""
    for old, new in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        if old and old != new:
            updated = updated.replace(old, new)
    return updated


def list_confluence_link_texts() -> list[str]:
    """Строки Postgres, в которых может лежать ссылка Confluence."""
    found: list[str] = []
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT source_input FROM ingest_digests
                WHERE source_input LIKE '%/pages/%'
                '''
            )
            found.extend(str(row[0]) for row in cursor.fetchall() if row and row[0])
            cursor.execute(
                '''
                SELECT source_path, content_hash FROM watch_sources
                WHERE source_kind = 'confluence'
                   OR source_path LIKE '%/pages/%'
                   OR COALESCE(content_hash, '') LIKE '%/pages/%'
                '''
            )
            for source_path, content_hash in cursor.fetchall():
                if source_path:
                    found.append(str(source_path))
                if content_hash:
                    found.append(str(content_hash))
    return found


def rewrite_confluence_link_texts(mapping: dict[str, str]) -> None:
    """Заменяет короткие ссылки Confluence в ingest_digests и watch_sources."""
    if not mapping:
        return
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('SELECT graph_id, source_input, content_hash FROM ingest_digests')
            for graph_id, source_input, content_hash in cursor.fetchall():
                new_source = _replace_stored_texts(str(source_input or ""), mapping)
                if new_source == source_input:
                    continue
                cursor.execute(
                    '''
                    DELETE FROM ingest_digests
                    WHERE graph_id = %s AND source_input = %s
                    ''',
                    (graph_id, source_input),
                )
                cursor.execute(
                    '''
                    INSERT INTO ingest_digests (graph_id, source_input, content_hash, created_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (graph_id, source_input) DO NOTHING
                    ''',
                    (graph_id, new_source, content_hash),
                )
            cursor.execute(
                '''
                SELECT id, source_path, content_hash FROM watch_sources
                WHERE source_kind = 'confluence'
                   OR source_path LIKE '%/pages/%'
                   OR COALESCE(content_hash, '') LIKE '%/pages/%'
                '''
            )
            for watch_id, source_path, content_hash in cursor.fetchall():
                new_path = _replace_stored_texts(str(source_path or ""), mapping)
                new_hash = _replace_stored_texts(str(content_hash or ""), mapping)
                if new_path == source_path and new_hash == content_hash:
                    continue
                cursor.execute('SAVEPOINT conf_link')
                try:
                    cursor.execute(
                        '''
                        UPDATE watch_sources
                        SET source_path = %s, content_hash = %s, updated_at = NOW()
                        WHERE id = %s
                        ''',
                        (new_path, new_hash, watch_id),
                    )
                    cursor.execute('RELEASE SAVEPOINT conf_link')
                except Exception as exc:
                    cursor.execute('ROLLBACK TO SAVEPOINT conf_link')
                    print(f"confluence watch link rewrite skip {source_path}: {exc}")
        conn.commit()


def update_history_messages(user_id, message_id, message_text, message_date, message_type, bot_answer):
    """Сохраняет пару «сообщение пользователя — ответ бота» в postgres."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO history_messages (user_id, message_id, message_text, message_date, message_type, bot_answer)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (user_id, message_id, message_text, message_date, message_type, bot_answer))
            conn.commit()
