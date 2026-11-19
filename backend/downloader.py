import os
import asyncio
import yt_dlp
import requests
import json
import random
from pathlib import Path
import logging
import uuid
import re

# Общие функции очистки/сравнения названий, проверки версий и длительности живут в hitmo_parser.py
try:
    from .hitmo_parser import (
        split_artists, clean_title, build_search_queries, match_track, norm, download_from_hitmo,
        duration_ok, mp3_length, has_bad_marker, BAD_WORDS,
    )
except ImportError:  # модуль запущен не как пакет (backend/ добавлен в sys.path)
    from hitmo_parser import (
        split_artists, clean_title, build_search_queries, match_track, norm, download_from_hitmo,
        duration_ok, mp3_length, has_bad_marker, BAD_WORDS,
    )

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
FFMPEG_PATH = "/usr/bin/ffmpeg"
TRUSTED_FILE = Path(__file__).resolve().parent / "trusted_artists.json"
PROXIES_FILE = Path(__file__).resolve().parent / "proxies.txt"

SC_MAX_DOWNLOAD_ATTEMPTS = 4   # сколько кандидатов максимум пробуем скачать на один трек
SC_MAX_QUERIES = 4             # сколько вариантов поискового запроса максимум

# SoundCloud в свежих yt-dlp ходит через имперсонацию браузера (curl_cffi). Без неё на VPS
# страница артиста/поиск может вернуть пусто/403 — а локально (yt-dlp[default] + curl_cffi) всё работало.
try:
    import curl_cffi  # noqa: F401
except ImportError:
    logger.warning(
        "curl_cffi не установлен: SoundCloud может отдавать пустой результат/403. "
        "Установи: pip install -U \"yt-dlp[default,curl-cffi]\" и перезапусти бота."
    )


def _load_proxies() -> list[str]:
    """Загружает список прокси из backend/proxies.txt (построчно)."""
    if not PROXIES_FILE.exists():
        return []
    try:
        with open(PROXIES_FILE, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    except Exception as e:
        logger.error(f"Ошибка чтения proxies.txt: {e}")
        return []

def _get_ydl_opts(base_path: Path):
    return {
        'format': 'bestaudio/best',
        'outtmpl': str(base_path) + '.%(ext)s',
        'ffmpeg_location': str(FFMPEG_PATH),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'writethumbnail': True,
        'js_runtimes': {'node': {}, 'deno': {}},
        'retries': 3,
        'fragment_retries': 3,
        'socket_timeout': 60,
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
            {'key': 'EmbedThumbnail', 'already_have_thumbnail': False}
        ],
    }


# ============================================================
# ТЕГИ И ОБЛОЖКИ (общие хелперы)
# ============================================================
def _ensure_id3(file_path: str) -> None:
    """Если у mp3 нет ID3-заголовка (сырые файлы Яндекса/Hitmo) — создаём пустой."""
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        ID3(file_path)
    except ID3NoHeaderError:
        ID3().save(file_path)

def _has_cover(file_path: str) -> bool:
    try:
        from mutagen.id3 import ID3
        return bool(ID3(file_path).getall('APIC'))
    except Exception:
        return False

def _embed_cover_bytes(file_path: str, raw: bytes) -> bool:
    """Любую картинку -> JPEG <=320px (лимиты Telegram) -> APIC в mp3."""
    try:
        from PIL import Image
        from io import BytesIO
        from mutagen.id3 import ID3, APIC
        img = Image.open(BytesIO(raw)).convert("RGB")
        img.thumbnail((320, 320))
        out = BytesIO()
        img.save(out, format="JPEG", quality=85)
        _ensure_id3(file_path)
        tags = ID3(file_path)
        tags.delall('APIC')
        tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=out.getvalue()))
        tags.save(file_path)
        return True
    except Exception as e:
        logger.error(f"Не удалось вшить обложку в {file_path}: {e}")
        return False


# ============================================================
# SOUNDCLOUD (запасной источник)
# ============================================================
def _sc_candidate_ok(entry: dict, artist: str, title: str, duration: int, check_artist: bool = True) -> bool:
    url = entry.get('url')
    etitle = entry.get('title') or ''
    if not url or not etitle:
        return False
    if has_bad_marker(etitle, title):
        return False  # slowed / 8D / remix / live / cover ... (если этого нет в самом запросе)
    dur = entry.get('duration') or 0
    if dur and dur < 60:
        return False  # 30-секундные Go+ превью и обрывки
    if duration and dur and not duration_ok(dur, duration):
        return False  # замедленные/ускоренные/удлинённые версии
    signature = f"{entry.get('uploader') or ''} - {etitle}"
    return match_track(signature, artist if check_artist else "", title)

def _sc_search(query: str, limit: int = 10) -> list[dict]:
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            result = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
        return [e for e in ((result or {}).get('entries') or []) if e]
    except Exception as e:
        logger.error(f"Ошибка поиска SoundCloud по '{query}': {e}")
        return []

def _sc_list_page(page_url: str) -> list[dict]:
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            result = ydl.extract_info(page_url, download=False)
        return [e for e in ((result or {}).get('entries') or []) if e]
    except Exception as e:
        logger.error(f"Ошибка чтения страницы {page_url}: {e}")
        return []

def _sc_webpage_url(url: str) -> str | None:
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl_meta:
            info = ydl_meta.extract_info(url, download=False, process=False)
            if info:
                return info.get('webpage_url') or info.get('permalink_url')
    except Exception:
        pass
    return None

def _sc_download(url: str, ydl_opts: dict, base_path: Path) -> str | None:
    proxy_list = _load_proxies()
    opts = dict(ydl_opts)
    if proxy_list:
        opts['proxy'] = random.choice(proxy_list)
        logger.info(f"Использую прокси: {opts['proxy']}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        mp3_path = base_path.with_suffix('.mp3')
        if mp3_path.exists():
            return str(mp3_path)
    except Exception as e:
        if "DRM" in str(e):
            logger.info(f"SoundCloud: трек защищён DRM, беру следующего кандидата ({url})")
        else:
            logger.warning(f"SoundCloud: не удалось скачать {url}: {e}")
    return None

def _sync_download(artist: str, title: str, filename: str, duration: int = 0) -> tuple[str | None, str | None, str | None]:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    base_path = UPLOADS_DIR / filename
    ydl_opts = _get_ydl_opts(base_path)

    tried: set[str] = set()
    attempts = 0

    def try_entries(entries: list[dict], check_artist: bool = True):
        """Перебирает кандидатов: фильтр -> скачивание; DRM/ошибка -> следующий."""
        nonlocal attempts
        for entry in entries:
            if attempts >= SC_MAX_DOWNLOAD_ATTEMPTS:
                return None
            url = entry.get('url')
            if url in tried or not _sc_candidate_ok(entry, artist, title, duration, check_artist):
                continue
            tried.add(url)
            attempts += 1
            webpage_url = _sc_webpage_url(url)
            path = _sc_download(url, ydl_opts, base_path)
            if path:
                real = mp3_length(path)
                if duration and not duration_ok(real, duration):
                    logger.warning(f"SoundCloud: длительность не совпала ({real:.0f}с вместо ~{duration}с), беру следующего: {url}")
                    Path(path).unlink(missing_ok=True)
                    continue
                return path, None, (webpage_url or url)
        return None

    # --- ПРОВЕРКА БЕЛОГО СПИСКА (по полной строке артистов и по каждому артисту отдельно) ---
    artist_url = None
    if TRUSTED_FILE.exists():
        try:
            # utf-8-sig — чтобы не спотыкаться на BOM (PowerShell, Блокнот и т.п.)
            with open(TRUSTED_FILE, 'r', encoding='utf-8-sig') as f:
                trusted = {k.lower(): v for k, v in json.load(f).items()}
            for name in [artist] + split_artists(artist):
                if name.lower() in trusted:
                    artist_url = trusted[name.lower()]
                    logger.info(f"⭐ {name} в белом списке. Ищу на официальной странице.")
                    break
        except Exception as e:
            logger.error(f"Ошибка чтения белого списка: {e}")

    # 1. Официальная страница артиста — артиста внутри не проверяем, он и так «свой»
    if artist_url:
        found = try_entries(_sc_list_page(f"{artist_url}/tracks"), check_artist=False)
        if found:
            return found
        logger.info(f"На странице {artist} ничего не нашли или DRM. Переключаюсь на общий поиск...")

    # 2. Общий поиск: несколько вариантов запроса (главный артист + чистое название, все артисты, первые слова, ...)
    for query in build_search_queries(artist, title, limit=SC_MAX_QUERIES):
        found = try_entries(_sc_search(query, 10))
        if found:
            return found
        if attempts >= SC_MAX_DOWNLOAD_ATTEMPTS:
            break

    # 3. Фолбэк по исполнителю: берём топ его треков и ищем среди них нужное название
    main_artist = (split_artists(artist) or [""])[0]
    if main_artist and norm(main_artist) != "unknown" and attempts < SC_MAX_DOWNLOAD_ATTEMPTS:
        logger.info(f"SoundCloud: фолбэк-поиск по исполнителю '{main_artist}'")
        found = try_entries(_sc_search(main_artist, 25))
        if found:
            return found

    logger.info(f"SoundCloud: ничего не подошло для {artist} - {title} (попыток скачивания: {attempts})")
    return None, "error", None

async def download_track_from_youtube(artist: str, title: str, duration: int = 0) -> tuple[str | None, str | None, str | None]:
    """Имя оставлено прежним для совместимости (на деле — SoundCloud). duration (сек) — необязательная сверка длины."""
    safe_name = "".join(c for c in f"{artist} - {title}" if c.isalnum() or c in " -_").strip()
    return await asyncio.to_thread(_sync_download, artist, title, safe_name, duration)


# ============================================================
# ЕДИНАЯ ТОЧКА ВХОДА: Hitmo -> SoundCloud
# ============================================================
# Яндекс используется ТОЛЬКО для метаданных плейлиста (artist, title, duration, yandex_id).
# Аудио оттуда не качаем: Яндекс отдаёт цензурированные версии.
async def download_track_smart(track: dict) -> tuple[str | None, str | None, str | None]:
    """track = {'artist', 'title', 'duration'?, ...}  ->  (file_path | None, err | None, source_url | None)."""
    artist = track.get('artist') or ''
    title = track.get('title') or ''
    duration = int(track.get('duration') or 0)
    last_err = None

    # 1. SoundCloud — ОСНОВНОЙ источник (там 99% без цензуры)
    path, err, src = await download_track_from_youtube(artist, title, duration)
    if path:
        return path, None, src
    last_err = err
    logger.info(f"SoundCloud не дал файл ({err}) для {artist} - {title}, пробую Hitmo")

    # 2. Hitmo — ЗАПАСНОЙ (через прокси, если SoundCloud не смог)
    try:
        path, err, src = await download_from_hitmo(artist, title, UPLOADS_DIR, duration)
        if path:
            ensure_track_has_cover(path, artist, title)
            return path, None, src
        last_err = err or last_err
    except Exception as e:
        logger.error(f"Hitmo упал на {artist} - {title}: {e}")

    return None, (err or last_err or "not_found"), None


# --- ФУНКЦИЯ: СКАЧИВАНИЕ ПО ПРЯМОЙ ССЫЛКЕ ---
def _sync_download_by_url(url: str, filename: str) -> tuple[str | None, str | None, str | None]:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    unique_filename = f"{filename}_{uuid.uuid4().hex[:6]}"
    base_path = UPLOADS_DIR / unique_filename
    ydl_opts = _get_ydl_opts(base_path)
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            webpage_url = info.get('webpage_url', url)
        
        mp3_path = base_path.with_suffix('.mp3')
        if mp3_path.exists():
            return str(mp3_path), None, webpage_url
    except Exception as e:
        logger.error(f"Ошибка скачивания по ссылке {url}: {e}")
        return None, "error", None
    return None, "error", None

async def download_from_soundcloud_url(url: str, artist: str, title: str) -> tuple[str | None, str | None, str | None]:
    safe_name = "".join(c for c in f"{artist} - {title}" if c.isalnum() or c in " -_").strip()
    return await asyncio.to_thread(_sync_download_by_url, url, safe_name)

def find_soundcloud_link(query: str) -> str | None:
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            result = ydl.extract_info(f"scsearch5:{query} audio", download=False)
            entries = result.get('entries', [])
            for entry in entries:
                url = entry.get('url')
                duration = entry.get('duration', 0)
                if url and (not duration or duration > 30):
                    webpage_url = None
                    try:
                        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl_meta:
                            info = ydl_meta.extract_info(url, download=False, process=False)
                            if info:
                                webpage_url = info.get('webpage_url') or info.get('permalink_url')
                    except Exception:
                        pass
                    return webpage_url or url
    except Exception as e:
        logger.error(f"Ошибка поиска ссылки SoundCloud: {e}")
    return None

def fix_audio_tags(file_path: str, artist: str, title: str) -> None:
    try:
        from mutagen.easyid3 import EasyID3
        _ensure_id3(file_path)  # у сырых mp3 (Яндекс/Hitmo) заголовка ID3 может не быть
        audio = EasyID3(file_path)
        audio['artist'] = str(artist)
        audio['title'] = str(title)
        audio.save()
    except Exception as e:
        logger.error(f"Не удалось исправить теги: {e}")

# --- ОБЛОЖКИ: ПРОВЕРКА И ВШИВАНИЕ ИЗ SOUNDCLOUD ---
def ensure_track_has_cover(file_path: str, artist: str, title: str) -> bool:
    """Проверяет наличие обложки в MP3. Если нет — ищет трек в SoundCloud и вшивает обложку в JPEG.

    Кандидата сверяем по артисту и названию (раньше брался первый результат поиска вслепую
    и мог подтянуться чужой трек).
    """
    if _has_cover(file_path):
        return True

    try:
        main_artist = (split_artists(artist) or [artist])[0]
        query = f"{main_artist} {clean_title(title)}".strip()
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            result = ydl.extract_info(f"scsearch3:{query}", download=False)
        for entry in ((result or {}).get('entries') or []):
            if not entry:
                continue
            signature = f"{entry.get('uploader') or ''} - {entry.get('title') or ''}"
            if not match_track(signature, artist, title):
                continue
            thumb_url = entry.get('thumbnail')
            if entry.get('thumbnails'):
                thumb_url = entry['thumbnails'][-1]['url']
            if not thumb_url:
                continue
            response = requests.get(thumb_url, timeout=10)
            if response.status_code != 200:
                continue
            if _embed_cover_bytes(file_path, response.content):
                logger.info(f"Обложка (JPEG) успешно добавлена для {artist} - {title}")
                return True
    except Exception as e:
        logger.error(f"Ошибка при добавлении обложки для {artist} - {title}: {e}")
    return False

# --- СКАЧИВАНИЕ ПО ПРЯМОЙ ССЫЛКЕ (для /import одиночного трека) ---
def _sync_download_direct_url(url: str, filename: str):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    unique_filename = f"{filename}_{uuid.uuid4().hex[:6]}"
    base_path = UPLOADS_DIR / unique_filename
    ydl_opts = _get_ydl_opts(base_path)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            webpage_url = info.get('webpage_url', url)
        mp3_path = base_path.with_suffix('.mp3')
        if mp3_path.exists():
            return str(mp3_path), None, webpage_url
    except Exception as e:
        logger.error(f"Ошибка скачивания по прямой ссылке {url}: {e}")
        return None, "error", None
    return None, "error", None

async def download_track_from_direct_url(url: str, artist: str, title: str):
    safe_name = "".join(c for c in f"{artist} - {title}" if c.isalnum() or c in " -_").strip()
    return await asyncio.to_thread(_sync_download_direct_url, url, safe_name)