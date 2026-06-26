from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from config.prompts import zettel_atomizer_prompt, linker_system_prompt, RAG_generator_prompt

class Settings(BaseSettings):
    # .env в корне проекта
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')
    
    # zettelkasten
    zettel_atomizer_model_name: str = "openai/gpt-4o"
    zettel_atomizer_temperature: float = 0.0
    zettel_atomizer_prompt: str = zettel_atomizer_prompt
    linker_model_name: str = "openai/gpt-4o"
    linker_system_prompt: str = linker_system_prompt
    
    # embeddings
    embedding_model_name: str = "intfloat/multilingual-e5-base"

    # chromadb
    # режимы: "http" (Docker) или "persistent" (Локально)
    chroma_mode: Literal["http", "persistent", "memory"] = "http" 
    # для http режима
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    # для persistent режима
    chroma_persist_dir: str = "./storage/chromadb/data"
    chroma_collection_name: str = "zettelkasten_nodes" 


settings = Settings()