# проверяем размер файла
# если превышает, то скипаем и не обрабатываем
# выводим сообщение пользователю, что файл слишком большой
import os

def check_size(file_path,
    extension,
    max_size=50*1024*1024
    ): # 50MB, 50к кб

    if extension in ['xlsx', 'xls','xlsm','xlsb']:
        # для excel файлов максимальный размер 10MB, 10к кб, чтобы не грузить загроможденные таблицы
        max_size = 10*1024*1024 # 10MB, 10к кб

    # превышает маскимально допустимый размер
    if os.path.getsize(file_path) > max_size:
        # не обрабатываем
        return False
    else:
        # обрабатываем
        return True
