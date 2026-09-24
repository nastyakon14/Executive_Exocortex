"""Обход папки и извлечение текста на Windows."""
import gc
import os
from pathlib import Path

try:
    from app.handlers.txt_reader import read_txt
    from app.handlers.pdf_reader_win import read_pdf
    from app.handlers.size_checker import check_size
except ImportError:
    from txt_reader import read_txt
    from pdf_reader_win import read_pdf
    from size_checker import check_size

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable


EXTRACTABLE_EXTENSIONS = (
    ".txt",
    ".pdf",
    ".pptx",
    ".ppt",
    ".png",
    ".jpg",
    ".jpeg",
    ".doc",
    ".docx",
    ".xlsx",
    ".xls",
    ".xlsm",
    ".xlsb",
)
extensions = EXTRACTABLE_EXTENSIONS
EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".xlsb"}
SUPPORTED_FILES_HINT = "(.pdf, .txt, .pptx, .ppt, .doc, .docx, .xlsx, .xls, .xlsm, .png, .jpg, .jpeg)"

TOO_LARGE_MESSAGE = "Превышен максимальный размер файла (50 МБ)"
TOO_LARGE_EXCEL_MESSAGE = "Превышен максимальный размер Excel-файла (10 МБ)"
MEMORY_ERROR_MESSAGE = (
    "Не хватило памяти на обработку файла. "
    "Загрузите папку частями или возьмите меньшие файлы."
)


class FileTooLargeError(ValueError):
    def __init__(self, message: str = TOO_LARGE_MESSAGE):
        super().__init__(message)


def extract_paths(root_path):
    """Извлекает дочернее содержимое директории."""
    all_files = []
    subfolders = []

    for dirpath, dirs, filenames in os.walk(root_path):
        for filename in filenames:
            full_path = os.path.abspath(os.path.join(dirpath, filename))
            all_files.append(full_path)
        for dirname in dirs:
            full_dirpath = os.path.abspath(os.path.join(dirpath, dirname))
            subfolders.append(full_dirpath)

    return subfolders, all_files


def list_folder_files(folder_path, extract_child_content=False):
    """Список файлов в папке. Возвращает (files, error)."""
    folder_path = os.path.abspath(os.path.expanduser((folder_path or "").strip()))
    if not folder_path:
        return [], "Вставьте путь до директории"
    if not os.path.exists(folder_path):
        return [], "Папка не существует"
    if not os.path.isdir(folder_path):
        return [], "Указанный путь не является директорией"

    if extract_child_content:
        _, files = extract_paths(folder_path)
        files = [f for f in files if f.lower().endswith(EXTRACTABLE_EXTENSIONS)]
    else:
        files = [
            os.path.abspath(str(f))
            for f in Path(folder_path).iterdir()
            if f.is_file() and f.suffix.lower() in EXTRACTABLE_EXTENSIONS
        ]
        files.sort()

    if not files:
        return [], (
            "В директории нет поддерживаемых файлов "
            + SUPPORTED_FILES_HINT
        )
    return files, None


def _pdf_text(data) -> str:
    if isinstance(data, dict):
        return "\n".join(str(v) for v in data.values() if v)
    return str(data or "")


def extract_file_text(file_path: str) -> str:
    """Достаёт текст из одного файла для пайплайна заметок."""
    if not os.path.isfile(file_path):
        raise ValueError(f"Файл не найден: {file_path}")
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if not check_size(file_path, ext.lstrip(".")):
            msg = TOO_LARGE_EXCEL_MESSAGE if ext in EXCEL_EXTENSIONS else TOO_LARGE_MESSAGE
            raise FileTooLargeError(msg)
    except FileTooLargeError:
        raise
    except OSError as e:
        raise ValueError(f"Не удалось прочитать файл: {e}") from e

    try:
        if ext == ".pdf":
            return _pdf_text(read_pdf(file_path))
        if ext == ".txt":
            return read_txt(file_path) or ""
        if ext in {".pptx", ".ppt"}:
            try:
                from app.handlers.pptx_reader_win import pptx_to_pdf
            except ImportError:
                from pptx_reader_win import pptx_to_pdf
            return _pdf_text(pptx_to_pdf(file_path))
        if ext in {".doc", ".docx"}:
            try:
                from app.handlers.word_reader_win import read_word
            except ImportError:
                from word_reader_win import read_word
            return _pdf_text(read_word(file_path))
        if ext in {".png", ".jpg", ".jpeg"}:
            try:
                from app.handlers.image_reader_win import process_image
            except ImportError:
                from image_reader_win import process_image
            return process_image(file_path) or ""
        if ext in EXCEL_EXTENSIONS:
            try:
                from app.handlers.excel_reader import read_excel
            except ImportError:
                from excel_reader import read_excel
            return read_excel(file_path) or ""
    except FileTooLargeError:
        raise
    except MemoryError as e:
        gc.collect()
        raise ValueError(f"{MEMORY_ERROR_MESSAGE} Файл: «{os.path.basename(file_path)}»") from e
    except Exception as e:
        raise ValueError(f"Не удалось извлечь текст из «{os.path.basename(file_path)}»: {e}") from e
    raise ValueError(f"Неподдерживаемый формат: {ext}")


def folder_processing(folder_path, extract_child_content=False):
    """
    Обходит файлы папки. Возвращает список словарей
    {path, name, text, error} либо строку с ошибкой.
    """
    files, err = list_folder_files(folder_path, extract_child_content=extract_child_content)
    if err:
        return err

    results = []
    for file in tqdm(files, desc="Обработка файлов"):
        item = {
            "path": file,
            "name": os.path.basename(file),
            "text": "",
            "error": None,
        }
        try:
            text = extract_file_text(file)
            if not (text or "").strip():
                item["error"] = "Текст не извлечен"
            else:
                item["text"] = text
        except FileTooLargeError as e:
            item["error"] = str(e)
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    return results
