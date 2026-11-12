import os
import asyncio
import yt_dlp
import requests
from pathlib import Path
import logging
import uuid
import re
import difflib

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
FFMPEG_PATH = BASE_DIR / "ffmpeg.exe"

BAD_WORDS = ['8d', '8 d', '8-d', '8d audio', 'echo', 'эхо', 'cover', 'instrumental', 'минус', 'кавер']

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

def _sync_download(artist: str, title: str, filename: str) -> tuple[str | None, str | None, str | None]:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    base_path = UPLOADS_DIR / filename
    ydl_opts = _get_ydl_opts(base_path)
    
    # Очищаем названия для сравнения
    clean_req_title = re.sub(r'[^\w\s]', '', title.lower()).strip()

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            result = ydl.extract_info(f"scsearch10:{artist} - {title} audio", download=False)
            entries = result.get('entries', [])
            
        for entry in entries:
            entry_title = entry.get('title', '')
            entry_title_lower = entry_title.lower()
            url = entry.get('url')
            duration = entry.get('duration', 0)
            
            if not url or (duration and duration < 60): continue
            if any(bad_word in entry_title_lower for bad_word in BAD_WORDS): continue

            # --- ПРОВЕРКА СХОЖЕСТИ НАЗВАНИЙ ---
            clean_entry_title = re.sub(r'[^\w\s]', '', entry_title_lower).strip()
            
            # Проверяем, входит ли искомое название в найденное или наоборот
            if clean_req_title not in clean_entry_title and clean_entry_title not in clean_req_title:
                # Если не входит, проверяем общую схожесть (для опечаток)
                ratio = difflib.SequenceMatcher(None, clean_req_title, clean_entry_title).ratio()
                if ratio < 0.65: # Если совпадение меньше 65%, пропускаем
                    logger.info(f"Пропущен трек '{entry_title}', так как не похож на '{title}'")
                    continue
            
            webpage_url = None
            try:
                with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl_meta:
                    info = ydl_meta.extract_info(url, download=False, process=False)
                    if info:
                        webpage_url = info.get('webpage_url') or info.get('permalink_url')
            except Exception:
                pass

            final_source_url = webpage_url or url

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                mp3_path = base_path.with_suffix('.mp3')
                if mp3_path.exists():
                    return str(mp3_path), None, final_source_url
            except Exception as e:
                if "DRM" in str(e): continue
    except Exception as e:
        logger.error(f"Ошибка поиска SoundCloud: {e}")

    return None, "error", None

async def download_track_from_youtube(artist: str, title: str) -> tuple[str | None, str | None, str | None]:
    safe_name = "".join(c for c in f"{artist} - {title}" if c.isalnum() or c in " -_").strip()
    return await asyncio.to_thread(_sync_download, artist, title, safe_name)

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
        from mutagen.id3 import ID3NoHeaderError
        from mutagen.mp3 import MP3
        try:
            audio = EasyID3(file_path)
        except ID3NoHeaderError:
            audio = MP3(file_path, ID3=EasyID3)
            audio.add_tags()
            audio = EasyID3(file_path)
        audio['artist'] = str(artist)
        audio['title'] = str(title)
        audio.save()
    except Exception as e:
        logger.error(f"Не удалось исправить теги: {e}")

# --- ОБЛОЖКИ: ПРОВЕРКА И ВШИВАНИЕ ИЗ SOUNDCLOUD ---
def ensure_track_has_cover(file_path: str, artist: str, title: str) -> bool:
    """Проверяет наличие обложки в MP3. Если нет — качает из SoundCloud и вшивает."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3
        audio = MP3(file_path, ID3=ID3)
        if audio.tags and audio.tags.getall('APIC'):
            return True
    except Exception:
        pass

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
            result = ydl.extract_info(f"scsearch1:{artist} - {title} audio", download=False)
            if not result or not result.get('entries'):
                return False

            entry = result['entries'][0]
            thumb_url = None
            if 'thumbnails' in entry:
                thumb_url = entry['thumbnails'][-1]['url']
            elif 'thumbnail' in entry:
                thumb_url = entry['thumbnail']

            if not thumb_url:
                return False

            response = requests.get(thumb_url, timeout=10)
            if response.status_code != 200:
                return False

            from mutagen.id3 import APIC, ID3NoHeaderError
            try:
                audio = MP3(file_path, ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(file_path)
                audio.add_tags()
                audio = MP3(file_path, ID3=ID3)

            audio.tags.add(
                APIC(
                    encoding=3, mime='image/jpeg', type=3, desc='Cover',
                    data=response.content
                )
            )
            audio.save()
            logger.info(f"Обложка успешно добавлена для {artist} - {title}")
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