"""Парсер Hitmo (ru.hitmox.net / hitmo.me / hitmo2.ru) для FreeDom Music Bot.

Ищет трек по запросу "Исполнитель - Название", заходит на страницу трека,
находит прямую ссылку на MP3 и скачивает её. Также умеет собирать все
треки со страницы артиста, автоматически переходя по ?page=N.

Возвращает: (file_path | None, err_type | None, source_url | None)
- Успех: (путь_к_mp3, None, url_страницы_трека)
- Ошибка: (None, "error" | "not_found", None)
"""
from __future__ import annotations

import asyncio
import re
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
import urllib3

# Отключаем спам про невалидные SSL-сертификаты в консоли
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# BeautifulSoup опционален — если не установлен, используется regex-фолбэк
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# Базовые домены Hitmo — пробуем по очереди
HITMO_HOSTS = [
    "https://ru.hitmox.net",
    "https://hitmo.me",
    "https://hitmo2.ru",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Регулярки для парсинга (без BeautifulSoup — работает без доп. зависимостей)
RE_TRACK_LINK = re.compile(r'href="(/tracks?/[^"]+|/song/[^"]+)"', re.I)
RE_MP3_LINK = re.compile(r'(?:data-url|href)="([^"]+\.mp3[^"]*)"', re.I)
RE_TITLE_H1 = re.compile(r'<h1[^>]*>(.*?)</h1>', re.I | re.S)
RE_TAGS = re.compile(r'<[^>]+>')
# Для пагинации без bs4: <a ... href="/song/xxx" ...>Текст</a>
RE_SONG_ANCHOR = re.compile(r'<a\s+[^>]*href="(/song/[^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


def _clean_html(s: str) -> str:
    return RE_TAGS.sub('', s).strip()


def _normalize(s: str) -> str:
    """Приводит строку к нижнему регистру, оставляет только буквы/цифры."""
    return re.sub(r'[^\w\s]', '', s.lower()).strip()


def _clean_artist_from_h1(raw_h1: str) -> str:
    """Очищает H1 от служебных фраз вроде 'Слушать и скачать все песни автора X'."""
    if not raw_h1:
        return "Unknown"
    s = raw_h1.strip()
    # Убираем служебные куски после "автора" и "все песни"
    if "автора" in s:
        s = s.split("автора")[-1].strip()
    if "все песни" in s:
        s = s.split("все песни")[-1].strip()
    # Дополнительные служебные слова
    s = re.sub(r'^(?:скачать|слушать|бесплатно)\s+', '', s, flags=re.IGNORECASE).strip()
    return s or "Unknown"


def _clean_track_title(title_text: str, page_artist: str) -> str:
    """Убирает имя артиста из названия трека (префикс + суффикс)."""
    if not title_text:
        return ""

    # 1. Чистим префикс "Ноггано - ..." / "Ноггано ..."
    if page_artist != "Unknown" and title_text.lower().startswith(page_artist.lower()):
        title_text = title_text[len(page_artist):].strip(" -–—,")

    # 2. Чистим формат "Ноггано - Название"
    if " - " in title_text:
        left, right = title_text.split(" - ", 1)
        if page_artist != "Unknown" and _normalize(left) == _normalize(page_artist):
            title_text = right.strip()

    # 3. Чистим СУФФИКС — если артист склеился в конце (баг с parent.text)
    if page_artist != "Unknown":
        # Если "Ноггано, GUF" в конце — режем всё после (включая запятую и фитов)
        suffix_re = re.compile(rf"\s+{re.escape(page_artist)}\b.*$", re.IGNORECASE)
        title_text = suffix_re.sub("", title_text).strip()

        # Финальная зачистка: если артист в самом конце
        suffix_words = re.compile(rf"\s+{re.escape(page_artist)}\s*$", re.IGNORECASE)
        title_text = suffix_words.sub("", title_text).strip()

    return title_text.strip(" -–—,")


def _search_track_url(session: requests.Session, artist: str, title: str) -> Optional[str]:
    """Ищет страницу трека на Hitmo. Возвращает абсолютный URL или None."""
    
    def clean_text(s: str) -> str:
        # Убираем все спецсимволы, оставляем только буквы и цифры
        return re.sub(r'[^\w\s]', ' ', s)

    # Варианты запросов: 1) Полный, 2) Только название (если артист неизвестен или сложный)
    queries = []
    if artist and artist.lower() != "unknown":
        queries.append(f"{clean_text(artist)} {clean_text(title)}".strip())
    queries.append(clean_text(title).strip()) # Фолбэк только по названию

    for query in queries:
        logger.info(f"[Hitmo] Пробую поиск по запросу: '{query}'")
        
        for host in HITMO_HOSTS:
            try:
                search_url = f"{host}/search?q={quote(query)}"
                r = session.get(search_url, headers=HEADERS, timeout=15, allow_redirects=True, verify=False)
                if r.status_code != 200:
                    continue

                links = RE_TRACK_LINK.findall(r.text)
                if not links:
                    continue

                seen = set()
                uniq_links = []
                for lnk in links:
                    if lnk not in seen:
                        seen.add(lnk)
                        uniq_links.append(lnk)

                # Проверяем первых кандидатов
                for rel in uniq_links[:5]:
                    full_url = urljoin(host, rel)
                    try:
                        rr = session.get(full_url, headers=HEADERS, timeout=15, verify=False)
                        if rr.status_code != 200:
                            continue

                        m = RE_TITLE_H1.search(rr.text)
                        page_title = _clean_html(m.group(1)) if m else ""
                        
                        # Очищаем и сравниваем
                        page_norm = _normalize(page_title)
                        title_norm = _normalize(title)
                        
                        # Если название из запроса есть в заголовке страницы — это оно!
                        if title_norm in page_norm or page_norm in title_norm:
                            logger.info(f"[Hitmo] Совпадение по названию: {page_title} -> {full_url}")
                            return full_url
                            
                    except Exception as e:
                        continue
            except requests.exceptions.RequestException:
                continue

    return None


def _extract_mp3_url(session: requests.Session, track_page_url: str) -> Optional[str]:
    """Со страницы трека достаёт прямую ссылку на mp3."""
    try:
        r = session.get(track_page_url, headers=HEADERS, timeout=15, verify=False)
        if r.status_code != 200:
            return None

        # Ищем прямые .mp3 ссылки (в data-url или href)
        mp3_links = RE_MP3_LINK.findall(r.text)
        if mp3_links:
            return urljoin(track_page_url, mp3_links[0])

        # Фолбэк: ищем "download" кнопку с data-url, даже без .mp3 в href
        m = re.search(r'data-url="([^"]+)"', r.text)
        if m and ("download" in m.group(1).lower() or ".mp3" in m.group(1).lower()):
            return urljoin(track_page_url, m.group(1))

        return None
    except Exception as e:
        logger.error(f"[Hitmo] Ошибка парсинга страницы трека: {e}")
        return None


def _sync_download_from_hitmo(
    artist: str, title: str, uploads_dir: Path
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Синхронная функция — вызывается из asyncio.to_thread."""
    uploads_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(
        c for c in f"{artist} - {title}" if c.isalnum() or c in " -_"
    ).strip() or "track"
    dest = uploads_dir / f"hitmo_{safe_name}.mp3"

    session = requests.Session()

    try:
        track_url = _search_track_url(session, artist, title)
        if not track_url:
            logger.info(f"[Hitmo] Не найдено: {artist} - {title}")
            return None, "not_found", None

        mp3_url = _extract_mp3_url(session, track_url)
        if not mp3_url:
            logger.info(f"[Hitmo] На странице {track_url} не найдена mp3-ссылка")
            return None, "error", track_url

        logger.info(f"[Hitmo] Скачиваю: {mp3_url}")
        with session.get(mp3_url, headers=HEADERS, timeout=120, stream=True, verify=False) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        if not dest.exists() or dest.stat().st_size < 50_000:
            logger.warning(f"[Hitmo] Файл слишком маленький или пустой: {dest}")
            try:
                dest.unlink()
            except Exception:
                pass
            return None, "error", track_url

        logger.info(f"[Hitmo] ✅ Скачано: {dest} ({dest.stat().st_size} байт)")
        return str(dest), None, track_url

    except Exception as e:
        logger.error(f"[Hitmo] Ошибка скачивания {artist} - {title}: {e}")
        try:
            if dest.exists():
                dest.unlink()
        except Exception:
            pass
        return None, "error", None


# ============================================================
# УМНАЯ ПАГИНАЦИЯ: собираем все треки со страницы артиста
# ============================================================
def _sync_parse_artist_page(url: str) -> list[dict]:
    """Парсит ВСЕ страницы артиста, автоматически переходя по ?page=N.

    Возвращает список словарей {artist, title, url}.
    """
    session = requests.Session()
    all_tracks: list[dict] = []

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # Начинаем с указанной страницы (по умолчанию 1)
    try:
        start_page = int(query.get('page', [1])[0])
    except (ValueError, TypeError):
        start_page = 1
    max_pages = 30  # Защита от бесконечности

    for page in range(start_page, start_page + max_pages):
        query['page'] = [str(page)]
        new_query = urlencode(query, doseq=True)
        current_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                                  parsed.params, new_query, parsed.fragment))

        logger.info(f"[Hitmo Artist] Парсинг страницы: {current_url}")
        try:
            r = session.get(current_url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code != 200:
                logger.warning(f"[Hitmo Artist] Страница {page} вернула {r.status_code}")
                break

            page_tracks: list[dict] = []

            if BS4_AVAILABLE:
                soup = BeautifulSoup(r.text, 'html.parser')

                # 1. Берём имя артиста из заголовка страницы <h1>
                h1_tag = soup.find('h1')
                page_artist = _clean_artist_from_h1(h1_tag.text.strip() if h1_tag else "")
                
                # Если H1 пустой или Unknown — ищем в <title>
                if page_artist == "Unknown":
                    title_tag = soup.find('title')
                    if title_tag:
                        title_text = title_tag.text.strip()
                        if " - " in title_text:
                            page_artist = title_text.split(" - ")[0].strip()
                        elif "Скачать песни" in title_text:
                            page_artist = title_text.split("Скачать песни")[-1].split("бесплатно")[0].strip()

                logger.info(f"[Hitmo Artist] Имя артиста: '{page_artist}'")

                for a_tag in soup.find_all('a', href=re.compile(r'/(song|track)/')):
                    track_url = urljoin(current_url, a_tag['href'])

                    # 2. Берём название из текста ссылки (или родителя)
                    title_text = a_tag.text.strip()
                    if not title_text:
                        parent = a_tag.find_parent(['div', 'li'])
                        if parent:
                            title_text = parent.text.strip()

                    title_text = ' '.join(title_text.split())
                    if not title_text:
                        continue

                    # 3. Чистим префикс/суффикс артиста
                    title_text = _clean_track_title(title_text, page_artist)

                    if title_text:
                        page_tracks.append({
                            "artist": page_artist,
                            "title": title_text,
                            "url": track_url,
                        })
            else:
                # Regex-фолбэк, если bs4 не установлен
                h1_match = RE_TITLE_H1.search(r.text)
                page_artist = _clean_artist_from_h1(_clean_html(h1_match.group(1)) if h1_match else "")
                logger.info(f"[Hitmo Artist] Имя артиста из H1 (regex): '{page_artist}'")

                for m in RE_SONG_ANCHOR.finditer(r.text):
                    rel_url = m.group(1)
                    raw_text = m.group(2)
                    track_url = urljoin(current_url, rel_url)
                    title_text = _clean_html(raw_text)
                    title_text = ' '.join(title_text.split())
                    if not title_text:
                        continue

                    title_text = _clean_track_title(title_text, page_artist)

                    if title_text:
                        page_tracks.append({
                            "artist": page_artist,
                            "title": title_text,
                            "url": track_url,
                        })

            if not page_tracks:
                logger.info(f"[Hitmo Artist] Страница {page} не содержит треков. Конец.")
                break

            all_tracks.extend(page_tracks)
            time.sleep(1.5)  # Пауза, чтобы Hitmo не забанил

        except Exception as e:
            logger.error(f"[Hitmo Artist] Ошибка на странице {page}: {e}")
            break

    # Убираем дубликаты по URL
    unique_tracks = {t['url']: t for t in all_tracks}.values()
    logger.info(f"[Hitmo Artist] Всего найдено {len(unique_tracks)} уникальных треков.")
    return list(unique_tracks)


async def parse_artist_page_hitmo(url: str) -> list[dict]:
    """Асинхронная обёртка над _sync_parse_artist_page."""
    return await asyncio.to_thread(_sync_parse_artist_page, url)


async def download_from_hitmo(
    artist: str, title: str, uploads_dir: Path
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Асинхронная обёртка. Возвращает (file_path, err_type, source_url)."""
    return await asyncio.to_thread(_sync_download_from_hitmo, artist, title, uploads_dir)