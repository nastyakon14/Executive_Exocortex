# добавить инфу по ссылке со страницы confluence
from __future__ import annotations 
from atlassian import Confluence
from atlassian.errors import ApiError, ApiPermissionError
from requests.exceptions import HTTPError
import re
from urllib.parse import unquote
from bs4 import BeautifulSoup
import os
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


def child_page_source(page_id: str) -> str:
    """Стабильная ссылка дочерней страницы: по ней лежат карточки и хэш."""
    return f"{CONFLUENCE_URL.rstrip('/')}/pages/{page_id}"


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
    pages = [{
        "page_id": page_id,
        "source_input": url.strip(),
        "is_parent": True,
    }]
    if extract_child:
        for child_id in get_child_pages(page_id, max_depth=max_depth, seen={page_id}):
            pages.append({
                "page_id": child_id,
                "source_input": child_page_source(child_id),
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
            source = child_page_source(child["page_id"])
            nodes.append({
                "source_input": source,
                "title": child["title"],
                "children": walk(child["page_id"], depth + 1, seen),
            })
        nodes.sort(key=lambda item: (item.get("title") or "").lower())
        return nodes

    return {
        "source_input": url.strip(),
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