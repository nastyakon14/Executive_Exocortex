def read_txt(file_path: str) -> str:
    """Читает текстовый файл целиком в строку."""
    with open(file_path, 'r', encoding='utf-8') as file:
        return file.read()