"""Парсер плейлистов FreeDom Music Bot: VK, Яндекс.Музыка, Spotify."""
from __future__ import annotations

import os
import re
import logging
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from yandex_music import Client
from yandex_music.exceptions import YandexMusicError, UnauthorizedError, NotFoundError

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

load_dotenv()

SourceType = Literal["vk", "yandex", "spotify"]
TrackDict = Dict[str, str]

_yandex_client = None

def get_yandex_client() -> Client:
    global _yandex_client
    if _yandex_client is None:
        token = os.getenv("YANDEX_TOKEN")
        if not token: raise ValueError("Не задана переменная окружения YANDEX_TOKEN.")
        try:
            _yandex_client = Client(token).init()
            logger.info("Клиент Яндекс.Музыки успешно инициализирован и закэширован.")
        except UnauthorizedError as exc:
            raise ValueError("Токен Яндекс.Музыки недействителен или просрочен.") from exc
        except YandexMusicError as exc:
            raise ValueError(f"Ошибка инициализации клиента Яндекс.Музыки: {exc}")
    return _yandex_client

_VK_HOSTS = {"vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "m.vk.ru"}
_VK_PATH_PLAYLIST = re.compile(r"^/music/(?:playlist|album)/(-?\d+)_(\d+)(?:_([0-9A-Za-z]+))?/?$", re.IGNORECASE)
_VK_AUDIO_PLAYLIST = re.compile(r"audio_playlist(-?\d+)_(\d+)(?:_([0-9A-Za-z]+))?", re.IGNORECASE)

_YANDEX_HOST_RE = re.compile(r"^(?:www\.)?music\.yandex\.(?:ru|com|by|kz|ua|uz)$", re.IGNORECASE)
_YANDEX_ALBUM = re.compile(r"^/album/(\d+)/?", re.IGNORECASE)
_YANDEX_USER_PLAYLIST = re.compile(r"^/users/([^/]+)/playlists/(\d+)/?", re.IGNORECASE)
_YANDEX_LK_PLAYLIST = re.compile(r"^/playlists/lk\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?", re.IGNORECASE)
_YANDEX_UUID_PLAYLIST = re.compile(r"^/playlists/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?", re.IGNORECASE)

_SPOTIFY_HOSTS = {"open.spotify.com"}
_SPOTIFY_PLAYLIST = re.compile(r"^/(?:intl-[a-z]+/)?playlist/([A-Za-z0-9]+)(?:/.*)?$", re.IGNORECASE)

def _getenv_token(name: str) -> str: return (os.getenv(name) or "").strip()

def _parse_url(url: str):
    raw = (url or "").strip()
    if not raw: raise ValueError("Ссылка на плейлист пустая")
    if "://" not in raw: raw = "https://" + raw
    parsed = urlparse(raw)
    return parsed, (parsed.netloc or "").lower(), parsed.path or ""

def _vk_ids(url: str) -> Optional[tuple[str, str, Optional[str]]]:
    parsed, host, path = _parse_url(url)
    if host not in _VK_HOSTS: return None
    match = _VK_PATH_PLAYLIST.match(path)
    if match: return match.groups()
    blob = f"{path}?{parsed.query}#{parsed.fragment}"
    match = _VK_AUDIO_PLAYLIST.search(blob)
    if match: return match.groups()
    return None

def _yandex_target(url: str) -> Optional[tuple[str, tuple[str, ...]]]:
    _parsed, host, path = _parse_url(url)
    if not _YANDEX_HOST_RE.match(host): return None
    album = _YANDEX_ALBUM.match(path)
    if album: return "album", (album.group(1),)
    user_pl = _YANDEX_USER_PLAYLIST.match(path)
    if user_pl: return "user_playlist", (user_pl.group(1), user_pl.group(2))
    lk_pl = _YANDEX_LK_PLAYLIST.match(path)
    if lk_pl: return "lk_playlist", (lk_pl.group(1),)
    uuid_pl = _YANDEX_UUID_PLAYLIST.match(path)
    if uuid_pl: return "uuid_playlist", (uuid_pl.group(1),)
    return None

def _spotify_id(url: str) -> Optional[str]:
    _parsed, host, path = _parse_url(url)
    if host not in _SPOTIFY_HOSTS: return None
    match = _SPOTIFY_PLAYLIST.match(path)
    if match: return match.group(1)
    return None

def detect_source(url: str) -> SourceType:
    try:
        if _vk_ids(url) is not None: return "vk"
        if _yandex_target(url) is not None: return "yandex"
        if _spotify_id(url) is not None: return "spotify"
    except ValueError: pass
    raise ValueError(f"Невалидная ссылка на плейлист: {url!r}. Ожидается VK, Яндекс.Музыка или Spotify.")

def _track(title: str, artist: str, duration: int = 0) -> TrackDict:
    return {"title": (title or "").strip(), "artist": (artist or "").strip(), "duration": duration}

def parse_vk_playlist(url: str) -> Tuple[List[TrackDict], str]:
    ids = _vk_ids(url)
    if ids is None: raise ValueError(f"Невалидная ссылка на плейлист VK: {url!r}")
    token = _getenv_token("VK_TOKEN")
    if not token: raise RuntimeError("Не задана переменная окружения VK_TOKEN.")
    owner_id, playlist_id, access_key = ids
    try:
        import vk_api
        from vk_api.exceptions import VkApiError
    except ImportError as exc:
        raise RuntimeError("Библиотека vk-api не установлена") from exc
    try:
        session = vk_api.VkApi(token=token)
        api = session.get_api()
        tracks: List[TrackDict] = []
        offset = 0
        
        # Попытка получить название плейлиста
        playlist_title = "Импорт из ВКонтакте"
        try:
            pl_info = api.audio.getPlaylistById(owner_id=int(owner_id), playlist_id=int(playlist_id))
            if pl_info and pl_info.get("title"):
                playlist_title = pl_info["title"]
        except Exception:
            pass

        while True:
            params = {"owner_id": int(owner_id), "album_id": int(playlist_id), "count": 200, "offset": offset}
            if access_key: params["access_key"] = access_key
            response = api.audio.get(**params)
            items = response.get("items") if isinstance(response, dict) else response
            if not items: break
            for item in items:
                if item.get("title"): 
                    tracks.append(_track(item["title"], item.get("artist"), item.get("duration", 0)))
            if len(items) < 200: break
            offset += 200
    except VkApiError as exc:
        raise ConnectionError(f"Не удалось распарсить плейлист VK: {exc}") from exc
    return tracks, playlist_title

def _yandex_artist_name(track: object) -> str:
    names_fn = getattr(track, "artists_name", None)
    if callable(names_fn):
        names = [name for name in names_fn() if name]
        if names: return ", ".join(names)
    artists = getattr(track, "artists", None) or []
    names = [getattr(artist, "name", "") for artist in artists if getattr(artist, "name", None)]
    return ", ".join(names)

def parse_yandex_playlist(url: str) -> Tuple[List[Dict[str, str]], str]:
    client = get_yandex_client()
    RE_LK_UUID = re.compile(r"music\.yandex\.[a-z]+/playlists/lk\.([0-9a-f\-]{36})", re.IGNORECASE)
    RE_PUBLIC_UUID = re.compile(r"music\.yandex\.[a-z]+/playlists/([0-9a-f\-]{36})", re.IGNORECASE)
    RE_USER_PLAYLIST = re.compile(r"music\.yandex\.[a-z]+/users/([^/]+)/playlists/([^/?#]+)", re.IGNORECASE)

    match_lk = RE_LK_UUID.search(url)
    match_public = RE_PUBLIC_UUID.search(url)
    match_user = RE_USER_PLAYLIST.search(url)
    playlist = None
    playlist_title = "Импорт из Яндекса"

    if match_lk or match_public:
        playlist_uuid = match_lk.group(1) if match_lk else match_public.group(1)
        try:
            user_playlists = client.users_playlists_list()
        except Exception as exc:
            raise ValueError(f"Нет доступа к списку плейлистов: {exc}")
        target = None
        for pl in user_playlists or []:
            if getattr(pl, "playlist_uuid", None) == playlist_uuid:
                target = pl
                break
        if target is None: raise ValueError(f"Плейлист с UUID {playlist_uuid} не найден.")
        
        playlist_title = getattr(target, "title", "Импорт из Яндекса")
        
        try:
            playlist = client.users_playlists(kind=target.kind, user_id=target.uid)
        except Exception:
            playlist = target
    elif match_user:
        owner_login = match_user.group(1)
        kind = match_user.group(2)
        try:
            playlist = client.users_playlists(kind=kind, user_id=owner_login)
            playlist_title = getattr(playlist, "title", "Импорт из Яндекса")
        except NotFoundError:
            raise ValueError(f"Плейлист не найден: /users/{owner_login}/playlists/{kind}")
    else:
        raise ValueError(f"Не удалось распознать URL плейлиста: {url}")

    if playlist is None: raise ValueError(f"Плейлист не найден или недоступен: {url}")

    track_list = playlist.tracks or []
    tracks = []
    track_ids = []
    for t in track_list:
        tid = getattr(t, 'id', None) or getattr(t, 'track_id', None)
        if not tid and hasattr(t, 'track'): tid = getattr(t.track, 'id', None)
        if tid: track_ids.append(str(tid))
            
    if track_ids:
        try:
            full_tracks = client.tracks(track_ids)
            for ft in full_tracks:
                if ft and getattr(ft, 'title', None):
                    dur = int(ft.duration_ms / 1000) if getattr(ft, 'duration_ms', None) else 0
                    tracks.append(_track(str(ft.title), _yandex_artist_name(ft), dur))
            return tracks, playlist_title
        except Exception as e:
            logger.warning(f"Ошибка пакетной загрузки: {e}. Включаю медленный режим...")

    for track_short in track_list:
        title = getattr(track_short, "title", None)
        artists = getattr(track_short, "artists", None)
        dur_ms = getattr(track_short, "duration_ms", 0)
        if (not title or not artists) and hasattr(track_short, "fetch_track"):
            try:
                full = track_short.fetch_track()
                title = getattr(full, "title", title)
                artists = getattr(full, "artists", artists)
                dur_ms = getattr(full, "duration_ms", dur_ms)
            except Exception: pass
        artist_name = getattr(artists[0], "name", "Unknown") if artists else "Unknown"
        if title: tracks.append(_track(title, artist_name, int(dur_ms / 1000) if dur_ms else 0))
    return tracks, playlist_title

def parse_spotify_playlist(url: str) -> Tuple[List[TrackDict], str]:
    playlist_id = _spotify_id(url)
    if playlist_id is None: raise ValueError(f"Невалидная ссылка на плейлист Spotify: {url!r}")
    logger.warning("Парсер Spotify пока не реализован.")
    return [], "Импорт из Spotify"

def parse_playlist(url: str) -> Tuple[List[TrackDict], str]:
    source = detect_source(url)
    if source == "vk": return parse_vk_playlist(url)
    if source == "yandex": return parse_yandex_playlist(url)
    return parse_spotify_playlist(url)