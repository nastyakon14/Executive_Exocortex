"""Картинки на Windows: tesseract.exe с корпоративного пути, затем тот же VLM."""
import os
import re
import subprocess
import warnings

import pandas as pd
import PIL
import pytesseract
from img2table.document import Image
from img2table.ocr import TesseractOCR

warnings.filterwarnings("ignore")

try:
    from app.handlers.image_vlm import VL_extract_table
except ImportError:
    from image_vlm import VL_extract_table


def get_home_dir():
    return r"\\0001fsrvau01\fs_analytics_unit"


home_dir = get_home_dir()
TESSERACT_DIR = os.path.join(
    home_dir, "Projects", "2026", "34. IDP opensource", "3. Data processing", "Tesseract-OCR"
)
TESSERACT_EXE = os.path.join(TESSERACT_DIR, "tesseract.exe")
TESSDATA_DIR = os.path.join(TESSERACT_DIR, "tessdata")

pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE
os.environ["TESSDATA_PREFIX"] = TESSDATA_DIR
os.environ["PATH"] = TESSERACT_DIR + os.pathsep + os.environ.get("PATH", "")

result = subprocess.run(
    [TESSERACT_EXE, "--list-langs"],
    capture_output=True,
    text=True,
)
print("Доступные языки:", result.stdout)

ocr = TesseractOCR(
    n_threads=4,
    lang="rus+eng",
    tessdata_dir=TESSDATA_DIR,
)


def cleaning_text(text):
    return re.sub(r"\n\n", "\n", text)


def extract_png(input_path) -> str:
    img = PIL.Image.open(input_path)
    try:
        text = cleaning_text(pytesseract.image_to_string(img, lang="rus+eng").strip())
    except Exception:
        text = cleaning_text(pytesseract.image_to_string(img).strip())
    return text if text else "Текст не найден"


def df_to_markdown(df: pd.DataFrame) -> str:
    df.columns = [str(c) if pd.notna(c) else "" for c in df.columns]
    df = df.fillna("")
    header = "| " + " | ".join(df.columns) + " |"
    separator = "| " + " | ".join(["---"] * len(df.columns)) + " |"
    rows = ["| " + " | ".join(str(v) for v in row) + " |"
            for row in df.itertuples(index=False)]
    return "\n".join([header, separator] + rows)


def table_processing(input_path) -> str:
    doc = Image(input_path)
    extracted = doc.extract_tables(
        ocr=ocr,
        implicit_rows=True,
        implicit_columns=False,
        borderless_tables=False,
        min_confidence=50,
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


def process_image(input_path: str) -> str:
    result_parts = []
    text = extract_png(input_path)
    if text and text not in {"Текст не найден", ""}:
        result_parts.append(text)
    try:
        tables_md = table_processing(input_path)
        if tables_md:
            result_parts.append(tables_md)
        else:
            print("pytesseract не извлёк таблицы → пробуем VLM")
            result_parts.append(VL_extract_table(input_path))
    except Exception as e:
        print(f"Ошибка pytesseract → пробуем VLM. Ошибка: {e}")
        try:
            result_parts.append(VL_extract_table(input_path))
        except Exception as e2:
            result_parts.append(f"Ошибка извлечения таблиц: {e2}")
    return "\n\n".join(result_parts) if result_parts else "Данные не найдены"
