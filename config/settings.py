# config/settings.py

from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):

    # LLM
    model_name: str = "openai/gpt-4o"
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 3072 
    
    # storage

    

settings = Settings()
print(f"Loaded settings: {settings.dict()}")