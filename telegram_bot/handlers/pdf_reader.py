import os
import re 
import pytesseract
import pdfplumber
import pandas as pd

def cleaning_text(text):
    '''Очистка текста'''
    text = re.sub(r'\n\n', '\n', text)
    return text


# конвертер страницы пдф в изображение и извлечение текста из нее
def extract_text_pdf2image(page):
    # преобразовываем страницу пдф в изображение
    try:
        image = page.to_image(resolution=300).original
        # display(image)
        # text = (pytesseract.image_to_string(image, lang = 'rus+eng').strip())
        # считываем текст с изображения
        text = cleaning_text(pytesseract.image_to_string(image, lang = 'rus+eng').strip())
    except: text = ''
    return text

# извлечение таблиц из pdf файла
def extract_tables(file_path):
    '''Извлечение таблиц из pdf файла'''
    all_tables = {}
    # Открываем PDF
    with pdfplumber.open(file_path) as pdf:
        # Проходимся по всем страницам
        for page in pdf.pages:
            # Извлекаем таблицы со страницы
            tables = page.extract_tables()
                
            for table in tables:  # отдельно сохраняем каждую найденную таблицу

                df = pd.DataFrame(table[1:], columns=table[0]) 
                all_tables[page.page_number] = df.to_markdown()                  
        
    return all_tables

# добавляем найденные таблицы на нужные страницы
def tables_to_pages(tables, page_text_dict):
    '''добавляем найденные таблицы на нужные страницы'''
    for page_num, table in tables.items():
        page_text_dict[page_num - 1] += f'\n\n{table}'
    return page_text_dict


def read_pdf(file_path):
    '''Чтение pdf файла'''
    # извлекаем все текстовое содержимое страницы, конвертируя в изображение
    # словарь - номер страницы: текст
    page_text_dict = {}

    with pdfplumber.open(file_path) as pdf:  # открываем каждый файл
        num_pages = len(pdf.pages)  # кол-во страниц в пдф документе
        for page_num in (range(num_pages)): #num_pages  # по каждой странице
            page = pdf.pages[page_num]
            # извлекаем текст из страницы
            text = extract_text_pdf2image(page)
            page_text_dict[page_num] = text # словарь - номер страницы: текст

        # извлекаем все таблицы из файла
        tables = extract_tables(file_path)
        
        # найдена хотя бы 1 таблица
        if len(tables) > 0:
            # добавляем найденные таблицы на страницы
            page_text_dict = tables_to_pages(tables, page_text_dict)

    # возвращаем словарь - номер страницы: текст - все тексты из страниц и таблиц
    return page_text_dict
