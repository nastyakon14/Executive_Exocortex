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
            conn.commit()
            print('Таблица history_messages готова.')

def update_history_messages(user_id, message_id, message_text, message_date, message_type, bot_answer):
    """Сохраняет пару «сообщение пользователя — ответ бота» в postgres."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO history_messages (user_id, message_id, message_text, message_date, message_type, bot_answer)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (user_id, message_id, message_text, message_date, message_type, bot_answer))
            conn.commit()