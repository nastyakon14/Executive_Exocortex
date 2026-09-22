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
            conn.commit()
            print('Таблицы history_messages и watch_sources готовы.')


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


def list_watch_sources(watch_only: bool = True) -> list[dict]:
    """Список источников для будущего auto-refresh."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            sql = '''
                SELECT id, graph_id, project_slug, source_kind, source_path,
                       watch, extract_child, created_at, updated_at
                FROM watch_sources
            '''
            if watch_only:
                sql += ' WHERE watch = TRUE'
            sql += ' ORDER BY updated_at DESC'
            cursor.execute(sql)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

def update_history_messages(user_id, message_id, message_text, message_date, message_type, bot_answer):
    """Сохраняет пару «сообщение пользователя — ответ бота» в postgres."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO history_messages (user_id, message_id, message_text, message_date, message_type, bot_answer)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (user_id, message_id, message_text, message_date, message_type, bot_answer))
            conn.commit()