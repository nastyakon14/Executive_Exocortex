"""Картинки на macOS: системный tesseract, затем тот же VLM, что на Windows."""
import logging
import os
import re
import shutil
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

logger = logging.getLogger(__name__)

_ocr = None
_tesseract_ready = False
_tesseract_available = False


def _configure_tesseract() -> bool:
    global _tesseract_ready, _tesseract_available, _ocr
    if _tesseract_ready:
        return _tesseract_available

    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    tessdata = os.environ.get("TESSDATA_PREFIX") or ""
    if not cmd:
        for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
            if os.path.isfile(candidate):
                cmd = candidate
                break
    if not cmd:
        _tesseract_ready = True
        _tesseract_available = False
        _ocr = None
        return False

    pytesseract.pytesseract.tesseract_cmd = cmd
    if tessdata and os.path.isdir(tessdata):
        os.environ["TESSDATA_PREFIX"] = tessdata
    os.environ["PATH"] = os.path.dirname(cmd) + os.pathsep + os.environ.get("PATH", "")
    try:
        subprocess.run([cmd, "--list-langs"], capture_output=True, text=True, timeout=8)
    except Exception:
        pass
    try:
        kwargs = {"n_threads": 4, "lang": "rus+eng"}
        if tessdata and os.path.isdir(tessdata):
            kwargs["tessdata_dir"] = tessdata
        _ocr = TesseractOCR(**kwargs)
    except Exception as e:
        logger.warning("img2table OCR недоступен: %s", e)
        _ocr = None
    _tesseract_ready = True
    _tesseract_available = True
    return True


def cleaning_text(text):
    return re.sub(r"\n\n", "\n", text)


def extract_png(input_path) -> str:
    if not _configure_tesseract():
        return ""
    img = PIL.Image.open(input_path)
    try:
        try:
            text = cleaning_text(pytesseract.image_to_string(img, lang="rus+eng").strip())
        except Exception:
            text = cleaning_text(pytesseract.image_to_string(img).strip())
        return text if text else "Текст не найден"
    finally:
        img.close()


def df_to_markdown(df: pd.DataFrame) -> str:
    df.columns = [str(c) if pd.notna(c) else "" for c in df.columns]
    df = df.fillna("")
    header = "| " + " | ".join(df.columns) + " |"
    separator = "| " + " | ".join(["---"] * len(df.columns)) + " |"
    rows = ["| " + " | ".join(str(v) for v in row) + " |"
            for row in df.itertuples(index=False)]
    return "\n".join([header, separator] + rows)


def table_processing(input_path) -> str:
    if not _configure_tesseract() or _ocr is None:
        return ""
    doc = Image(input_path)
    extracted = doc.extract_tables(
        ocr=_ocr,
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
