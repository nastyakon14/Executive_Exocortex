import os
from pathlib import Path

try:
    from telegram_bot.handlers.txt_reader import read_txt
    from telegram_bot.handlers.pdf_reader import read_pdf
except ImportError:
    from txt_reader import read_txt
    from pdf_reader import read_pdf

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

# Читалки есть только для txt/pdf; остальные расширения пропускаются.
EXTRACTABLE_EXTENSIONS = (".txt", ".pdf")
extensions = EXTRACTABLE_EXTENSIONS


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
        return [], "В директории нет файлов .pdf или .txt"
    return files, None


def extract_file_text(file_path: str) -> str:
    """Достаёт текст из одного файла для пайплайна заметок."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        data = read_pdf(file_path)
        if isinstance(data, dict):
            return "\n".join(str(v) for v in data.values() if v)
        return str(data or "")
    if ext == ".txt":
        return read_txt(file_path) or ""
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
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    return results
