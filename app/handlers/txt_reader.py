def read_txt(file_path: str) -> str:
    """Читает текстовый файл, перебирая обычные кодировки."""
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            with open(file_path, "r", encoding=encoding) as file:
                return file.read()
        except UnicodeDecodeError as e:
            last_error = e
            continue
    if last_error:
        raise last_error
    return ""
