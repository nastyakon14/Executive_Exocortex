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
                        last_error = '',
                        updated_at = NOW()
                    WHERE id = %s
                    ''',
                    (content_hash, watch_id),
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
                        last_error = '',
                        updated_at = NOW()
                    WHERE graph_id = %s AND source_kind = %s AND source_path = %s
                    ''',
                    (content_hash, graph_id, source_kind, source_path),
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
    if not graph_id or not source_input:
        return None
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT content_hash FROM ingest_digests
                WHERE graph_id = %s AND source_input = %s
                ''',
                (graph_id, source_input),
            )
            row = cursor.fetchone()
            return row[0] if row else None


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


def update_history_messages(user_id, message_id, message_text, message_date, message_type, bot_answer):
    """Сохраняет пару «сообщение пользователя — ответ бота» в postgres."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO history_messages (user_id, message_id, message_text, message_date, message_type, bot_answer)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (user_id, message_id, message_text, message_date, message_type, bot_answer))
            conn.commit()