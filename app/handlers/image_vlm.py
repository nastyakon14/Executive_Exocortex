"""Общая VLM-часть разбора картинок — одинакова на Windows и macOS."""
import logging
import os

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from httpx import ConnectError, ConnectTimeout, TimeoutException
from openai import APIConnectionError, APITimeoutError
from typing import List
from dotenv import load_dotenv

from config.settings import settings

load_dotenv()

logger = logging.getLogger(__name__)

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
model_name = settings.image_vlm_model


class TableItem(BaseModel):
    number: int = Field(..., description="Порядковый номер таблицы (начиная с 1)")
    content: str = Field(..., description="Содержимое таблицы в формате markdown")
    max_columns: int = Field(..., description="Максимальное количество столбцов")


class TablesList(BaseModel):
    tables: List[TableItem] = Field(..., description="Список найденных таблиц")
    found_relevant_flag: bool = Field(..., description="True если найдены таблицы")


http_client = httpx.Client(verify=False)
headers = {"X-ASGK-TOKEN": f"{LLM_API_KEY}"}

llm = ChatOpenAI(
    model=model_name,
    base_url=f"{LLM_BASE_URL}/v1",
    api_key=LLM_API_KEY,
    default_headers=headers,
    http_client=http_client,
    temperature=0,
    timeout=150,
    max_tokens=10000,
)
structured_llm = llm.with_structured_output(TablesList)


def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        import base64
        return base64.b64encode(image_file.read()).decode("utf-8")


@retry(
    retry=retry_if_exception_type((
        TimeoutError,
        APITimeoutError,
        APIConnectionError,
        TimeoutException,
        ConnectError,
        ConnectTimeout,
    )),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    after=after_log(logger, logging.INFO),
)
def invoke_llm(base64_image, structured_llm=structured_llm):
    system_prompt = settings.image_vlm_system_prompt
    messages = [
        SystemMessage(content=[{"type": "text", "text": system_prompt}]),
        HumanMessage(content=[{
            "type": "image_url",
            "image_url": f"data:image/jpeg;base64,{base64_image}",
        }]),
    ]
    try:
        return structured_llm.invoke(messages)
    except Exception as e:
        logger.error("Ошибка LLM: %s: %s", type(e).__name__, e)
        return None


def VL_extract_table(image_path) -> str:
    """Извлекает таблицы через VLM. Возвращает markdown или сообщение об ошибке."""
    print(f"Обработка через VLM: {image_path}")
    tables_vl = invoke_llm(encode_image(image_path))
    if tables_vl is None:
        return "Ошибка при обращении к VLM"
    if not tables_vl.found_relevant_flag or not tables_vl.tables:
        return "Таблицы не найдены"
    markdown_parts = []
    for tab in tables_vl.tables:
        markdown_parts.append(f"**Таблица {tab.number}:**\n{tab.content}")
    return "\n\n".join(markdown_parts)
