# добавить инфу по ссылке со страницы confluence

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
    login = os.getenv("CONFLUENCE_LOGIN", "")
    pswd = os.getenv("CONFLUENCE_PASSWORD")
    os.environ["NO_PROXY"] = CONFLUENCE_URL
    return Confluence(
        url=CONFLUENCE_URL,
        username=login,
        password=pswd,
        verify_ssl=False,
    )


def get_confluence():
    global _confluence
    if _confluence is None:
        _confluence = connect_Confluence()
    return _confluence

def extract_page_info_from_url(url):
    """Парсит URL-ссылку Confluence для извлечения ID страницы или Space Key + Title."""
    # Декодируем URL (заменяет %20 на пробелы, %D1%82... на кириллицу и т.д.)
    decoded_url = unquote(url).replace("+", " ")

    # Попытка 1: Ищем Page ID (работает для ссылок вида /pages/123456 или viewpage.action?pageId=123456)
    page_id_match = re.search(r"/pages/(\d+)|pageId=(\d+)", decoded_url)
    if page_id_match:
        page_id = page_id_match.group(1) or page_id_match.group(2)
        return {"type": "id", "value": page_id}

    # Попытка 2: Ищем Space Key и Title (для ссылок вида /display/SPACEKEY/Page+Title)
    space_title_match = re.search(r"/display/([^/]+)/([^?#\s]+)", decoded_url)
    if space_title_match:
        space = space_title_match.group(1)
        title = space_title_match.group(2).strip("/")
        return {"type": "space_title", "space": space, "title": title}

    raise ValueError(
        "Не удалось распознать формат ссылки Confluence. Проверьте URL."
    )

def get_confluence_page_version_when(url):
    """Дата последней правки страницы Confluence (UTC) или None."""
    from datetime import datetime, timezone

    info = extract_page_info_from_url(url)
    if info["type"] == "id":
        page = get_confluence().get_page_by_id(info["value"], expand="version")
    else:
        page = get_confluence().get_page_by_title(
            space=info["space"], title=info["title"], expand="version"
        )
    if not page:
        raise RuntimeError("Страница Confluence не найдена")
    when = ((page.get("version") or {}).get("when") or "").strip()
    if not when:
        raise RuntimeError("У страницы нет даты версии")
    dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_confluence_page_content(url):
    """Получает содержимое страницы по ссылке и очищает от HTML-тегов"""
    try:
        info = extract_page_info_from_url(url)
    except ValueError as e:
        return f"Ошибка URL: {e}"

    try:
        # Запрашиваем страницу у Confluence (с расширением body.storage, где лежит весь текст)
        if info["type"] == "id":
            print(f"Запрос по Page ID: {info['value']}")
            page = get_confluence().get_page_by_id(
                info["value"], expand="body.storage"
            )
        else:
            print(f"Запрос по Space: {info['space']}, Title: {info['title']}")
            page = get_confluence().get_page_by_title(
                space=info["space"], title=info["title"], expand="body.storage"
            )

        if not page:
            return "Отсутствует доступ к странице (или страница не найдена)."

        title = page.get("title", "Без названия")

        # Содержимое Confluence хранится в формате XHTML
        xhtml_body = page.get("body", {}).get("storage", {}).get("value", "")

        # Очищаем XHTML от тегов с помощью BeautifulSoup, чтобы получить чистый текст
        soup = BeautifulSoup(xhtml_body, "html.parser")
        clean_text = soup.get_text(separator="\n")

        return f"{title}:\n\n{clean_text}"

    except (ApiError, ApiPermissionError, HTTPError):
        # Confluence скрывает страницы с ограниченным доступом, возвращая 404/403,
        # что приводит к ApiError / HTTPError
        return "Отсутствует доступ к странице (или страница не существует)."

    except Exception as e:
        # Для любых других непредвиденных сетевых или системных сбоев
        return f"Произошла ошибка при загрузке страницы: {e}"
