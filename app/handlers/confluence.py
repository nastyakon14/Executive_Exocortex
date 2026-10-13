# добавить инфу по ссылке со страницы confluence
from __future__ import annotations 
from atlassian import Confluence
from atlassian.errors import ApiError, ApiPermissionError
from requests.exceptions import HTTPError
import re
from urllib.parse import unquote
from bs4 import BeautifulSoup
import os
import requests
import tempfile
import warnings
warnings.filterwarnings("ignore")
from dotenv import load_dotenv

load_dotenv()

CONFLUENCE_DOMAIN = os.getenv("CONFLUENCE_DOMAIN", "mts")
CONFLUENCE_HOST = os.getenv("CONFLUENCE_HOST", f"confluence.{CONFLUENCE_DOMAIN}.ru")
CONFLUENCE_URL = os.getenv("CONFLUENCE_URL", f"https://{CONFLUENCE_HOST}")

_confluence = None


def connect_Confluence():
    login =os.getenv("CONFLUENCE_LOGIN")
    pswd = os.getenv("CONFLUENCE_PASSWORD")
    os.environ["NO_PROXY"] = CONFLUENCE_URL
    return Confluence(
        url=CONFLUENCE_URL,
        username=login,
        password=pswd,
        verify_ssl=False,
        timeout=30,
    )


def get_confluence():
    global _confluence
    if _confluence is None:
        _confluence = connect_Confluence()
    return _confluence


def extract_page_info_from_url(url):
    """Парсит URL-ссылку Confluence для извлечения ID страницы или Space Key + Title."""
    decoded_url = unquote(url).replace("+", " ")

    # Попытка 1: Ищем Page ID
    page_id_match = re.search(r"/pages/(\d+)|pageId=(\d+)", decoded_url)
    if page_id_match:
        page_id = page_id_match.group(1) or page_id_match.group(2)
        return {"type": "id", "value": page_id}

    # Попытка 2: Ищем Space Key и Title
    space_title_match = re.search(r"/display/([^/]+)/([^?#\s]+)", decoded_url)
    if space_title_match:
        space = space_title_match.group(1)
        title = space_title_match.group(2).strip("/")
        return {"type": "space_title", "space": space, "title": title}

    raise ValueError(
        "Не удалось распознать формат ссылки Confluence. Проверьте URL."
    )


def _version_stamp(page: dict) -> tuple:
    from datetime import datetime, timezone

    if not page:
        raise RuntimeError("Страница Confluence не найдена")
    when = ((page.get("version") or {}).get("when") or "").strip()
    if not when:
        raise RuntimeError("У страницы нет даты версии")
    dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    title = (page.get("title") or "").strip()
    return dt.astimezone(timezone.utc), title


def get_confluence_page_version_info(url: str) -> tuple:
    """Дата последней правки и заголовок страницы."""
    info = extract_page_info_from_url(url)
    if info["type"] == "id":
        page = get_confluence().get_page_by_id(info["value"], expand="version")
    else:
        page = get_confluence().get_page_by_title(
            space=info["space"], title=info["title"], expand="version"
        )
    return _version_stamp(page)


def get_confluence_page_version_when(url):
    """Дата последней правки страницы Confluence (UTC) или None."""
    return get_confluence_page_version_info(url)[0]


def clean_text(text: str) -> str:
    """Очистка текста от лишних символов и переносов."""
    if not text:
        return ""
    
    # Убираем множественные пробелы
    text = re.sub(r'[ \t]+', ' ', text)
    
    # Убираем множественные переносы строк (оставляем максимум 2)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # Убираем пробелы в начале и конце строк
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    
    # Убираем пустые строки в начале и конце
    text = text.strip()
    
    return text


def extract_tables_from_soup(soup: BeautifulSoup) -> str:
    """Извлекает таблицы из HTML и форматирует их в читаемый текст."""
    tables_text = []
    
    for table in soup.find_all('table'):
        table_lines = []
        
        # Заголовки таблицы
        headers = []
        for th in table.find_all('th'):
            headers.append(th.get_text(strip=True))
        
        if headers:
            table_lines.append(' | '.join(headers))
            table_lines.append('-' * (len(' | '.join(headers))))
        
        # Строки таблицы
        for row in table.find_all('tr'):
            cells = []
            for cell in row.find_all(['td', 'th']):
                cell_text = cell.get_text(strip=True)
                cells.append(cell_text)
            
            if cells:
                table_lines.append(' | '.join(cells))
        
        if table_lines:
            tables_text.append('\n'.join(table_lines))
    
    return '\n\n'.join(tables_text) if tables_text else ""


def parse_confluence_page(page_id: str) -> dict:
    """Парсит одну страницу Confluence."""
    try:
        page = get_confluence().get_page_by_id(
            page_id, 
            expand="body.storage,version,space"
        )
        
        if not page:
            return {
                "id": page_id,
                "title": "Недоступно",
                "content": "Отсутствует доступ к странице.",
                "error": True
            }
        
        title = page.get("title", "Без названия")
        xhtml_body = page.get("body", {}).get("storage", {}).get("value", "")
        
        # Парсим HTML
        soup = BeautifulSoup(xhtml_body, "html.parser")
        
        # Извлекаем таблицы отдельно
        tables_content = extract_tables_from_soup(soup)
        
        # Извлекаем весь текст
        clean_content = soup.get_text(separator="\n")
        clean_content = clean_text(clean_content)
        
        # Объединяем текст и таблицы
        full_content = clean_content
        if tables_content:
            full_content += f"\n\n{'='*50}\nТАБЛИЦЫ:\n{'='*50}\n\n{tables_content}"
        attachments_content = attachments_text(page_id)
        if attachments_content:
            full_content += (
                f"\n\n{'='*50}\nВЛОЖЕНИЯ:\n{'='*50}\n\n{attachments_content}"
            )
        
        return {
            "id": page_id,
            "title": title,
            "content": full_content,
            "space": page.get("space", {}).get("key", ""),
            "error": False
        }
        
    except (ApiError, ApiPermissionError, HTTPError):
        return {
            "id": page_id,
            "title": "Недоступно",
            "content": "Отсутствует доступ к странице.",
            "error": True
        }
    except Exception as e:
        return {
            "id": page_id,
            "title": "Ошибка",
            "content": f"Произошла ошибка: {e}",
            "error": True
        }


def _as_page_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, dict):
        return list(raw.get("results") or [])
    if isinstance(raw, list):
        return raw
    return []


_SHORT_PAGE = re.compile(r"^(https?://[^/?#]+)/pages/(\d+)/?$", re.I)
_VIEWPAGE = re.compile(
    r"^(https?://[^/?#]+)/pages/viewpage\.action\?pageId=(\d+)",
    re.I,
)
_BARE_PAGE_ID = re.compile(r"^\d{6,}$")
_SHORT_IN_TEXT = re.compile(r"https?://[^/\s\"']+/pages/\d+/?", re.I)


def _origin(url: str) -> str:
    match = re.match(r"^(https?://[^/?#]+)", (url or "").strip(), re.I)
    if match:
        return match.group(1)
    return CONFLUENCE_URL.rstrip("/")


def confluence_page_url(page_id: str, base_url: str = "") -> str:
    """Открываемая ссылка на страницу Confluence Server."""
    return f"{_origin(base_url)}/pages/viewpage.action?pageId={page_id}"


def public_confluence_url(url: str) -> str:
    """Ссылка как ввёл пользователь. Короткий /pages/<id> приводим к viewpage.action."""
    text = (url or "").strip()
    short = _SHORT_PAGE.match(text)
    if short:
        return f"{short.group(1)}/pages/viewpage.action?pageId={short.group(2)}"
    return text


def display_confluence_source(url: str) -> str:
    """Ссылка для показа: полный viewpage.action, не голый id."""
    text = public_confluence_url(url)
    if _BARE_PAGE_ID.match(text):
        return confluence_page_url(text)
    return text


def legacy_short_confluence_url(url: str) -> str:
    """Старый короткий вид, если карточки ещё лежат как /pages/<id>."""
    text = (url or "").strip()
    match = _VIEWPAGE.match(text)
    if match:
        return f"{match.group(1)}/pages/{match.group(2)}"
    return ""


def child_page_source(page_id: str, parent_url: str = "") -> str:
    """Стабильная ссылка дочерней страницы: тот же хост, что ввёл пользователь."""
    return confluence_page_url(page_id, parent_url)


def is_confluence_fetch_error(text: str) -> bool:
    t = (text or "").strip()
    return (
        not t
        or t.startswith("Ошибка URL:")
        or t.startswith("Отсутствует доступ")
        or t.startswith("Произошла ошибка")
        or t.startswith("Страница не найдена")
    )


def resolve_page_id(url: str) -> str:
    info = extract_page_info_from_url(url)
    if info["type"] == "id":
        return str(info["value"])
    page = get_confluence().get_page_by_title(
        space=info["space"], title=info["title"], expand="version"
    )
    if not page:
        raise RuntimeError("Страница Confluence не найдена")
    return str(page.get("id") or "")


def get_page_version_info_by_id(page_id: str) -> tuple:
    """Дата последней правки и заголовок страницы по её id."""
    page = get_confluence().get_page_by_id(str(page_id), expand="version")
    return _version_stamp(page)


def get_page_version_when_by_id(page_id: str):
    """Дата последней правки страницы по её id."""
    return get_page_version_info_by_id(page_id)[0]


def page_plain_text(page_id: str) -> str:
    """Текст одной страницы в том виде, по которому считается хэш."""
    parsed = parse_confluence_page(str(page_id))
    if parsed.get("error"):
        content = (parsed.get("content") or "").strip()
        if content.startswith("Отсутствует"):
            return "Отсутствует доступ к странице (или страница не существует)."
        if content.startswith("Произошла ошибка"):
            return f"Произошла ошибка при загрузке страницы: {content}"
        return content or "Произошла ошибка при загрузке страницы"
    title = parsed.get("title") or "Без названия"
    return f"{title}:\n\n{parsed.get('content') or ''}"


def get_child_pages(page_id: str, max_depth: int = 5, current_depth: int = 0, seen: set | None = None) -> list:
    """Рекурсивно получает id всех дочерних страниц."""
    if current_depth >= max_depth:
        return []
    seen = seen if seen is not None else set()
    child_ids = []
    start = 0
    limit = 50
    try:
        while True:
            raw = get_confluence().get_page_child_by_type(
                page_id,
                type="page",
                start=start,
                limit=limit,
            )
            batch = _as_page_list(raw)
            if not batch:
                break
            for child in batch:
                child_id = str(child.get("id") or "")
                if not child_id or child_id in seen:
                    continue
                seen.add(child_id)
                child_ids.append(child_id)
                child_ids.extend(get_child_pages(child_id, max_depth, current_depth + 1, seen))
            if len(batch) < limit:
                break
            start += limit
        return child_ids
    except Exception as e:
        print(f"Ошибка при получении дочерних страниц для {page_id}: {e}")
        return child_ids


def list_confluence_pages(url: str, extract_child: bool = False, max_depth: int = 5) -> tuple[list[dict], str | None]:
    """Родительская страница и, если включено, все вложенные. Ключ источника стабильный."""
    try:
        page_id = resolve_page_id(url)
    except ValueError as e:
        return [], f"Ошибка URL: {e}"
    except Exception as e:
        return [], f"Произошла ошибка при загрузке страницы: {e}"
    if not page_id:
        return [], "Страница Confluence не найдена"
    parent_source = public_confluence_url(url.strip())
    pages = [{
        "page_id": page_id,
        "source_input": parent_source,
        "is_parent": True,
    }]
    if extract_child:
        for child_id in get_child_pages(page_id, max_depth=max_depth, seen={page_id}):
            pages.append({
                "page_id": child_id,
                "source_input": child_page_source(child_id, parent_source),
                "is_parent": False,
            })
    return pages, None


def _direct_children(page_id: str, seen: set) -> list[dict]:
    found = []
    start = 0
    limit = 50
    while True:
        raw = get_confluence().get_page_child_by_type(
            page_id, type="page", start=start, limit=limit,
        )
        batch = _as_page_list(raw)
        if not batch:
            break
        for child in batch:
            child_id = str(child.get("id") or "")
            if not child_id or child_id in seen:
                continue
            seen.add(child_id)
            found.append({
                "page_id": child_id,
                "title": child.get("title") or child_id,
            })
        if len(batch) < limit:
            break
        start += limit
    return found


def confluence_page_tree(url: str, with_children: bool = False, max_depth: int = 5) -> tuple[dict | None, str | None]:
    """Дерево страницы и вложений: заголовок и ссылка, без текста страницы."""
    try:
        page_id = resolve_page_id(url)
        page = get_confluence().get_page_by_id(page_id, expand="version")
    except Exception as e:
        return None, str(e)
    if not page_id:
        return None, "Страница Confluence не найдена"
    title = (page or {}).get("title") or url

    def walk(parent_id: str, depth: int, seen: set) -> list[dict]:
        if not with_children or depth >= max_depth:
            return []
        nodes = []
        try:
            kids = _direct_children(parent_id, seen)
        except Exception as e:
            print(f"confluence tree warning: {e}")
            return nodes
        for child in kids:
            source = child_page_source(child["page_id"], url)
            nodes.append({
                "source_input": source,
                "title": child["title"],
                "children": walk(child["page_id"], depth + 1, seen),
            })
        nodes.sort(key=lambda item: (item.get("title") or "").lower())
        return nodes

    return {
        "source_input": public_confluence_url(url.strip()),
        "title": title,
        "children": walk(page_id, 0, {page_id}),
    }, None


def get_confluence_page_content(
    url: str, 
    extract_child: bool = False,
    max_depth: int = 5
) -> str:
    """
    Получает содержимое страницы Confluence.
    
    Args:
        url: URL страницы Confluence
        extract_child: Если True, извлекает все дочерние страницы
        max_depth: Максимальная глубина рекурсии для дочерних страниц
    
    Returns:
        Текстовое содержимое страницы (и дочерних, если extract_child=True)
    """
    try:
        info = extract_page_info_from_url(url)
    except ValueError as e:
        return f"Ошибка URL: {e}"

    try:
        # Получаем основную страницу
        if info["type"] == "id":
            page_id = info["value"]
        else:
            # Получаем ID страницы по space и title
            page = get_confluence().get_page_by_title(
                space=info["space"], 
                title=info["title"], 
                expand="version"
            )
            if not page:
                return "Страница не найдена"
            page_id = page.get("id")
        
        # Парсим основную страницу
        main_page = parse_confluence_page(page_id)
        
        result_parts = [
            f"{'='*80}",
            f"СТРАНИЦА: {main_page['title']}",
            f"ID: {main_page['id']}",
            f"{'='*80}",
            f"\n{main_page['content']}\n"
        ]
        
        # Если нужны дочерние страницы
        if extract_child:
            child_ids = get_child_pages(page_id, max_depth=max_depth)
            
            if child_ids:
                result_parts.append(f"\n\n{'#'*80}")
                result_parts.append(f"НАЙДЕНО ДОЧЕРНИХ СТРАНИЦ: {len(child_ids)}")
                result_parts.append(f"{'#'*80}\n")
                
                for idx, child_id in enumerate(child_ids, 1):
                    child_page = parse_confluence_page(child_id)
                    
                    result_parts.append(f"\n{'='*80}")
                    result_parts.append(f"ДОЧЕРНЯЯ СТРАНИЦА #{idx}: {child_page['title']}")
                    result_parts.append(f"ID: {child_page['id']}")
                    result_parts.append(f"{'='*80}")
                    result_parts.append(f"\n{child_page['content']}\n")
        
        return '\n'.join(result_parts)
        
    except (ApiError, ApiPermissionError, HTTPError):
        return "Отсутствует доступ к странице (или страница не существует)."
    except Exception as e:
        return f"Произошла ошибка при загрузке страницы: {e}"


def get_confluence_pages_batch(
    urls: list[str], 
    extract_child: bool = False,
    max_depth: int = 5
) -> dict:
    """
    Получает содержимое нескольких страниц Confluence.
    
    Args:
        urls: Список URL страниц
        extract_child: Извлекать дочерние страницы
        max_depth: Максимальная глубина рекурсии
    
    Returns:
        Словарь {url: content}
    """
    results = {}
    
    for url in urls:
        print(f"Обработка: {url}")
        content = get_confluence_page_content(url, extract_child, max_depth)
        results[url] = content
    
    return results


def _attachment_filename(attachment: dict) -> str:
    raw = str(attachment.get("title") or attachment.get("id") or "attachment")
    name = os.path.basename(raw).replace("\x00", "").strip(" .")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name[:180] or "attachment"


def _attachment_size(attachment: dict) -> int:
    extensions = attachment.get("extensions") or {}
    raw = extensions.get("fileSize") or attachment.get("fileSize") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _download_link(confluence, attachment: dict) -> str:
    link = str((attachment.get("_links") or {}).get("download") or "").strip()
    if not link:
        return ""
    if link.lower().startswith(("http://", "https://")):
        return link
    return confluence.url.rstrip("/") + "/" + link.lstrip("/")


def extract_attachments(confluence, page_num, save_folder: str | None = None) -> list[str]:
    """Скачивает вложения страницы. Возвращает пути файлов поддерживаемых форматов."""
    try:
        from app.handlers.folders_mac import EXCEL_EXTENSIONS, EXTRACTABLE_EXTENSIONS
    except ImportError:
        from folders_mac import EXCEL_EXTENSIONS, EXTRACTABLE_EXTENSIONS

    raw = confluence.get_attachments_from_content(
        page_id=page_num,
        start=0,
        limit=500,
    ) or {}
    attachments = raw.get("results") or []
    folder = save_folder or "confluence_attachments"
    os.makedirs(folder, exist_ok=True)
    saved: list[str] = []
    used_names: set[str] = set()
    for attachment in attachments:
        filename = _attachment_filename(attachment)
        if not filename or filename.startswith("~"):
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext not in EXTRACTABLE_EXTENSIONS:
            print(f"confluence attachment skip unsupported: {filename}")
            continue
        size = _attachment_size(attachment)
        limit = 10 * 1024 * 1024 if ext in EXCEL_EXTENSIONS else 50 * 1024 * 1024
        if size and size > limit:
            print(f"confluence attachment skip too large: {filename}")
            continue
        link = _download_link(confluence, attachment)
        if not link:
            print(f"confluence attachment skip no link: {filename}")
            continue
        name = filename
        stem, suffix = os.path.splitext(filename)
        n = 2
        while name.lower() in used_names:
            name = f"{stem}_{n}{suffix}"
            n += 1
        used_names.add(name.lower())
        try:
            response = requests.get(
                link,
                auth=(confluence.username, confluence.password),
                verify=False,
                timeout=60,
            )
        except Exception as exc:
            print(f"Ошибка загрузки {filename}: {exc}")
            continue
        if response.status_code != 200:
            print(f"Ошибка загрузки {filename}: {response.status_code}")
            continue
        dest = os.path.join(folder, name)
        with open(dest, "wb") as handle:
            handle.write(response.content)
        saved.append(dest)
    return saved


def attachments_text(page_id: str) -> str:
    """Текст вложений страницы тем же пайплайном, что файлы в папке."""
    try:
        from app.handlers.folders_mac import FileTooLargeError, extract_file_text
    except ImportError:
        from folders_mac import FileTooLargeError, extract_file_text

    blocks: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="confluence_attachments_") as folder:
            paths = extract_attachments(get_confluence(), page_id, folder)
            for path in paths:
                name = os.path.basename(path)
                try:
                    text = (extract_file_text(path) or "").strip()
                except FileTooLargeError as exc:
                    print(f"confluence attachment skip: {name}: {exc}")
                    continue
                except Exception as exc:
                    print(f"confluence attachment skip: {name}: {exc}")
                    continue
                if text:
                    blocks.append(f"Вложение «{name}»:\n\n{text}")
    except Exception as exc:
        print(f"confluence attachments warning: {page_id}: {exc}")
        return ""
    return "\n\n".join(blocks)


def _replace_short_urls(text: str, mapping: dict[str, str]) -> str:
    updated = text or ""
    for old, new in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        if old and old != new:
            updated = updated.replace(old, new)
    return updated


def rewrite_stored_confluence_links() -> None:
    """В базе короткий /pages/<id> заменяется на viewpage.action?pageId=."""
    mapping: dict[str, str] = {}

    def remember(value: str) -> None:
        text = (value or "").strip()
        if not text:
            return
        canon = public_confluence_url(text)
        if canon != text:
            mapping[text] = canon
        for found in _SHORT_IN_TEXT.findall(text):
            canon_found = public_confluence_url(found)
            if canon_found != found:
                mapping[found] = canon_found

    try:
        from storage.neo4j.client import get_neo4j_client

        client = get_neo4j_client()
        rows = client.execute_read(
            """
            MATCH (z:Zettel)
            WHERE z.source_input IS NOT NULL AND z.source_input CONTAINS '/pages/'
            RETURN DISTINCT z.source_input AS source_input
            """
        ) or []
        for row in rows:
            remember(str(row.get("source_input") or ""))
    except Exception as exc:
        print(f"confluence link rewrite neo4j read warning: {exc}")
        client = None
        rows = []
    else:
        client = get_neo4j_client()

    try:
        from storage.postgres.db_connect import get_connection

        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_input FROM ingest_digests
                    WHERE source_input LIKE '%/pages/%'
                    """
                )
                for row in cursor.fetchall():
                    remember(str(row[0] or ""))
                cursor.execute(
                    """
                    SELECT source_path, content_hash FROM watch_sources
                    WHERE source_kind = 'confluence'
                       OR source_path LIKE '%/pages/%'
                       OR COALESCE(content_hash, '') LIKE '%/pages/%'
                    """
                )
                watch_rows = cursor.fetchall()
                for source_path, content_hash in watch_rows:
                    remember(str(source_path or ""))
                    remember(str(content_hash or ""))
    except Exception as exc:
        print(f"confluence link rewrite postgres read warning: {exc}")
        watch_rows = []

    if not mapping:
        return

    if client is not None:
        try:
            for old, new in mapping.items():
                client.execute_write(
                    """
                    MATCH (z:Zettel {source_input: $old})
                    SET z.source_input = $new
                    """,
                    {"old": old, "new": new},
                )
        except Exception as exc:
            print(f"confluence link rewrite neo4j write warning: {exc}")

    try:
        from storage.postgres.db_connect import get_connection

        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT graph_id, source_input, content_hash FROM ingest_digests")
                for graph_id, source_input, content_hash in cursor.fetchall():
                    new_source = _replace_short_urls(str(source_input or ""), mapping)
                    if new_source == source_input:
                        continue
                    cursor.execute(
                        """
                        DELETE FROM ingest_digests
                        WHERE graph_id = %s AND source_input = %s
                        """,
                        (graph_id, source_input),
                    )
                    cursor.execute(
                        """
                        INSERT INTO ingest_digests (graph_id, source_input, content_hash, created_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (graph_id, source_input) DO NOTHING
                        """,
                        (graph_id, new_source, content_hash),
                    )
                cursor.execute(
                    """
                    SELECT id, source_path, content_hash FROM watch_sources
                    WHERE source_kind = 'confluence'
                       OR source_path LIKE '%/pages/%'
                       OR COALESCE(content_hash, '') LIKE '%/pages/%'
                    """
                )
                for watch_id, source_path, content_hash in cursor.fetchall():
                    new_path = _replace_short_urls(str(source_path or ""), mapping)
                    new_hash = _replace_short_urls(str(content_hash or ""), mapping)
                    if new_path == source_path and new_hash == content_hash:
                        continue
                    cursor.execute("SAVEPOINT conf_link")
                    try:
                        cursor.execute(
                            """
                            UPDATE watch_sources
                            SET source_path = %s, content_hash = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            (new_path, new_hash, watch_id),
                        )
                        cursor.execute("RELEASE SAVEPOINT conf_link")
                    except Exception as exc:
                        cursor.execute("ROLLBACK TO SAVEPOINT conf_link")
                        print(f"confluence watch link rewrite skip {source_path}: {exc}")
            conn.commit()
    except Exception as exc:
        print(f"confluence link rewrite postgres write warning: {exc}")
    print(f"confluence links rewritten: {len(mapping)}")