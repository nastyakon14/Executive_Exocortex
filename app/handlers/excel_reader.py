from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd


def read_excel(file_path):
    """Текст всех листов: отдельные таблицы, настоящие заголовки, без Unnamed."""
    book = pd.ExcelFile(file_path)
    try:
        merges = _merged_ranges(file_path)
        parts = []
        for sheet_name in book.sheet_names:
            frame = book.parse(sheet_name, header=None)
            text = _sheet_text(frame, merges.get(sheet_name) or [])
            if text:
                parts.append(f"Лист: {sheet_name}\n{text}")
        return "\n\n".join(parts)
    finally:
        book.close()


def _merged_ranges(file_path) -> dict[str, list[tuple[int, int, int, int]]]:
    """Прямоугольники объединённых ячеек. Для .xls их нет: openpyxl их не читает."""
    if not str(file_path).lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        return {}
    try:
        import openpyxl
    except ImportError:
        return {}
    found: dict[str, list[tuple[int, int, int, int]]] = {}
    workbook = openpyxl.load_workbook(file_path, read_only=False, data_only=False)
    try:
        for name in workbook.sheetnames:
            sheet = workbook[name]
            found[name] = [
                (item.min_row - 1, item.min_col - 1, item.max_row - 1, item.max_col - 1)
                for item in sheet.merged_cells.ranges
            ]
    finally:
        workbook.close()
    return found


def _sheet_text(frame: pd.DataFrame, merges: list[tuple[int, int, int, int]]) -> str:
    if frame is None or frame.empty:
        return ""
    frame = frame.copy()
    _fill_merges(frame, merges)
    grid = _grid(frame)
    blocks = _blocks(grid)
    rendered = [text for text in (_render_block(block) for block in blocks) if text]
    if len(rendered) <= 1:
        return rendered[0] if rendered else ""
    return "\n\n".join(f"Таблица {index}\n{text}" for index, text in enumerate(rendered, 1))


def _fill_merges(frame: pd.DataFrame, merges: list[tuple[int, int, int, int]]) -> None:
    height, width = frame.shape
    for top, left, bottom, right in merges:
        if top >= height or left >= width or _missing(frame.iat[top, left]):
            continue
        value = frame.iat[top, left]
        for row in range(top, min(bottom, height - 1) + 1):
            for col in range(left, min(right, width - 1) + 1):
                if _missing(frame.iat[row, col]):
                    frame.iat[row, col] = value


def _grid(frame: pd.DataFrame) -> list[list[str]]:
    grid = [[_cell(value) for value in row] for row in frame.itertuples(index=False, name=None)]
    return _trim_border(grid)


def _trim_border(grid: list[list[str]]) -> list[list[str]]:
    """Срезает пустую кромку листа. Пустой столбец внутри не трогает: это граница таблиц."""
    if not grid:
        return []
    used_rows = [index for index, row in enumerate(grid) if any(row)]
    if not used_rows:
        return []
    grid = grid[used_rows[0]:used_rows[-1] + 1]
    width = max(len(row) for row in grid)
    used_cols = [
        col for col in range(width)
        if any(col < len(row) and row[col] for row in grid)
    ]
    if not used_cols:
        return []
    left, right = used_cols[0], used_cols[-1]
    width = right - left + 1
    trimmed = []
    for row in grid:
        piece = row[left:right + 1]
        if len(piece) < width:
            piece = piece + [""] * (width - len(piece))
        trimmed.append(piece)
    return trimmed


def _missing(value) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except TypeError:
        return False


def _cell(value) -> str:
    if _missing(value):
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, datetime):
        if value.hour or value.minute or value.second:
            return value.strftime("%d.%m.%Y %H:%M")
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, float) and math.isfinite(value):
        return _number(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return " ".join(text.split())


def _number(value: float) -> str:
    rounded = round(value, 2 if abs(value) >= 1000 else 4)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _drop_empty(grid: list[list[str]]) -> list[list[str]]:
    if not grid:
        return []
    kept_rows = [row for row in grid if any(cell for cell in row)]
    if not kept_rows:
        return []
    width = max(len(row) for row in kept_rows)
    kept_cols = [col for col in range(width) if any(col < len(row) and row[col] for row in kept_rows)]
    if not kept_cols:
        return []
    return [[row[col] if col < len(row) else "" for col in kept_cols] for row in kept_rows]


def _blocks(grid: list[list[str]]) -> list[list[list[str]]]:
    """Прямоугольники, разделённые пустой строкой или пустым столбцом."""
    if not grid:
        return []
    found: list[list[list[str]]] = []
    for top, bottom in _spans(grid, by_row=True):
        band = grid[top:bottom]
        for left, right in _spans(band, by_row=False):
            piece = _drop_empty([row[left:right] for row in band])
            if not piece:
                continue
            nested = _blocks(piece) if piece != grid else []
            if nested and nested != [piece]:
                found.extend(nested)
            else:
                found.append(piece)
    return found


def _spans(grid: list[list[str]], by_row: bool) -> list[tuple[int, int]]:
    if not grid:
        return []
    limit = len(grid) if by_row else max(len(row) for row in grid)

    def empty(index: int) -> bool:
        if by_row:
            return not any(grid[index])
        return all(index >= len(row) or not row[index] for row in grid)

    spans = []
    start = None
    for index in range(limit):
        if empty(index):
            if start is not None:
                spans.append((start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        spans.append((start, limit))
    return spans


def _render_block(block: list[list[str]]) -> str:
    if not block:
        return ""
    header_rows = _header_row_count(block)
    if header_rows == 0:
        headers = [""] * len(block[0])
        body = block
    else:
        headers = _compose_headers(block[:header_rows])
        body = [row for row in block[header_rows:] if any(row)]
        body = [row for row in body if row != headers]
    if not body:
        cells = [cell for cell in headers if cell]
        return " · ".join(cells)
    if header_rows == 0 and len(body) == 1:
        cells = [cell for cell in body[0] if cell]
        return " · ".join(cells)
    return _markdown(headers, body)


def _header_row_count(block: list[list[str]]) -> int:
    """1–3 верхние текстовые строки. Групповой заголовок реже, чем строка под ним."""
    if not block:
        return 0
    if _row_kind(block[0]) != "text":
        return 0
    count = 1
    while count < min(3, len(block)):
        current = block[count - 1]
        nxt = block[count]
        kind = _row_kind(nxt)
        if kind == "data":
            break
        if kind != "text":
            break
        filled = sum(1 for cell in current if cell)
        filled_next = sum(1 for cell in nxt if cell)
        if filled < filled_next:
            count += 1
            continue
        break
    return count


def _row_kind(row: list[str]) -> str:
    values = [cell for cell in row if cell]
    if not values:
        return "empty"
    numeric = sum(1 for cell in values if _numeric_text(cell))
    if numeric >= 1 and numeric >= len(values) - numeric:
        return "data"
    return "text"


def _numeric_text(value: str) -> bool:
    text = value.replace(" ", "").replace(",", ".")
    if text.endswith("%"):
        text = text[:-1]
    if not text:
        return False
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def _compose_headers(rows: list[list[str]]) -> list[str]:
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    filled = [_spread_group_header(row) if _sparse(row) else row for row in normalized]
    headers = []
    for col in range(width):
        parts = []
        for row in filled:
            value = row[col]
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        headers.append(" / ".join(parts))
    return headers


def _sparse(row: list[str]) -> bool:
    filled = sum(1 for cell in row if cell)
    return 0 < filled < len(row) * 0.6


def _spread_group_header(row: list[str]) -> list[str]:
    """Пустые ячейки объединённого заголовка получают название группы слева."""
    carried = ""
    spread = []
    for cell in row:
        if cell:
            carried = cell
            spread.append(cell)
        else:
            spread.append(carried)
    return spread


def _markdown(headers: list[str], body: list[list[str]]) -> str:
    width = max([len(headers), *[len(row) for row in body]])
    head = headers + [""] * (width - len(headers))

    def line(cells: list[str]) -> str:
        padded = cells + [""] * (width - len(cells))
        return "| " + " | ".join(_md(cell) for cell in padded) + " |"

    rows = [
        line(head),
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    rows.extend(line(row) for row in body)
    return "\n".join(rows)


def _md(value: str) -> str:
    return (value or "").replace("|", "\\|")
