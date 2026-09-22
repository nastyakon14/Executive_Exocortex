import pandas as pd
import os


def read_file(file_path, sheet_name):
        try:
            df = pd.read_excel(file_path, sheet_name = sheet_name) 
        except:
            df = pd.read_excel(file_path, sheet_name = sheet_name, engine = 'openpyxl')
        # удаляем полностью псутые строки и полностью пустые столбцы
        df = df.dropna(how = 'all', axis = 0).dropna(how = 'all', axis = 1).fillna('').to_markdown()
        return df

def read_excel(file_path):
        # список всех листов эксель
        sheet_names = pd.ExcelFile(file_path).sheet_names

        # отдельно обрабатываем каждый лист и склеиваем с заголовком
        parts = []
        # for sheet_name in tqdm(sheet_names, desc = 'Обработка листов'):
        for sheet_name in sheet_names:
            df = read_file(file_path, sheet_name)
            if df:
                parts.append(f"Лист: {sheet_name}\n{df}")
        return "\n\n".join(parts)