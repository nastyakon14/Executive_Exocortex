import speech_recognition as sr

def recognize_audio(file_path: str) -> str:
    '''транскрибация аудио файла в текст'''
    r = sr.Recognizer()
    with sr.AudioFile(file_path) as source:
        audio_data = r.record(source) # записываем аудио данные из файла
        try:
            # language='ru-RU' для русского языка
            return r.recognize_google(audio_data, language='ru-RU') # распознаем текст из аудио данных
        except sr.UnknownValueError:
            return "Не удалось распознать речь."
        except sr.RequestError: # ошибка сервиса распознавания
            return "Ошибка сервиса распознавания."
