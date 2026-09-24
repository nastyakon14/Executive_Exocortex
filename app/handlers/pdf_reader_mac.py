"""PDF на macOS: встроенный текст, иначе системный tesseract (Homebrew)."""
import os
import re
import shutil

import pandas as pd
import pdfplumber
import pytesseract


def _configure_tesseract() -> None:
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not cmd:
        for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
            if os.path.isfile(candidate):
                cmd = candidate
                break
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd


_configure_tesseract()


def cleaning_text(text):
    """Нормализует переносы строк в тексте после ocr."""
    text = re.sub(r"\n\n", "\n", text)
    return text


def extract_text_pdf2image(page):
    """Конвертирует страницу pdf в изображение и извлекает текст через tesseract."""
    image = None
    try:
        image = page.to_image(resolution=150).original
        text = cleaning_text(pytesseract.image_to_string(image, lang="rus+eng").strip())
    except MemoryError:
        text = ""
    except Exception:
        text = ""
    finally:
        if image is not None:
            try:
                image.close()
            except Exception:
                pass
        try:
            page.flush_cache()
        except Exception:
            pass
    return text


def extract_text_native(page) -> str:
    """Текст, который PDF уже содержит, без OCR."""
    try:
        return cleaning_text((page.extract_text() or "").strip())
    except Exception:
        return ""


def extract_tables(file_path):
    """Извлекает таблицы из pdf и возвращает их в markdown по номерам страниц."""
    all_tables = {}
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            try:
                tables = page.extract_tables() or []
            except Exception:
                continue
            for table in tables:
                if not table:
                    continue
                try:
                    header = table[0] if table else []
                    rows = table[1:] if len(table) > 1 else []
                    df = pd.DataFrame(rows, columns=header)
                    all_tables[page.page_number] = df.to_markdown()
                except Exception:
                    continue
    return all_tables


def tables_to_pages(tables, page_text_dict):
    """Добавляет markdown-таблицы к тексту соответствующих страниц."""
    for page_num, table in tables.items():
        idx = page_num - 1
        page_text_dict[idx] = (page_text_dict.get(idx) or "") + f"\n\n{table}"
    return page_text_dict


def read_pdf(file_path):
    """
    Читает pdf: сначала встроенный текст, иначе OCR, плюс таблицы.
    Возвращает словарь {номер_страницы: текст}.
    """
    page_text_dict = {}

    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = extract_text_native(page)
            if len(text) < 40:
                ocr = extract_text_pdf2image(page)
                if ocr:
                    text = ocr
            try:
                tables = page.extract_tables() or []
                for table in tables:
                    if not table:
                        continue
                    header = table[0] if table else []
                    rows = table[1:] if len(table) > 1 else []
                    df = pd.DataFrame(rows, columns=header)
                    text = (text or "") + f"\n\n{df.to_markdown()}"
            except Exception:
                pass
            page_text_dict[page_num] = text
            try:
                page.flush_cache()
            except Exception:
                pass

    return page_text_dict
