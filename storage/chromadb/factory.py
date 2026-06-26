import os
import chromadb
from chromadb.config import Settings as ChromaSettings
from typing import Union

from config.settings import settings

def get_chroma_client() -> Union[chromadb.Client, chromadb.HttpClient]:
    """
    Создает и возвращает клиент ChromaDB на основе настроек из settings.py / .env
    """
    base_settings = ChromaSettings(anonymized_telemetry=False)

    if settings.chroma_mode == "http":
        print(f"[ChromaDB] Подключение к HTTP-серверу на {settings.chroma_host}:{settings.chroma_port}")
        client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
            settings=base_settings
        )
        # Проверяем живость сервера
        try:
            client.heartbeat()
            print("[ChromaDB] Сервер доступен!")
        except Exception as e:
            raise ConnectionError(f"[ChromaDB] Сервер недоступен. Убедитесь, что запущен docker-compose. Ошибка: {e}")
        
        return client

    elif settings.chroma_mode == "persistent":
        print(f"[ChromaDB] Локальный режим. Сохраняем в: {settings.chroma_persist_dir}")
        os.makedirs(settings.chroma_persist_dir, exist_ok=True)
        return chromadb.PersistentClient(
            path=settings.chroma_persist_dir,
            settings=base_settings
        )
        
    else:
        print("[ChromaDB] Режим In-Memory (данные удалятся при выключении бота)")
        return chromadb.Client(settings=base_settings)