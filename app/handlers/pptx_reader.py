from pptxtopdf import convert
import os
from pathlib import Path
try:
    from app.handlers.pdf_reader import read_pdf
except ImportError:
    from pdf_reader import read_pdf
 
def pptx_to_pdf(input_path, output_folder = 'convert_pdf'):
    # конвертируем .pptx/.ppt в .pdf
    file_name = Path(input_path).stem
    output_path = os.path.join(output_folder, f'{file_name}.pdf')
    # создает директорию с названием output_folder и помещает в нее конвертированный файл
    convert(input_path, output_folder)
    # после конвертации читаем и обрабатываем pdf
    text = read_pdf(output_path)
    # удаляем временный файл
    os.remove(output_path)
    return text