import speech_recognition as sr

def recognize_audio(file_path: str) -> str:
    """Транскрибация wav-файла в текст через google asr."""
    r = sr.Recognizer()
    with sr.AudioFile(file_path) as source:
        audio_data = r.record(source)
        try:
            # ru-RU для русскоязычных голосовых заметок
            return r.recognize_google(audio_data, language='ru-RU')
        except sr.UnknownValueError:
            return "Не удалось распознать речь."
        except sr.RequestError:
            return "Ошибка сервиса распознавания."
