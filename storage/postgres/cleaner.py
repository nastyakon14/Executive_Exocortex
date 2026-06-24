import psycopg2
from db_connect import get_connection, DB_CONFIG

conn = psycopg2.connect(**DB_CONFIG)
print(conn)

def clean_history_messages(conn):
    '''Очистка истории сообщений'''
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM history_messages")
        conn.commit()
    print("История сообщений очищена")

clean_history_messages(conn)