"""Табличный граф для длинных листов Excel: без атомайзера и линкера."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable

from app.handlers.excel_reader import ExcelTable, _skip_value
from storage.neo4j.repository import ZettelRepository


def ingest_excel_tables(
    repository: ZettelRepository,
    embedding_model,
    user_id: str,
    source_input: str,
    tables: list[ExcelTable],
    on_stage: Callable | None = None,
) -> tuple[bool, str]:
    if not tables:
        return True, ""
    file_name = Path(source_input or "").name or source_input or "Excel"
    created = 0
    for index, table in enumerate(tables, 1):
        if on_stage:
            on_stage(
                "link",
                "Табличный граф Excel",
                f"Лист «{table.sheet_name}», {index} из {len(tables)}",
            )
        created += _ingest_sheet(
            repository,
            embedding_model,
            user_id,
            source_input,
            file_name,
            table,
        )
    return True, f"✅ Excel: в граф записано {created} узлов табличной модели."


def _ingest_sheet(
    repository: ZettelRepository,
    embedding_model,
    user_id: str,
    source_input: str,
    file_name: str,
    table: ExcelTable,
) -> int:
    root_no = repository.get_max_root_id(user_id) + 1
    sheet_id = str(uuid.uuid4())
    items: list[dict] = []
    child_links: list[dict] = []
    related_links: list[dict] = []
    entity_nodes: list[tuple[str, list[str]]] = []

    cat_headers = [
        header for header, kind in zip(table.headers, table.kinds)
        if kind == "category"
    ]
    kind_label = "CSV" if (source_input or "").lower().endswith(".csv") else "Excel"
    kind_tag = "csv" if kind_label == "CSV" else "excel"
    sheet_content = (
        f"Таблица {kind_label} «{table.sheet_name}» из файла «{file_name}». "
        f"Строк: {len(table.rows)}. "
        f"Столбцы: {', '.join(table.headers)}."
    )
    items.append(_node(
        sheet_id,
        str(root_no),
        table.sheet_name,
        sheet_content,
        "excel_sheet",
        [kind_tag, table.sheet_name],
        True,
        source_input,
        sheet_content,
    ))
    entity_nodes.append((sheet_id, [kind_tag, table.sheet_name]))

    column_ids: dict[str, str] = {}
    value_ids: dict[tuple[str, str], str] = {}
    col_index = 0
    for header, kind in zip(table.headers, table.kinds):
        if kind != "category":
            continue
        col_index += 1
        column_id = str(uuid.uuid4())
        column_ids[header] = column_id
        unique_values = table.values_by_column.get(header) or []
        content = (
            f"Столбец «{header}» листа «{table.sheet_name}». "
            f"Уникальные значения: {', '.join(unique_values[:40])}"
            f"{'…' if len(unique_values) > 40 else ''}."
        )
        items.append(_node(
            column_id,
            f"{root_no}.{col_index}",
            header,
            content,
            "excel_column",
            [header],
            False,
            source_input,
            header,
        ))
        child_links.append({"child": column_id, "parent": sheet_id})
        entity_nodes.append((column_id, [header]))
        for val_index, value in enumerate(unique_values, 1):
            value_id = str(uuid.uuid4())
            value_ids[(header, value.casefold())] = value_id
            value_content = (
                f"Значение столбца «{header}»: {value}. "
                f"Лист «{table.sheet_name}»."
            )
            items.append(_node(
                value_id,
                f"{root_no}.{col_index}.{val_index}",
                value,
                value_content,
                "excel_value",
                [header, value],
                False,
                source_input,
                value,
            ))
            child_links.append({"child": value_id, "parent": column_id})
            entity_nodes.append((value_id, [header, value]))

    id_index = next((i for i, kind in enumerate(table.kinds) if kind == "id"), 0)
    name_index = next(
        (
            i for i, (header, kind) in enumerate(zip(table.headers, table.kinds))
            if kind != "skip" and i != id_index and "назван" in header.casefold()
        ),
        None,
    )
    for row_no, row in enumerate(table.rows, 1):
        row_id = str(uuid.uuid4())
        title = _row_title(table.headers, row, id_index, name_index, row_no)
        content = _row_content(table.sheet_name, table.headers, table.kinds, row, title, kind_label)
        items.append(_node(
            row_id,
            f"{root_no}.r{row_no}",
            title[:80],
            content,
            "excel_row",
            [],
            False,
            source_input,
            content[:500],
        ))
        child_links.append({"child": row_id, "parent": sheet_id})
        for header, kind, cell in zip(table.headers, table.kinds, row):
            if kind != "category" or _skip_value(cell):
                continue
            value_id = value_ids.get((header, cell.casefold()))
            if value_id:
                related_links.append({"left": row_id, "right": value_id})

    texts = [item["content"] for item in items]
    vectors = embedding_model.embed_passages_batch(texts)
    for item, vector in zip(items, vectors):
        item["embedding"] = vector
        item["source_input"] = source_input

    repository.create_zettels_batch(user_id, items)
    repository.create_child_links(user_id, child_links)
    repository.create_related_links(user_id, related_links)
    for zettel_id, tags in entity_nodes:
        repository._create_entity_links(user_id, zettel_id, tags)
    return len(items)


def _node(
    zettel_id: str,
    luhmann_id: str,
    topic: str,
    content: str,
    thought_type: str,
    tags: list[str],
    is_root: bool,
    source_input: str,
    source_quote: str,
) -> dict:
    return {
        "zettel_id": zettel_id,
        "luhmann_id": luhmann_id,
        "topic": topic,
        "content": content,
        "thought_type": thought_type,
        "tags": tags,
        "is_root_topic": is_root,
        "source_input": source_input,
        "source_quote": source_quote,
        "embedding": [],
    }


def _row_title(headers, row, id_index: int, name_index: int | None, row_no: int) -> str:
    key = row[id_index] if id_index < len(row) and not _skip_value(row[id_index]) else ""
    name = ""
    if name_index is not None and name_index < len(row) and not _skip_value(row[name_index]):
        name = row[name_index]
    if key and name:
        return f"{key} · {name}"
    if key:
        return key
    if name:
        return name
    for cell in row:
        if not _skip_value(cell):
            return cell
    return f"Строка {row_no}"


def _row_content(
    sheet_name: str,
    headers: list[str],
    kinds: list[str],
    row: list[str],
    title: str,
    kind_label: str = "Excel",
) -> str:
    lines = [f"Строка {kind_label} «{title}» в таблице «{sheet_name}»."]
    for header, kind, cell in zip(headers, kinds, row):
        if kind == "skip" or _skip_value(cell):
            continue
        lines.append(f"{header}: {cell}")
    return "\n".join(lines)
