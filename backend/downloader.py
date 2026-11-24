import os
import asyncio
import yt_dlp
import requests
import json
import random
from collections.abc import Iterator
from pathlib import Path
import logging
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
import uuid
import re
from urllib.parse import urlsplit

# Общие функции очистки/сравнения названий, проверки версий и длительности живут в hitmo_parser.py
try:
    from .hitmo_parser import (
        split_artists, clean_title, build_search_queries, match_track, norm, download_from_hitmo,
        duration_ok, mp3_length, has_bad_marker, has_censored_label,
    )
except ImportError:  # модуль запущен не как пакет (backend/ добавлен в sys.path)
    from hitmo_parser import (
        split_artists, clean_title, build_search_queries, match_track, norm, download_from_hitmo,
        duration_ok, mp3_length, has_bad_marker, has_censored_label,
    )

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
FFMPEG_PATH = "/usr/bin/ffmpeg"
TRUSTED_FILE = Path(__file__).resolve().parent / "trusted_artists.json"
PROXIES_FILE = Path(__file__).resolve().parent / "proxies.txt"

SC_MAX_DOWNLOAD_ATTEMPTS = 4   # сколько кандидатов максимум пробуем скачать на один трек
SC_MAX_QUERIES = 4             # сколько вариантов поискового запроса максимум
SC_COOLDOWN_SECONDS = 120
_sc_retry_at = 0.0
_sc_cooldown_lock = Lock()


class SoundCloudForbidden(Exception):
    pass


def _sc_in_cooldown() -> bool:
    return monotonic() < _sc_retry_at


def _sc_pause() -> None:
    global _sc_retry_at
    with _sc_cooldown_lock:
        if _sc_in_cooldown():
            return
        _sc_retry_at = monotonic() + SC_COOLDOWN_SECONDS
    logger.warning("SoundCloud: доступ отклонён (HTTP 403). Следующие %s с использую Hitmo.", SC_COOLDOWN_SECONDS)


def _sc_forbidden(error: Exception) -> bool:
    return bool(re.search(r"\bHTTP Error 403\b", str(error)))

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
def _sc_candidate_ok(entry: dict, artist: str, title: str, duration: int, check_artist: bool = True,
                     allow_short: bool = False) -> bool:
    etitle = entry.get('title') or ''
    if not etitle:
        return False
    if has_bad_marker(etitle, title):
        return False  # slowed / 8D / remix / live / cover ... (если этого нет в самом запросе)
    dur = entry.get('duration') or 0
    if dur and dur < 60 and not allow_short:
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
        if _sc_forbidden(e):
            raise SoundCloudForbidden("поиск вернул HTTP 403") from e
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
        logger.info("SoundCloud: использую настроенный прокси.")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        mp3_path = base_path.with_suffix('.mp3')
        if mp3_path.exists():
            return str(mp3_path)
    except Exception as e:
        if "DRM" in str(e):
            logger.info(f"SoundCloud: трек защищён DRM, беру следующего кандидата ({url})")
        elif _sc_forbidden(e):
            raise SoundCloudForbidden("загрузка вернула HTTP 403") from e
        else:
            logger.warning(f"SoundCloud: не удалось скачать {url}: {e}")
    return None

def is_soundcloud_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return parsed.scheme in {"http", "https"} and (host == "soundcloud.com" or host.endswith(".soundcloud.com"))
    except ValueError:
        return False


def _sc_public_url(url: str) -> bool:
    return is_soundcloud_url(url) and urlsplit(url).hostname in {"soundcloud.com", "www.soundcloud.com"}


def _iter_matches(artist: str, title: str, duration: int, prefer_uncensored=False) -> Iterator[str]:
    """Перебирает подходящие ссылки, запрашивая следующий источник только при необходимости."""
    seen: set[str] = set()

    def match_entries(entries: list[dict], check_artist: bool = True) -> Iterator[str]:
        for entry in entries:
            webpage = entry.get('webpage_url') or ''
            url = webpage if _sc_public_url(webpage) else entry.get('url')
            allow_short = prefer_uncensored and 0 < duration < 60
            if not url or url in seen or not _sc_candidate_ok(entry, artist, title, duration, check_artist, allow_short):
                continue
            if prefer_uncensored and (not is_soundcloud_url(url) or has_censored_label(entry["title"], title)):
                continue
            seen.add(url)
            yield url

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
    if artist_url and (not prefer_uncensored or is_soundcloud_url(artist_url)):
        yield from match_entries(_sc_list_page(f"{artist_url}/tracks"), check_artist=False)

    # 2. Общий поиск: несколько вариантов запроса (главный артист + чистое название, все артисты, первые слова, ...)
    for query in build_search_queries(artist, title, limit=SC_MAX_QUERIES):
        yield from match_entries(_sc_search(query, 10))

    # 3. Фолбэк по исполнителю: берём топ его треков и ищем среди них нужное название
    main_artist = (split_artists(artist) or [""])[0]
    if main_artist and norm(main_artist) != "unknown":
        logger.info(f"SoundCloud: фолбэк-поиск по исполнителю '{main_artist}'")
        yield from match_entries(_sc_search(main_artist, 25))


def _download_candidate(url: str, destination: Path, duration: int) -> str | None:
    # Свой временный каталог у каждой попытки: yt-dlp не переиспользует чужой .part.
    with TemporaryDirectory(dir=destination.parent) as temp_dir:
        base_path = Path(temp_dir) / "audio"
        path = _sc_download(url, _get_ydl_opts(base_path), base_path)
        if not path:
            return None
        real_duration = mp3_length(path)
        if not real_duration or not duration_ok(real_duration, duration):
            logger.warning(
                "SoundCloud: неподходящая длительность (%.0f с, ожидалось ~%s с): %s",
                real_duration, duration or "?", url,
            )
            return None
        Path(path).replace(destination)
    return str(destination)


def _sync_download(artist: str, title: str, filename: str, duration: int = 0, *,
                   uploads_dir: Path = UPLOADS_DIR, prefer_uncensored=False) -> tuple[str | None, str | None, str | None]:
    if _sc_in_cooldown():
        return None, "cooldown", None
    uploads_dir.mkdir(parents=True, exist_ok=True)
    destination = uploads_dir / f"{(filename or 'track')[:55]}_{uuid.uuid4().hex[:12]}.mp3"
    attempts = 0
    forbidden = 0
    try:
        for attempts, url in enumerate(_iter_matches(artist, title, duration, prefer_uncensored), start=1):
            if _sc_in_cooldown():
                return None, "cooldown", None
            try:
                path = _download_candidate(url, destination, duration)
            except SoundCloudForbidden:
                forbidden += 1
                if forbidden >= 2:
                    raise
                logger.info("SoundCloud: кандидат вернул HTTP 403, проверяю другую запись.")
            else:
                if path:
                    return path, None, url if _sc_public_url(url) else (_sc_webpage_url(url) or url)
            if attempts >= SC_MAX_DOWNLOAD_ATTEMPTS:
                break
    except SoundCloudForbidden:
        _sc_pause()
        return None, "forbidden", None

    logger.info(f"SoundCloud: ничего не подошло для {artist} - {title} (попыток скачивания: {attempts})")
    return None, "error", None

async def download_track_from_youtube(artist: str, title: str, duration: int = 0, *,
                                      uploads_dir: Path = UPLOADS_DIR, prefer_uncensored=False) -> tuple[str | None, str | None, str | None]:
    """Имя оставлено прежним для совместимости (на деле — SoundCloud). duration (сек) — необязательная сверка длины."""
    safe_name = "".join(c for c in f"{artist} - {title}" if c.isalnum() or c in " -_").strip()
    return await asyncio.to_thread(_sync_download, artist, title, safe_name, duration,
                                   uploads_dir=uploads_dir, prefer_uncensored=prefer_uncensored)


# ============================================================
# ЕДИНАЯ ТОЧКА ВХОДА: SoundCloud -> Hitmo
# ============================================================
# Яндекс используется ТОЛЬКО для метаданных плейлиста (artist, title, duration, yandex_id).
# Аудио оттуда не качаем: Яндекс отдаёт цензурированные версии.
async def download_track_smart(track: dict, *, uploads_dir: Path = UPLOADS_DIR,
                               prefer_uncensored=False) -> tuple[str | None, str | None, str | None]:
    """track = {'artist', 'title', 'duration'?, ...}  ->  (file_path | None, err | None, source_url | None)."""
    artist = track.get('artist') or ''
    title = track.get('title') or ''
    duration = int(track.get('duration') or 0)
    last_err = None

    # 1. SoundCloud — ОСНОВНОЙ источник (там 99% без цензуры)
    path, err, src = await download_track_from_youtube(artist, title, duration,
                                                       uploads_dir=uploads_dir, prefer_uncensored=prefer_uncensored)
    if path:
        return path, None, src
    last_err = err
    logger.info(f"SoundCloud не дал файл ({err}) для {artist} - {title}, пробую Hitmo")

    # 2. Hitmo — ЗАПАСНОЙ (через прокси, если SoundCloud не смог)
    try:
        path, err, src = await download_from_hitmo(artist, title, uploads_dir, duration,
                                                 prefer_uncensored=prefer_uncensored)
        if path:
            await asyncio.to_thread(ensure_track_has_cover, path, artist, title)
            return path, None, src
        last_err = err or last_err
    except Exception as e:
        logger.error(f"Hitmo упал на {artist} - {title}: {e}")

    return None, (last_err or "not_found"), None


# --- ФУНКЦИЯ: СКАЧИВАНИЕ ПО ПРЯМОЙ ССЫЛКЕ ---
def _sync_download_by_url(
    url: str, filename: str, *, proxy: str | None = None, uploads_dir: Path = UPLOADS_DIR
) -> tuple[str | None, str | None, str | None]:
    uploads_dir.mkdir(parents=True, exist_ok=True)
    unique_filename = f"{filename}_{uuid.uuid4().hex[:6]}"
    base_path = uploads_dir / unique_filename
    ydl_opts = _get_ydl_opts(base_path)
    if proxy:
        ydl_opts['proxy'] = proxy

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

async def download_from_soundcloud_url(url: str, artist: str, title: str, *,
                                       uploads_dir: Path = UPLOADS_DIR) -> tuple[str | None, str | None, str | None]:
    safe_name = "".join(c for c in f"{artist} - {title}" if c.isalnum() or c in " -_").strip()[:55]
    return await asyncio.to_thread(_sync_download_by_url, url, safe_name, uploads_dir=uploads_dir)


async def download_from_youtube_url(url: str, artist: str, title: str) -> tuple[str | None, str | None, str | None]:
    safe_name = "".join(c for c in f"{artist} - {title}" if c.isalnum() or c in " -_").strip()
    proxy = os.getenv("HITMO_PROXY")
    return await asyncio.to_thread(_sync_download_by_url, url, safe_name, proxy=proxy)


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
    if _sc_in_cooldown():
        return False

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

def extract_telegram_thumbnail(file_path: str) -> bytes | None:
    from io import BytesIO
    from mutagen.id3 import ID3
    from PIL import Image
    try:
        tags = ID3(file_path)
        frames = tags.getall("APIC")
        if not frames:
            return None
        frame = next((f for f in frames if f.type == 3), frames[0])
        img = Image.open(BytesIO(frame.data)).convert("RGB")
        img.thumbnail((320, 320))
        output = BytesIO()
        img.save(output, format="JPEG", quality=85, optimize=True)
        return output.getvalue()
    except Exception:
        return None
