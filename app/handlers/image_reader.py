import pytesseract
import PIL
import subprocess
from img2table.document import Image
from img2table.ocr import TesseractOCR
import warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
import os
import re
import base64
import httpx
import urllib3
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field
from typing import List
from pathlib import Path
import logging
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    after_log
)
from httpx import TimeoutException, ConnectError, ConnectTimeout
from openai import APITimeoutError, APIConnectionError
from dotenv import load_dotenv

from config.settings import settings


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


LLM_API_KEY = os.getenv('LLM_API_KEY')
LLM_BASE_URL = os.getenv('LLM_BASE_URL')
model_name = settings.image_vlm_model

def get_home_dir():
    if os.name == 'posix':
        return '/mnt/analytics_unit'
    elif os.name == 'nt':
        return r'\\0001fsrvau01\fs_analytics_unit'

home_dir = get_home_dir()

TESSERACT_DIR = os.path.join(home_dir, 'Projects', '2026', '34. IDP opensource',
                              '3. Data processing', 'Tesseract-OCR')
TESSERACT_EXE = os.path.join(TESSERACT_DIR, 'tesseract.exe')
TESSDATA_DIR = os.path.join(TESSERACT_DIR, "tessdata")

pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE
os.environ['TESSDATA_PREFIX'] = TESSDATA_DIR
os.environ['PATH'] = TESSERACT_DIR + os.pathsep + os.environ.get('PATH', '')

result = subprocess.run([TESSERACT_EXE, '--list-langs'], capture_output=True, text=True)
print("Доступные языки:", result.stdout)

ocr = TesseractOCR(
    n_threads=4,
    lang="rus+eng",
    tessdata_dir=TESSDATA_DIR
)


def cleaning_text(text):
    text = re.sub(r'\n\n', '\n', text)
    return text


def encode_image(image_path):
    with open(image_path, 'rb') as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')


# TEXT — извлечение текста через pytesseract
def extract_png(input_path) -> str:
    """Извлекает текст из изображения, возвращает строку"""
    img = PIL.Image.open(input_path)
    text = cleaning_text(
        pytesseract.image_to_string(img, lang='rus+eng').strip()
    )
    return text if text else 'Текст не найден'


# TABLE — извлечение таблиц через pytesseract → markdown
def df_to_markdown(df: pd.DataFrame) -> str:
    """Конвертирует DataFrame в markdown-таблицу"""
    df.columns = [str(c) if pd.notna(c) else "" for c in df.columns]
    df = df.fillna("")

    header = "| " + " | ".join(df.columns) + " |"
    separator = "| " + " | ".join(["---"] * len(df.columns)) + " |"
    rows = ["| " + " | ".join(str(v) for v in row) + " |"
            for row in df.itertuples(index=False)]

    return "\n".join([header, separator] + rows)


def table_processing(input_path) -> str:
    """
    Извлекает таблицы через pytesseract.
    Возвращает строку с markdown-таблицами или пустую строку если таблиц нет.
    """
    doc = Image(input_path)
    extracted = doc.extract_tables(
        ocr=ocr,
        implicit_rows=True,
        implicit_columns=False,
        borderless_tables=False,
        min_confidence=50
    )

    if not extracted:
        return ""

    markdown_tables = []
    for idx, table in enumerate(extracted):
        df = table.df

        if len(df) > 1:
            df = pd.DataFrame(df.iloc[1:].values, columns=list(df.iloc[0].values))

        markdown_tables.append(f"**Таблица {idx + 1}:**\n{df_to_markdown(df)}")

    return "\n\n".join(markdown_tables)


# VLM — извлечение таблиц через LLM

class TableItem(BaseModel):
    number: int = Field(..., description='Порядковый номер таблицы (начиная с 1)')
    content: str = Field(..., description='Содержимое таблицы в формате markdown')
    max_columns: int = Field(..., description='Максимальное количество столбцов')


class TablesList(BaseModel):
    tables: List[TableItem] = Field(..., description='Список найденных таблиц')
    found_relevant_flag: bool = Field(..., description='True если найдены таблицы')


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
    max_tokens=10000
)
structured_llm = llm.with_structured_output(TablesList)


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
    after=after_log(logger, logging.INFO)
)
def invoke_llm(base64_image, structured_llm=structured_llm):

    system_prompt = settings.image_vlm_system_prompt 


    messages = [
        SystemMessage(content=[{'type': 'text', 'text': system_prompt}]),
        HumanMessage(content=[{
            'type': 'image_url',
            'image_url': f"data:image/jpeg;base64,{base64_image}"
        }])
    ]

    try:
        return structured_llm.invoke(messages)
    except Exception as e:
        logger.error(f"Ошибка LLM: {type(e).__name__}: {e}")
        return None


def VL_extract_table(image_path) -> str:
    """
    Извлекает таблицы через VLM.
    Возвращает строку с markdown-таблицами или сообщение об ошибке.
    """
    print(f'Обработка через VLM: {image_path}')
    base64_image = encode_image(image_path)
    tables_vl = invoke_llm(base64_image)

    if tables_vl is None:
        return 'Ошибка при обращении к VLM'

    if not tables_vl.found_relevant_flag or not tables_vl.tables:
        return 'Таблицы не найдены'

    # Возвращаем markdown напрямую из LLM (уже готовый)
    markdown_parts = []
    for tab in tables_vl.tables:
        markdown_parts.append(f"**Таблица {tab.number}:**\n{tab.content}")

    return "\n\n".join(markdown_parts)


def process_image(input_path: str) -> str:
    """
    Принимает путь к изображению (png/jpg/jpeg).
    Возвращает строку:
      - текст (если текстовое изображение)
      - markdown-таблицы (если таблица найдена)
      - комбинацию текста и таблиц
    """
    result_parts = []

    # 1. Извлекаем текст
    text = extract_png(input_path)
    if text and text != 'Текст не найден':
        result_parts.append(text)

    # 2. Пробуем извлечь таблицы через pytesseract
    try:
        tables_md = table_processing(input_path)

        if tables_md:
            result_parts.append(tables_md)
        else:
            # 3. Если pytesseract не нашёл таблицы → пробуем VLM
            print('pytesseract не извлёк таблицы → пробуем VLM')
            tables_vlm = VL_extract_table(input_path)
            result_parts.append(tables_vlm)

    except Exception as e:
        print(f'Ошибка pytesseract → пробуем VLM. Ошибка: {e}')
        try:
            tables_vlm = VL_extract_table(input_path)
            result_parts.append(tables_vlm)
        except Exception as e2:
            result_parts.append(f'Ошибка извлечения таблиц: {e2}')

    return "\n\n".join(result_parts) if result_parts else 'Данные не найдены'
