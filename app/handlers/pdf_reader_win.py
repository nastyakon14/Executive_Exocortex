"""PDF на Windows: встроенный текст, иначе tesseract.exe с корпоративного пути."""
from __future__ import annotations
import os
import re
import subprocess

import pandas as pd
import pdfplumber
import pytesseract


def get_home_dir():
    return r"\\0001fsrvau01\fs_analytics_unit"


home_dir = get_home_dir()
TESSERACT_DIR = os.path.join(
    home_dir, "Projects", "2026", "34. IDP opensource", "3. Data processing", "Tesseract-OCR"
)
TESSERACT_EXE = os.path.join(TESSERACT_DIR, "tesseract.exe")
TESSDATA_DIR = os.path.join(TESSERACT_DIR, "tessdata")

pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE
os.environ["TESSDATA_PREFIX"] = TESSDATA_DIR
os.environ["PATH"] = TESSERACT_DIR + os.pathsep + os.environ.get("PATH", "")

result = subprocess.run(
    [TESSERACT_EXE, "--list-langs"],
    capture_output=True,
    text=True,
)
print("Доступные языки:", result.stdout)


def cleaning_text(text):
    """Готовит извлечённый текст к модели: убирает мусор, оставляет блоки и списки."""
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n").replace("\u00a0", " ")
    text = _normalize_glyphs(text)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"[●◦▪▸►‣⁃·∙■□○]", "•", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    kept = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]{2,}", " ", raw).strip()
        if not line:
            kept.append("")
            continue
        line = re.sub(r"(?:•\s*){2,}", "• ", line)
        if line.startswith("•") and not line.startswith("• "):
            line = "• " + line[1:].lstrip()
        if _is_noise_line(line):
            continue
        kept.append(line)

    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_noise_line(line: str) -> bool:
    """Строка без букв и цифр: голые маркеры, пустая сетка, «None»."""
    if re.fullmatch(r"\|?(?:\s*:?-{3,}:?\s*\|)+[\s:\-|]*", line):
        return False
    probe = re.sub(r"\b(?:none|null|nan)\b", "", line, flags=re.IGNORECASE)
    return re.search(r"[0-9A-Za-zА-Яа-яЁё]", probe) is None


def _normalize_glyphs(text: str) -> str:
    """Маркеры Wingdings/Symbol приходят как \\uf0a7 и похожие символы частной зоны."""
    return re.sub(r"[\uE000-\uF8FF]", "•", text or "")


def _meaningful_len(text: str) -> int:
    """Сколько в тексте настоящих букв и цифр, без маркеров и пунктуации."""
    cleaned = _normalize_glyphs(text or "")
    cleaned = re.sub(r"[^\w]", "", cleaned, flags=re.UNICODE)
    return len(cleaned)


def _overlap_len(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _inside_box(word, boxes) -> bool:
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["top"] + word["bottom"]) / 2
    for x0, top, x1, bottom in boxes:
        if x0 <= cx <= x1 and top <= cy <= bottom:
            return True
    return False


def _table_bboxes(page) -> list:
    boxes = []
    try:
        for table in page.find_tables() or []:
            bbox = getattr(table, "bbox", None)
            if bbox and len(bbox) == 4:
                boxes.append(tuple(bbox))
    except Exception:
        return []
    return boxes


_MARKER_RE = re.compile(r"^[\s•●◦▪▸►‣⁃–—\-·∙\*■□\uE000-\uF8FF]+$")


def _is_marker_text(text: str) -> bool:
    token = (text or "").strip()
    return bool(token) and len(token) <= 3 and bool(_MARKER_RE.match(token))


def _cell_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "none":
        return ""
    return text


def _table_markdown(table) -> str:
    """Markdown только для настоящей таблицы. Пустая сетка слайда не считается таблицей."""
    if not table or len(table) < 2:
        return ""
    rows = [[_cell_text(cell) for cell in (row or [])] for row in table]
    width = max((len(row) for row in rows), default=0)
    if width < 2:
        return ""
    rows = [row + [""] * (width - len(row)) for row in rows]
    filled = [cell for row in rows for cell in row if cell]
    total = width * len(rows)
    if len(filled) < 3 or len(filled) / total < 0.2:
        return ""
    header, body = rows[0], rows[1:]
    if not any(header):
        header = [f"col{i + 1}" for i in range(width)]
        body = rows
    if not body:
        return ""
    try:
        return pd.DataFrame(body, columns=header).to_markdown(index=False)
    except Exception:
        return ""


def _useful_tables(page) -> list[tuple[tuple | None, str]]:
    found = []
    try:
        detected = page.find_tables() or []
    except Exception:
        return []
    for table in detected:
        try:
            grid = table.extract()
        except Exception:
            continue
        markdown = _table_markdown(grid)
        if not markdown:
            continue
        bbox = getattr(table, "bbox", None)
        found.append((tuple(bbox) if bbox and len(bbox) == 4 else None, markdown))
    return found


def _line_segments(words: list[dict]) -> list[dict]:
    """Слова одной высоты — одна строка. Большой горизонтальный зазор режет строку на блоки."""
    heights = [max(1.0, w["bottom"] - w["top"]) for w in words]
    median_h = sorted(heights)[len(heights) // 2]
    y_tol = max(2.0, median_h * 0.55)
    gap_limit = max(median_h * 1.35, 14.0)

    rows = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(word["top"] - rows[-1]["top"]) <= y_tol:
            row = rows[-1]
            row["words"].append(word)
            row["top"] = min(row["top"], word["top"])
            row["bottom"] = max(row["bottom"], word["bottom"])
        else:
            rows.append({
                "words": [word],
                "top": word["top"],
                "bottom": word["bottom"],
            })

    segments = []
    for row in rows:
        ordered = sorted(row["words"], key=lambda w: w["x0"])
        current = [ordered[0]]
        for word in ordered[1:]:
            gap = word["x0"] - current[-1]["x1"]
            marker = _is_marker_text(" ".join(item["text"] for item in current))
            if gap > gap_limit and not marker:
                segments.append(_make_segment(current, median_h))
                current = [word]
            else:
                current.append(word)
        segments.append(_make_segment(current, median_h))
    return segments


def _make_segment(words: list[dict], median_h: float) -> dict:
    return {
        "words": words,
        "x0": min(w["x0"] for w in words),
        "x1": max(w["x1"] for w in words),
        "top": min(w["top"] for w in words),
        "bottom": max(w["bottom"] for w in words),
        "median_h": median_h,
        "text": " ".join(w["text"] for w in words if w.get("text")),
    }


def _projection_gaps(intervals: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float, float]]:
    merged = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if not merged or start > merged[-1][1] + 0.4:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    gaps = []
    for left, right in zip(merged, merged[1:]):
        width = right[0] - left[1]
        if width >= min_gap:
            gaps.append((left[1], right[0], width))
    return gaps


def _split_regions(segments: list[dict], gap: tuple[float, float, float], axis: str) -> tuple[list[dict], list[dict]]:
    mid = (gap[0] + gap[1]) / 2
    if axis == "x":
        left = [seg for seg in segments if (seg["x0"] + seg["x1"]) / 2 < mid]
        right = [seg for seg in segments if (seg["x0"] + seg["x1"]) / 2 >= mid]
        return left, right
    top = [seg for seg in segments if (seg["top"] + seg["bottom"]) / 2 < mid]
    bottom = [seg for seg in segments if (seg["top"] + seg["bottom"]) / 2 >= mid]
    return top, bottom


def _read_region(segments: list[dict], min_gap: float) -> list[str]:
    """
    Режет область по пустому месту.
    Сначала горизонтальный разрез: верх, потом низ.
    Если его нет — вертикальный: левый блок целиком, потом правый.
    """
    if not segments:
        return []
    if len(segments) == 1:
        return [segments[0]["text"]]

    y_gaps = _projection_gaps([(seg["top"], seg["bottom"]) for seg in segments], min_gap)
    x_gaps = _projection_gaps([(seg["x0"], seg["x1"]) for seg in segments], min_gap)
    best_y = max(y_gaps, key=lambda gap: gap[2], default=None)
    best_x = max(x_gaps, key=lambda gap: gap[2], default=None)

    if best_y:
        top, bottom = _split_regions(segments, best_y, "y")
        if top and bottom and len(top) < len(segments) and len(bottom) < len(segments):
            return _read_region(top, min_gap) + _read_region(bottom, min_gap)
    if best_x:
        left, right = _split_regions(segments, best_x, "x")
        if left and right and len(left) < len(segments) and len(right) < len(segments):
            return _read_region(left, min_gap) + _read_region(right, min_gap)

    lines = []
    for seg in sorted(segments, key=lambda item: (item["top"], item["x0"])):
        if lines and abs(seg["top"] - lines[-1][0]) <= max(seg["median_h"] * 0.6, 2):
            lines[-1] = (min(lines[-1][0], seg["top"]), f"{lines[-1][1]} {seg['text']}".strip())
        else:
            lines.append((seg["top"], seg["text"]))
    return ["\n".join(text for _, text in lines)]


def extract_text_by_blocks(words: list[dict]) -> str:
    """Собирает текст по визуальным блокам, а не по общей строке страницы."""
    words = [word for word in words if (word.get("text") or "").strip()]
    if not words:
        return ""
    segments = [seg for seg in _line_segments(words) if seg["text"].strip()]
    if not segments:
        return ""
    median_h = segments[0]["median_h"]
    min_gap = max(median_h * 0.9, 10.0)
    parts = [part for part in _read_region(segments, min_gap) if part.strip()]
    return cleaning_text("\n\n".join(parts))


def _native_words(page, table_boxes: list) -> list[dict]:
    try:
        words = page.extract_words(
            x_tolerance=2,
            y_tolerance=2,
            keep_blank_chars=False,
            use_text_flow=False,
        ) or []
    except Exception:
        return []
    return [word for word in words if not _inside_box(word, table_boxes)]


def _ocr_words(image) -> list[dict]:
    data = pytesseract.image_to_data(image, lang="rus+eng", output_type=pytesseract.Output.DICT)
    words = []
    for text, conf, left, top, width, height in zip(
        data.get("text") or [],
        data.get("conf") or [],
        data.get("left") or [],
        data.get("top") or [],
        data.get("width") or [],
        data.get("height") or [],
    ):
        token = (text or "").strip()
        if not token or not width or not height:
            continue
        try:
            score = float(conf)
        except (TypeError, ValueError):
            score = -1
        if score >= 0 and score < 20:
            continue
        words.append({
            "text": token,
            "x0": float(left),
            "x1": float(left) + float(width),
            "top": float(top),
            "bottom": float(top) + float(height),
        })
    return words


def extract_text_pdf2image(page):
    """OCR с теми же блоками: колонки слайда не склеиваются в одну строку."""
    image = None
    try:
        image = page.to_image(resolution=150).original
        text = extract_text_by_blocks(_ocr_words(image))
    except MemoryError:
        text = ""
    except Exception:
        text = ""
    finally:
        if image is not None:
            try:
                image.close()
            except Exception:
                pass
        try:
            page.flush_cache()
        except Exception:
            pass
    return text


def extract_text_native(page, table_boxes: list | None = None) -> str:
    """Текст PDF по блокам. Слова внутри таблиц не дублируются: таблицы добавляются отдельно."""
    boxes = table_boxes if table_boxes is not None else _table_bboxes(page)
    try:
        text = extract_text_by_blocks(_native_words(page, boxes))
        if text:
            return text
        return cleaning_text((page.extract_text() or "").strip())
    except Exception:
        return ""


def extract_tables(file_path):
    """Извлекает только непустые таблицы из pdf и возвращает их в markdown по номерам страниц."""
    all_tables = {}
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            chunks = [markdown for _, markdown in _useful_tables(page)]
            if chunks:
                all_tables[page.page_number] = "\n\n".join(chunks)
            try:
                page.flush_cache()
            except Exception:
                pass
    return all_tables


def tables_to_pages(tables, page_text_dict):
    """Добавляет markdown-таблицы к тексту соответствующих страниц."""
    for page_num, table in tables.items():
        idx = page_num - 1
        page_text_dict[idx] = (page_text_dict.get(idx) or "") + f"\n\n{table}"
    return page_text_dict


def read_pdf(file_path):
    """
    Читает pdf: сначала встроенный текст, иначе OCR, плюс таблицы.
    Возвращает словарь {номер_страницы: текст}.
    """
    page_text_dict = {}

    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            useful = _useful_tables(page)
            table_boxes = [bbox for bbox, _ in useful if bbox]
            text = extract_text_native(page, table_boxes)
            if _meaningful_len(text) < 20:
                ocr = extract_text_pdf2image(page)
                if _meaningful_len(ocr) > _meaningful_len(text):
                    text = ocr
            for _, markdown in useful:
                text = (text or "") + f"\n\n{markdown}"
            page_text_dict[page_num] = cleaning_text(text)
            try:
                page.flush_cache()
            except Exception:
                pass

    return page_text_dict
