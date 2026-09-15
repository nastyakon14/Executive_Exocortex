
import win32com.client
import os
from pathlib import Path
try:
    from app.handlers.pdf_reader import read_pdf
except ImportError:
    from pdf_reader import read_pdf

    
def convert_to_pdf(input_file, output_file=None):
    input_file = os.path.abspath(input_file)
    
    if output_file is None:
        base = os.path.splitext(input_file)[0]
        output_file = base + ".pdf"
    
    output_file = os.path.abspath(output_file)
    
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    
    # Отключаем все уведомления
    word.DisplayAlerts = False          # Отключить все диалоги
    word.AutomationSecurity = 3        # Отключить макросы (msoAutomationSecurityForceDisable)
    
    try:
        doc = word.Documents.Open(
            input_file,
            ConfirmConversions=False,   # Не спрашивать про конвертацию форматов
            ReadOnly=True,              # Открыть только для чтения (нет смысла сохранять)
            AddToRecentFiles=False,     # Не добавлять в последние файлы
            PasswordDocument='',        # Пароль документа (если есть)
            Revert=True                 # Перезагрузить если уже открыт
        )
        
        doc.SaveAs(output_file, FileFormat=17)
        # Закрыть без сохранения изменений
        doc.Close(SaveChanges=False)
        
        return output_file
        
    except Exception as e:
        return (f"Ошибка конвертации: {e}")
        
    finally:
        word.Quit(SaveChanges=False)   # Выйти без сохранения

def read_word(input_path):
    
    os.makedirs("word_to_pdf", exist_ok=True) 
    #  для .doc и .docx
    output_name = Path(input_path).stem
    output_path = convert_to_pdf(
        input_path,
        os.path.join('word_to_pdf',f'{output_name}.pdf')
    )
    print(output_path)
    text = read_pdf(output_path)
    # удаляем временный файл
    os.remove(output_path)
    return text