"""Презентации на Windows: PowerPoint COM (pptxtopdf) → PDF → тот же PDF-разбор."""
from __future__ import annotations

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


def pptx_to_pdf(input_path, output_folder="convert_pdf"):
    """`.ppt` и `.pptx` через PowerPoint, затем текст из PDF. Имя функции историческое."""
    from pptxtopdf import convert

    with tempfile.TemporaryDirectory(prefix="pptx_pdf_") as tmp:
        convert(input_path, tmp)
        pdfs = list(Path(tmp).glob("*.pdf"))
        if not pdfs:
            stem = Path(input_path).stem
            named = Path(tmp) / f"{stem}.pdf"
            if named.is_file():
                pdfs = [named]
        if not pdfs:
            raise RuntimeError("PowerPoint не создал PDF")
        text = _pages_to_text(read_pdf(str(pdfs[0])))
        if not (text or "").strip():
            raise RuntimeError("После конвертации презентации в PDF текст пустой")
        return text
