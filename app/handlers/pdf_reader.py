import os
import re 
import pytesseract
import pdfplumber
import pandas as pd

def cleaning_text(text):
    """Нормализует переносы строк в тексте после ocr."""
    text = re.sub(r'\n\n', '\n', text)
    return text


def extract_text_pdf2image(page):
    """Конвертирует страницу pdf в изображение и извлекает текст через tesseract."""
    try:
        image = page.to_image(resolution=300).original
        text = cleaning_text(pytesseract.image_to_string(image, lang='rus+eng').strip())
    except Exception:
        text = ''
    return text

def extract_tables(file_path):
    """Извлекает таблицы из pdf и возвращает их в markdown по номерам страниц."""
    all_tables = {}
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                df = pd.DataFrame(table[1:], columns=table[0]) 
                all_tables[page.page_number] = df.to_markdown()                  
        
    return all_tables

def tables_to_pages(tables, page_text_dict):
    """Добавляет markdown-таблицы к тексту соответствующих страниц."""
    for page_num, table in tables.items():
        page_text_dict[page_num - 1] += f'\n\n{table}'
    return page_text_dict


def read_pdf(file_path):
    """
    Читает pdf: ocr текста по страницам + таблицы.
    Возвращает словарь {номер_страницы: текст}.
    """
    page_text_dict = {}

    with pdfplumber.open(file_path) as pdf:
        num_pages = len(pdf.pages)
        for page_num in range(num_pages):
            page = pdf.pages[page_num]
            page_text_dict[page_num] = extract_text_pdf2image(page)

        tables = extract_tables(file_path)
        if len(tables) > 0:
            page_text_dict = tables_to_pages(tables, page_text_dict)

    return page_text_dict
