"""Конвертация Word → текст на Windows: Word COM → PDF → тот же PDF-разбор, что на Mac."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

try:
    from app.handlers.pdf_reader_win import read_pdf
except ImportError:
    from pdf_reader_win import read_pdf


def _pages_to_text(data) -> str:
    if isinstance(data, dict):
        return "\n".join(str(v) for v in data.values() if v)
    return str(data or "")


def convert_to_pdf(input_file, output_file=None):
    import win32com.client

    input_file = os.path.abspath(input_file)
    if output_file is None:
        output_file = os.path.splitext(input_file)[0] + ".pdf"
    output_file = os.path.abspath(output_file)

    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = False
    word.AutomationSecurity = 3
    try:
        doc = word.Documents.Open(
            input_file,
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            PasswordDocument="",
            Revert=True,
        )
        doc.SaveAs(output_file, FileFormat=17)
        doc.Close(SaveChanges=False)
        return output_file
    except Exception as e:
        raise RuntimeError(f"Ошибка конвертации Word: {e}") from e
    finally:
        word.Quit(SaveChanges=False)


def read_word(input_path):
    """`.doc` и `.docx` через Microsoft Word, затем извлечение текста из PDF."""
    with tempfile.TemporaryDirectory(prefix="word_pdf_") as tmp:
        output_path = os.path.join(tmp, Path(input_path).stem + ".pdf")
        converted = convert_to_pdf(input_path, output_path)
        text = _pages_to_text(read_pdf(converted))
        if not (text or "").strip():
            raise RuntimeError("После конвертации Word в PDF текст пустой")
        return text
