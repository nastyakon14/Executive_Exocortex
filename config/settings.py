from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from config.prompts import zettel_atomizer_prompt, linker_system_prompt, RAG_generator_prompt

class Settings(BaseSettings):
    # .env в корне проекта
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')
    
    # llm для atomizer и linker
    zettel_atomizer_model_name: str = "openai/gpt-4o"
    zettel_atomizer_temperature: float = 0.0
    zettel_atomizer_prompt: str = zettel_atomizer_prompt
    linker_model_name: str = "openai/gpt-4o"
    linker_system_prompt: str = linker_system_prompt
    
    # локальная модель эмбеддингов (huggingface)
    embedding_model_name: str = "intfloat/multilingual-e5-base"
    # Альтернативы:
    # "intfloat/multilingual-e5-small"  — быстрее, чуть хуже качество
    # "intfloat/multilingual-e5-large"  — медленнее, лучше качество
    # "ai-forever/sbert_large_nlu_ru"   — только русский, отличное качество

    # neo4j — основное хранилище графа
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""  # обязательно задайте в .env (NEO4J_PASSWORD)
    neo4j_database: str = "neo4j"
    
    # параметры линкера
    linker_similarity_threshold: float = 0.35
    linker_max_candidates: int = 5
    
    # параметры graphrag
    graphrag_search_limit: int = 10
    graphrag_context_hops: int = 2

    # chromadb (устарело — оставлено для обратной совместимости)
    chroma_mode: Literal["http", "persistent", "memory"] = "http"
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    chroma_persist_dir: str = "./storage/chromadb/data"
    chroma_collection_name: str = "zettelkasten_nodes" 


settings = Settings()