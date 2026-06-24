def read_txt(file_path: str) -> str:
    '''чтение текстового файла'''
    with open(file_path, 'r') as file:
        return file.read()