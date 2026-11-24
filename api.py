from fastapi import Body, FastAPI, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, Response, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
import logging
from html import escape as esc
from pathlib import Path
from mutagen import File as MutagenFile
import hmac
import hashlib
import time
import json
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Literal

# --- НОВЫЕ ИМПОРТЫ ДЛЯ АДМИН-ПРЕВЬЮ ---
import subprocess
import asyncio
import os
# --------------------------------------

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, LabeledPrice, BufferedInputFile
from aiogram.utils.web_app import WebAppUser, safe_parse_webapp_init_data

from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
STREAM_SECRET_KEY = os.getenv("STREAM_SECRET_KEY", "super_secret_fallback_key") 
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

from backend.database import (
    get_user, claim_daily, search_tracks, check_download_limit,
    increment_download_count, get_track_by_id, increment_track_download,
    add_points, get_user_by_internal_id, delete_track,
    create_user_playlist, get_user_playlists, add_track_to_playlist, get_playlist_tracks,
    get_level_progress, set_playlist_price, get_market_playlists, buy_playlist,
    clear_user_likes, clear_user_playlist, delete_user_playlist, remove_playlist_from_market,
    toggle_like, get_user_liked_tracks, get_user_liked_track_ids, get_user_stats,
    report_track, remove_track_from_playlist, get_playlist_by_id,
    set_playlist_custom_cover, get_similar_artists, get_trending_tracks,
    get_trending_artists, get_artist_tracks, get_artist_albums, get_artist_album, get_artist_album_cover,
    get_track_artist_pages,
    set_track_file_id,
    update_user_settings, buy_theme,
    get_wave_recommendations,
    is_premium_active, parse_premium_expiry, get_pending_tracks, update_track_status, delete_track, get_admin_stats,
    get_or_create_telegram_chat, create_telegram_topic, LIKED_PLAYLIST_TITLE, sync_liked_playlist
)
from backend.database import (
    PROFILE_DECORATIONS, change_profile_decoration, get_profile_decorations,
    get_artist_cover, set_artist_cover, get_playlist_market_preview, set_playlist_preview_tracks,
    init_mini_app_schema, set_public_username,
)
from backend.import_messages import PLAYLIST_IMPORT_INSTRUCTIONS
from backend import playlist_sync
from backend.playlist_media import TRAILER_MAX_BYTES, upload_playlist_trailer

# --- НОВЫЙ ИМПОРТ ДЛЯ FFMPEG/SOUNDCLOUD ---
from backend.downloader import (
    fix_audio_tags, FFMPEG_PATH, find_soundcloud_link, extract_telegram_thumbnail
)
# -----------------------------------------

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_mini_app_schema()
    yield


app = FastAPI(lifespan=lifespan)
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return f.read()

def get_tg_user(request: Request) -> WebAppUser | None:
    auth_header = request.headers.get("Authorization", "")
    init_data = ""
    
    if auth_header.startswith("TWA "):
        init_data = auth_header[4:]
    else:
        init_data = request.query_params.get("tgWebAppData", "")
    
    if not init_data:
        return None
        
    try:
        parsed = safe_parse_webapp_init_data(token=TELEGRAM_BOT_TOKEN, init_data=init_data)
        return parsed.user
    except Exception as e:
        logger.warning(f"Попытка подделки initData или ошибка парсинга: {e}")
        return None


async def get_tg_user_id(request: Request) -> int | None:
    user = get_tg_user(request)
    return user.id if user else None

# --- ЗАВИСИМОСТИ БЕЗОПАСНОСТИ ---
async def current_user(request: Request) -> dict:
    tg_id = await get_tg_user_id(request)
    if not tg_id: raise HTTPException(401, "Нет данных от Telegram")
    user = await get_user(tg_id)
    if not user: raise HTTPException(401, "Пользователь не найден")
    return user

async def owned_playlist(playlist_id: int, user: dict = Depends(current_user)) -> dict:
    pl = await get_playlist_by_id(playlist_id)
    if not pl or pl["user_id"] != user["id"] or pl["is_system"]:
        raise HTTPException(404, "Плейлист не найден или нет прав")
    return pl
# ----------------------------------

def stream_signature(resource_id: int, user_id: int, expires: int, *, scope: str = "") -> str:
    secret = TELEGRAM_BOT_TOKEN if scope == "trailer:" else STREAM_SECRET_KEY
    message = f"{scope}{resource_id}:{user_id}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def generate_stream_token(track_id: int, user_id: int, *, scope: str = "") -> str:
    expires = int(time.time()) + 3600
    sig = stream_signature(track_id, user_id, expires, scope=scope)
    return f"user_id={user_id}&expires={expires}&sig={sig}"

def add_stream_links(tracks: list[dict], user: dict | None, response: Response) -> list[dict]:
    response.headers["Cache-Control"] = "private, no-store"
    if not user:
        return tracks
    user_id = int(user["id"])
    return [dict(track, stream_url=f'/api/stream/{track["id"]}?{generate_stream_token(track["id"], user_id)}')
            for track in tracks]

@app.get("/api/get_stream_url/{track_id}")
async def api_get_stream_url(track_id: int, request: Request, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
        
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
        
    track = await get_track_by_id(track_id, approved_only=True)
    if not track:
        return {"error": "Трек не найден или на модерации"}
        
    file_path = Path(track["file_path"])
    if not file_path.exists(): return {"error": "Файл утерян"}
        
    token = generate_stream_token(track_id, int(user["id"]))
    return {"url": f"/api/stream/{track_id}?{token}"}

@app.get("/api/stream/{track_id}")
async def api_stream(track_id: int, request: Request):
    user_id_str = request.query_params.get("user_id")
    expires_str = request.query_params.get("expires")
    sig = request.query_params.get("sig")
    
    if not user_id_str or not expires_str or not sig:
        return Response(content="Missing signature", status_code=403)
        
    try:
        expires = int(expires_str)
        user_id = int(user_id_str)
    except ValueError:
        return Response(content="Invalid parameters", status_code=403)
        
    if expires < int(time.time()):
        return Response(content="Link expired", status_code=403)
        
    expected_sig = stream_signature(track_id, user_id, expires)
    
    if not hmac.compare_digest(expected_sig, sig):
        return Response(content="Invalid signature", status_code=403)
        
    track = await get_track_by_id(track_id, approved_only=True)
    if not track: return {"error": "Трек не найден"}
    
    file_path = Path(track["file_path"])
    if not file_path.exists(): return {"error": "Файл утерян"}
    
    return FileResponse(file_path, media_type="audio/mpeg")

@app.get("/api/profile")
async def get_profile(request: Request):
    telegram_user = get_tg_user(request)
    if not telegram_user: return {"error": "Нет данных от Telegram"}
    
    user = await get_user(telegram_user.id)
    if not user: return {"error": "Пользователь не найден"}
    if user["username"] != telegram_user.username:
        await set_public_username(user["id"], telegram_user.username)
    
    level_info = get_level_progress(user["xp"])
    premium_expiry = parse_premium_expiry(user.get("premium_expires_at"))
    
    unlocked = []
    try:
        unlocked = json.loads(user.get("unlocked_themes", '["dark"]'))
    except:
        unlocked = ["dark"]
        
    _, decorations = await get_profile_decorations(user["id"])
    return {
        "id": user["telegram_id"],
        "points": user["points"],
        "xp": user["xp"],
        "level": user["level"],
        "is_premium": is_premium_active(user),
        "premium_expires_at": int(premium_expiry.timestamp()) if premium_expiry else None,
        "level_progress": level_info,
        "active_theme": user.get("active_theme", "dark"),
        "unlocked_themes": unlocked,
        "loop_playlist": bool(user.get("loop_playlist", 0)),
        "decorations": decorations,
        "is_admin": user["telegram_id"] == ADMIN_ID
    }

@app.post("/api/claim_daily")
async def api_claim_daily(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
        
    result = await claim_daily(int(user["id"]))
    return result

@app.post("/api/buy_premium")
async def api_buy_premium(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}

    prices = [LabeledPrice(label="Премиум на 30 дней", amount=1)]
    
    try:
        invoice_link = await bot.create_invoice_link(
            title="Премиум подписка",
            description="Снимает все лимиты на скачивание треков на 30 дней!",
            payload=f"premium_30_days_{tg_id}",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        return {"link": invoice_link}
    except Exception as e:
        logger.error(f"Ошибка создания счета: {e}")
        return {"error": str(e)}

@app.get("/api/search")
async def api_search(request: Request, response: Response):
    query = request.query_params.get("q", "").strip()
    if not query: return {"error": "Пустой запрос"}
        
    tracks = await search_tracks(query, limit=10)
    artist_pages = await get_track_artist_pages(tracks)
    
    tg_id = await get_tg_user_id(request)
    liked_ids = []
    user = None
    if tg_id:
        user = await get_user(tg_id)
        if user:
            liked_ids = await get_user_liked_track_ids(int(user["id"]))

    result = []
    for t in tracks:
        quality = t["quality_status"] if t["quality_status"] != 'none' else 'Verified'
        pages = artist_pages[t["id"]]
        result.append({
            "id": t["id"], "artist": t["artist"], "title": t["title"], "quality": quality,
            "is_liked": t["id"] in liked_ids, "page_artists": pages, "page_artist": pages[0] if pages else "",
        })
        
    similar_artists = []
    if result and result[0]["page_artist"]:
        main_artist = result[0]["page_artist"]
        similar_artists = await get_similar_artists(main_artist, limit=5)
        
    return {"tracks": add_stream_links(result, user, response), "similar_artists": similar_artists}

@app.get("/api/trending_artists")
async def api_get_trending_artists(all_artists: bool = False):
    artists = await get_trending_artists(limit=None if all_artists else 7)
    if not all_artists:
        return {"artists": artists[:6], "has_more": len(artists) > 6}
    return {"artists": artists}


async def read_upload(request: Request, max_bytes: int) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > max_bytes:
            raise HTTPException(413, f"Максимальный размер файла — {max_bytes // (1024 * 1024)} МБ.")
        data.extend(chunk)
    if not data:
        raise HTTPException(400, "Выберите файл.")
    return bytes(data)


def make_artist_cover(content: bytes) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(content), formats=["JPEG", "PNG", "WEBP"]) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Выберите изображение размером до 16 мегапикселей.")
        image = ImageOps.exif_transpose(image)
        output = BytesIO()
        ImageOps.fit(image.convert("RGB"), (512, 512)).save(output, format="JPEG", quality=88)
        return output.getvalue()


@app.get("/api/artist_cover")
async def api_get_artist_cover(artist: str):
    jpeg = await get_artist_cover(artist)
    if jpeg is None:
        raise HTTPException(404, "Обложка артиста не найдена.")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.put("/api/admin/artist_cover")
async def api_set_artist_cover(artist: str, request: Request):
    if await get_tg_user_id(request) != ADMIN_ID:
        raise HTTPException(403, "Обложку может менять только администратор.")
    if not artist.strip() or len(artist) > 300:
        raise HTTPException(400, "Некорректное имя артиста.")
    content = await read_upload(request, 5 * 1024 * 1024)
    try:
        jpeg = await asyncio.to_thread(make_artist_cover, content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, "Выберите изображение JPG, PNG или WebP.") from exc
    if not await set_artist_cover(artist, jpeg):
        raise HTTPException(404, "Артист не найден.")
    return {"ok": True}

@app.get("/api/wave")
async def api_get_wave(request: Request, response: Response):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    tracks = await get_wave_recommendations(int(user["id"]), limit=30)
    liked_ids = await get_user_liked_track_ids(int(user["id"]))
    
    result = []
    for t in tracks:
        quality = t["quality_status"] if t["quality_status"] != 'none' else 'Verified'
        result.append({
            "id": t["id"], 
            "artist": t["artist"], 
            "title": t["title"], 
            "quality": quality,
            "is_liked": t["id"] in liked_ids
        })
        
    return {"tracks": add_stream_links(result, user, response)}

async def artist_stream_tracks(tracks: list[dict], request: Request, response: Response) -> list[dict]:
    tg_id = await get_tg_user_id(request)
    user = await get_user(tg_id) if tg_id else None
    liked_ids = set(await get_user_liked_track_ids(int(user["id"]))) if user else set()
    return add_stream_links([
        {
            "id": track["id"], "artist": track["artist"], "title": track["title"],
            "quality": track["quality_status"] if track["quality_status"] not in (None, "none") else "Verified",
            "is_liked": track["id"] in liked_ids,
        }
        for track in tracks
    ], user, response)


@app.get("/api/artist_tracks")
async def api_get_artist_tracks(
    request: Request, response: Response, artist: str = "",
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=2**63 - 1),
):
    if not artist.strip():
        return {"error": "Пустой запрос"}
    tracks, profile = await get_artist_tracks(artist, limit=limit, offset=offset)
    total = profile["track_count"]
    next_offset = offset + len(tracks)
    return {
        "tracks": await artist_stream_tracks(tracks, request, response),
        "profile": profile,
        "total": total,
        "next_offset": next_offset if next_offset < total else None,
    }


@app.get("/api/artist_albums")
async def api_get_artist_albums(artist: str = Query(min_length=1)):
    return {"albums": await get_artist_albums(artist)}


@app.get("/api/artist_albums/{album_id}")
async def api_get_artist_album(album_id: str, request: Request, response: Response,
                               artist: str = Query(min_length=1)):
    album = await get_artist_album(artist, album_id)
    if album is None:
        raise HTTPException(404, "Релиз не найден.")
    rows = album["tracks"]
    available = await artist_stream_tracks([
        {"id": row["id"], "title": row["local_title"], "artist": row["local_artist"],
         "quality_status": row["quality_status"]}
        for row in rows if row["id"] is not None
    ], request, response)
    by_id = {track["id"]: track for track in available}
    album["tracks"] = [
        dict(disc=row["disc"], position=row["position"], title=row["title"], artist=row["artist"],
             duration_ms=row["duration_ms"], track=by_id.get(row["id"]))
        for row in rows
    ]
    return {"album": album}


@app.get("/api/artist_albums/{album_id}/cover")
async def api_get_artist_album_cover(album_id: str, artist: str = Query(min_length=1)):
    image = await get_artist_album_cover(artist, album_id)
    if image is None:
        raise HTTPException(404, "Обложка не найдена.")
    data, mime = image
    return Response(content=data, media_type=mime, headers={"Cache-Control": "public, max-age=3600"})

@app.post("/api/download/{track_id}")
async def api_download(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
        
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
            
    limit_info = await check_download_limit(int(user["id"]))
    if not limit_info["allowed"]: return {"error": limit_info["msg"]}
            
    track = await get_track_by_id(track_id, approved_only=True)
    if not track:
        return {"error": "Трек не найден или на модерации"}
            
    quality = track["quality_status"] if track["quality_status"] != 'none' else 'Verified'
    caption = f"🎵 {esc(track['artist'])} — {esc(track['title'])}\n⭐ Качество: {esc(str(quality))}"
    
    if track.get("file_id"):
        # Если файл уже в кеше, обложку прикрепить нельзя
        await bot.send_audio(
            chat_id=tg_id,
            audio=track["file_id"],
            caption=caption,
            title=track["title"],
            performer=track["artist"]
        )
    else:
        file_path = Path(track["file_path"])
        if not file_path.exists():
            return {"error": "Файл утерян"}
            
        # Достаем обложку и передаем её явно
        cover_bytes = await asyncio.to_thread(extract_telegram_thumbnail, str(file_path))
        thumbnail = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None
        
        msg = await bot.send_audio(
            chat_id=tg_id,
            audio=FSInputFile(file_path),
            thumbnail=thumbnail, # <--- ГЛАВНАЯ МАГИЯ
            caption=caption,
            title=track["title"],
            performer=track["artist"]
        )
        
        if msg.audio and msg.audio.file_id:
            await set_track_file_id(track["id"], msg.audio.file_id)
        
    if not limit_info["is_premium"]:
        await increment_download_count(int(user["id"]), 1)
    await increment_track_download(track_id)
        
    if track['uploaded_by'] != int(user["id"]):
        await add_points(track['uploaded_by'], 1, f"track_download_{track_id}")
    return {"ok": True}

@app.get("/api/cover/{track_id}")
async def api_cover(track_id: int):
    fallback_url = "https://cdn-icons-png.flaticon.com/512/3844/3844724.png"
    
    track = await get_track_by_id(track_id, approved_only=True)
    if not track: return RedirectResponse(url=fallback_url)

    cache_dir = Path("uploads/covers")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_jpg = cache_dir / f"track_{track_id}.jpg"
    cached_png = cache_dir / f"track_{track_id}.png"
    
    if cached_jpg.exists():
        return FileResponse(cached_jpg, media_type="image/jpeg")
    if cached_png.exists():
        return FileResponse(cached_png, media_type="image/png")
    
    file_path = Path(track["file_path"])
    if not file_path.exists(): return RedirectResponse(url=fallback_url)
    
    try:
        audio = await asyncio.to_thread(MutagenFile, file_path)
        if not audio or not audio.tags: 
            return RedirectResponse(url=fallback_url)
        
        cover_data = None
        mime_type = "image/jpeg"
        
        if hasattr(audio.tags, 'getall'):
            apic_frames = audio.tags.getall('APIC')
            if apic_frames:
                cover_data = apic_frames[0].data
                mime_type = apic_frames[0].mime
        elif 'covr' in audio.tags:
            cover_data = bytes(audio.tags['covr'][0])
            mime_type = "image/png"
        elif hasattr(audio, 'pictures') and audio.pictures:
            cover_data = audio.pictures[0].data
            mime_type = audio.pictures[0].mime
            
        if cover_data:
            if "png" in mime_type.lower():
                cached_png.write_bytes(cover_data)
            else:
                cached_jpg.write_bytes(cover_data)
                
            return Response(content=cover_data, media_type=mime_type)
            
    except Exception as e:
        logger.error(f"Ошибка получения обложки: {e}")
        
    return RedirectResponse(url=fallback_url)

@app.post("/api/playlists/import-instructions")
async def api_send_playlist_import_instructions(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id:
        raise HTTPException(401, "Нет данных от Telegram")

    try:
        await bot.send_message(chat_id=tg_id, text=PLAYLIST_IMPORT_INSTRUCTIONS, request_timeout=15)
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить инструкцию по импорту плейлиста: %s", type(exc).__name__)
        raise HTTPException(502, "Не удалось отправить инструкцию в Telegram") from exc

    return {"ok": True}


class SyncLink(BaseModel):
    url: str = Field(max_length=2048)


class SyncSettings(SyncLink):
    destination_id: int | None = Field(default=None, gt=0)
    new_title: str | None = Field(default=None, max_length=120)


class SyncAction(BaseModel):
    action: Literal["pause", "resume", "disconnect"]


sync_preview_slots = asyncio.Semaphore(2)


@app.exception_handler(playlist_sync.SyncError)
async def sync_error_response(_request: Request, exc: playlist_sync.SyncError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


async def sync_premium_user(user: dict = Depends(current_user)) -> dict:
    if not is_premium_active(user):
        raise HTTPException(403, "Для автосинхронизации нужен активный Premium.")
    return user


@app.get("/api/playlist-sync")
async def api_playlist_sync(user: dict = Depends(current_user)):
    return {"is_premium": is_premium_active(user), "source": await playlist_sync.get_user_source(user["id"])}


@app.post("/api/playlist-sync/preview")
async def api_preview_playlist_sync(link: SyncLink, _user: dict = Depends(sync_premium_user)):
    async with sync_preview_slots:
        return await playlist_sync.preview_source(link.url)


async def save_playlist_sync(user_id: int, sync_id: int | None, settings: SyncSettings):
    if settings.destination_id is not None and settings.new_title is not None:
        raise HTTPException(400, "Выберите одно место для добавления треков.")
    current = await playlist_sync.get_user_source(user_id, sync_id)
    if sync_id is not None and not current:
        raise HTTPException(404, "Автосинхронизация не найдена.")
    if sync_id is None and current:
        raise HTTPException(409, "Автосинхронизация уже подключена. Откройте её настройки.")
    if current and current["source_url"] == settings.url:
        preview = current
    else:
        async with sync_preview_slots:
            preview = await playlist_sync.preview_source(settings.url)
    sync_id = await playlist_sync.save_source(user_id, sync_id, preview, settings.destination_id, settings.new_title)
    return {"source": await playlist_sync.get_user_source(user_id, sync_id)}


@app.post("/api/playlist-sync")
async def api_create_playlist_sync(settings: SyncSettings, user: dict = Depends(sync_premium_user)):
    return await save_playlist_sync(user["id"], None, settings)


@app.put("/api/playlist-sync/{sync_id}")
async def api_update_playlist_sync(sync_id: int, settings: SyncSettings, user: dict = Depends(sync_premium_user)):
    return await save_playlist_sync(user["id"], sync_id, settings)


@app.patch("/api/playlist-sync/{sync_id}")
async def api_manage_playlist_sync(sync_id: int, action: SyncAction, user: dict = Depends(current_user)):
    await playlist_sync.manage_source(user["id"], sync_id, action.action)
    return {"ok": True}


@app.post("/api/playlists/create")
async def api_create_playlist(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    data = await request.json()
    title = data.get("title", "").strip()
    if not title: return {"error": "Название не может быть пустым"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
        
    result = await create_user_playlist(int(user["id"]), title)
    return result

@app.get("/api/playlists")
async def api_get_playlists(request: Request, exclude_track_id: int = None):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
        
    playlists = await get_user_playlists(int(user["id"]), exclude_track_id)
    return {"playlists": playlists}

@app.post("/api/playlists/{playlist_id}/add")
async def api_add_to_playlist(playlist_id: int, request: Request, pl: dict = Depends(owned_playlist)):
    data = await request.json()
    track_id = data.get("track_id")
    if not track_id: return {"error": "Нет ID трека"}
    
    success = await add_track_to_playlist(pl["id"], track_id)
    if success:
        return {"ok": True, "msg": "Трек добавлен"}
    else:
        return {"error": "Трек уже есть в этом плейлисте"}

@app.get("/api/playlists/{playlist_id}")
async def api_get_playlist_tracks(playlist_id: int, response: Response, pl: dict = Depends(owned_playlist)):
    tracks = await get_playlist_tracks(pl["id"])
    preview = await playlist_trailer_source(await get_playlist_market_preview(playlist_id))
    mode = market_preview_mode(preview)
    selected = json.loads(preview["preview_ids"] or "[]") if preview else []
    available = {
        track["id"] for track in tracks
        if track["id"] in selected and track["file_path"] and Path(track["file_path"]).is_file()
    }
    return {
        "tracks": add_stream_links(tracks, {"id": pl["user_id"]}, response),
        "playlist": pl,
        "preview_mode": mode,
        "preview_ids": [track_id for track_id in selected if track_id in available],
        "trailer_url": trailer_url(playlist_id, pl["user_id"]) if mode == "trailer" else None,
    }

@app.delete("/api/playlists/{playlist_id}/track/{track_id}")
async def api_remove_track_from_playlist(playlist_id: int, track_id: int, pl: dict = Depends(owned_playlist)):
    await remove_track_from_playlist(pl["id"], track_id)
    return {"ok": True, "msg": "Трек удален из плейлиста"}

@app.post("/api/track/report/{track_id}")
async def api_report_track(track_id: int):
    await report_track(track_id)
    return {"ok": True, "msg": "Жалоба отправлена. Спасибо, что помогаете бороться с цензурой!"}

@app.post("/api/playlists/{playlist_id}/request_trailer")
async def api_request_trailer(playlist_id: int, request: Request, pl: dict = Depends(owned_playlist)):
    if pl["original_playlist_id"] is not None:
        raise HTTPException(403, "Трейлер может менять только автор плейлиста.")
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    playlist = pl

    try:
        await bot.send_message(
            chat_id=tg_id,
            text=f"🎵 Отправь мне аудиофайл для трейлера плейлиста <b>{esc(playlist['title'])}</b> (ID: {playlist['id']})\n\n"
                 f"⚠️ <b>В ответ на это сообщение!</b> (нажми на него и выбери 'Ответить', затем прикрепи MP3 или файл).\n"
                 f"Размер файла: до 3 МБ. Длительность: до 30 секунд."
        )
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка запроса трейлера: {e}")
        return {"error": "Не удалось отправить сообщение в Telegram"}

def market_preview_mode(playlist: dict | None) -> str | None:
    if not playlist:
        return None
    if playlist["preview_mode"] == "tracks":
        return "tracks"
    path = playlist["trailer_path"]
    return "trailer" if path and Path(path).is_file() else None


async def playlist_trailer_source(playlist: dict | None) -> dict | None:
    if playlist and playlist["original_playlist_id"] and not market_preview_mode(playlist):
        return await get_playlist_market_preview(playlist["original_playlist_id"])
    return playlist


def trailer_url(playlist_id: int, user_id: int) -> str:
    return f"/api/stream_trailer/{playlist_id}?{generate_stream_token(playlist_id, user_id, scope='trailer:')}"


@app.get("/api/stream_trailer/{playlist_id}")
async def api_stream_trailer(
    playlist_id: int, user_id: int | None = None, expires: int | None = None, sig: str = "",
):
    playlist = await get_playlist_market_preview(playlist_id)
    if not playlist:
        raise HTTPException(404, "Трейлер не найден.")
    if not playlist["is_public"] or playlist["price"] <= 0 or playlist["original_playlist_id"]:
        if user_id != playlist["user_id"] or expires is None or expires < time.time():
            raise HTTPException(404, "Трейлер не найден.")
        expected = stream_signature(playlist_id, user_id, expires, scope="trailer:")
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(404, "Трейлер не найден.")
    playlist = await playlist_trailer_source(playlist)
    if market_preview_mode(playlist) != "trailer":
        raise HTTPException(404, "Трейлер не найден.")
    return FileResponse(playlist["trailer_path"])


@app.put("/api/playlists/{playlist_id}/preview/trailer")
async def api_upload_playlist_trailer(playlist_id: int, request: Request, pl: dict = Depends(owned_playlist)):
    if pl["original_playlist_id"] is not None:
        raise HTTPException(403, "Трейлер может менять только автор плейлиста.")
    content = await read_upload(request, TRAILER_MAX_BYTES)
    try:
        await upload_playlist_trailer(playlist_id, pl["user_id"], content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"ok": True}


@app.put("/api/playlists/{playlist_id}/preview/tracks")
async def api_set_preview_tracks(
    playlist_id: int, track_ids: list[int] = Body(embed=True), pl: dict = Depends(owned_playlist),
):
    try:
        await set_playlist_preview_tracks(playlist_id, pl["user_id"], track_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"ok": True}

@app.post("/api/playlists/{playlist_id}/request_cover")
async def api_request_cover(playlist_id: int, request: Request, pl: dict = Depends(owned_playlist)):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    playlist = pl

    try:
        await bot.send_message(
            chat_id=tg_id,
            text=f"🖼 Отправь мне картинку для обложки плейлиста <b>{esc(playlist['title'])}</b> (ID: {playlist['id']})\n\n"
                 f"⚠️ <b>В ответ на это сообщение!</b> (нажми на него и выбери 'Ответить', затем прикрепи фото).\n"
                 f"Размер файла: до 5 МБ."
        )
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка запроса обложки: {e}")
        return {"error": "Не удалось отправить сообщение в Telegram"}

@app.get("/api/stream_playlist_cover/{playlist_id}")
async def api_stream_playlist_cover(playlist_id: int):
    playlist = await get_playlist_by_id(playlist_id)
    if playlist and playlist.get("custom_cover"):
        file_path = Path(playlist["custom_cover"])
        if file_path.exists():
            return FileResponse(file_path)
    
    if playlist and playlist.get("original_playlist_id"):
        orig_playlist = await get_playlist_by_id(playlist["original_playlist_id"])
        if orig_playlist and orig_playlist.get("custom_cover"):
            orig_path = Path(orig_playlist["custom_cover"])
            if orig_path.exists():
                return FileResponse(orig_path)

    tracks = await get_playlist_tracks(playlist_id)
    if tracks:
        return RedirectResponse(url=f"/api/cover/{tracks[0]['id']}")
    
    return RedirectResponse(url="https://cdn-icons-png.flaticon.com/512/3844/3844724.png")

@app.delete("/api/admin/delete_track/{track_id}")
async def api_admin_delete_track(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: 
        return {"error": "Нет прав"}
    success = await delete_track(track_id)
    if success:
        return {"ok": True, "msg": "Трек удален"}
    return {"error": "Не удалось удалить"}

@app.post("/api/market/sell/{playlist_id}")
async def api_sell_playlist(playlist_id: int, request: Request, pl: dict = Depends(owned_playlist)):
    data = await request.json()
    price = data.get("price", 0)
    if price <= 0: return {"error": "Цена должна быть больше 0"}
    
    try:
        await set_playlist_price(pl["id"], price)
        return {"ok": True, "msg": "Плейлист выставлен на продажу"}
    except ValueError as e:
        return {"error": str(e)}

@app.get("/api/market")
async def api_get_market():
    tracks = await get_market_playlists()
    return {"playlists": tracks}


@app.get("/api/market/playlists/{playlist_id}")
async def api_get_market_playlist(playlist_id: int, user: dict = Depends(current_user)):
    playlist = await get_playlist_market_preview(playlist_id, user["id"])
    if not playlist or not playlist["is_public"] or playlist["price"] <= 0:
        raise HTTPException(404, "Плейлист больше не продаётся.")
    tracks = await get_playlist_tracks(playlist_id)
    mode = market_preview_mode(playlist)
    entries = [{"id": track["id"], "title": track["title"], "artist": track["artist"]} for track in tracks]
    playable = {
        track["id"]: entry for track, entry in zip(tracks, entries)
        if track["file_path"] and Path(track["file_path"]).is_file()
    }
    preview_ids = json.loads(playlist["preview_ids"] or "[]") if mode == "tracks" else []
    preview_tracks = [playable[track_id] for track_id in preview_ids if track_id in playable]
    return {
        "id": playlist_id, "title": playlist["title"], "price": playlist["price"],
        "username": playlist["username"], "is_owner": playlist["user_id"] == user["id"],
        "owned_id": playlist["bought_id"], "tracks": entries,
        "preview_tracks": preview_tracks if len(preview_tracks) == 3 else [],
        "preview_incomplete": mode == "tracks" and len(preview_tracks) != 3,
        "trailer_url": trailer_url(playlist_id, user["id"]) if mode == "trailer" else None,
    }


@app.get("/api/market/decorations")
async def api_get_decorations(user: dict = Depends(current_user)):
    owned, equipped = await get_profile_decorations(user["id"])
    items = [
        {"id": key, **item, "owned": key in owned}
        for key, item in PROFILE_DECORATIONS.items()
    ]
    return {"items": items, "equipped": equipped}


@app.post("/api/market/decorations/{decoration_id}")
async def api_change_decoration(
    decoration_id: str, action: Literal["buy", "equip", "unequip"] = Body(embed=True),
    user: dict = Depends(current_user),
):
    if decoration_id not in PROFILE_DECORATIONS:
        raise HTTPException(404, "Украшение не найдено.")
    try:
        await change_profile_decoration(user["id"], decoration_id, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}

@app.post("/api/market/buy/{playlist_id}")
async def api_buy_playlist(playlist_id: int, request: Request, expected_price: int | None = Body(None, embed=True)):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    result = await buy_playlist(int(user["id"]), playlist_id, expected_price)
    
    if result.get("ok"):
        try:
            seller_tg = result.get("seller_tg")
            if seller_tg:
                await bot.send_message(
                    seller_tg, 
                    f"💰 Твой плейлист купили!\nТебе начислено {result['seller_cut']} поинтов."
                )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление: {e}")
            
    return result

@app.post("/api/playlists/{playlist_id}/clear")
async def api_clear_playlist(playlist_id: int, user: dict = Depends(current_user)):
    if not await clear_user_playlist(playlist_id, user["id"]):
        raise HTTPException(404, "Плейлист не найден или нет прав")
    return {"ok": True}


@app.post("/api/likes/clear")
async def api_clear_likes(user: dict = Depends(current_user)):
    await clear_user_likes(user["id"])
    return {"ok": True}


@app.delete("/api/playlists/{playlist_id}")
async def api_delete_playlist(playlist_id: int, pl: dict = Depends(owned_playlist)):
    await delete_user_playlist(pl["id"])
    return {"ok": True, "msg": "Плейлист удален"}

@app.post("/api/market/remove/{playlist_id}")
async def api_remove_from_market(playlist_id: int, pl: dict = Depends(owned_playlist)):
    await remove_playlist_from_market(pl["id"])
    return {"ok": True, "msg": "Плейлист снят с продажи"}

@app.post("/api/like/{track_id}")
async def api_toggle_like(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    is_liked = await toggle_like(int(user["id"]), track_id)
    return {"ok": True, "is_liked": is_liked}

@app.get("/api/likes")
async def api_get_likes(request: Request, response: Response):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    tracks = await get_user_liked_tracks(int(user["id"]))
    return {"tracks": add_stream_links(tracks, user, response)}

@app.get("/api/stats")
async def api_get_stats(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    stats = await get_user_stats(int(user["id"]))
    return stats

@app.post("/api/settings")
async def api_update_settings(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    data = await request.json()
    active_theme = data.get("active_theme")
    loop_playlist = data.get("loop_playlist")
    
    await update_user_settings(int(user["id"]), active_theme, loop_playlist)
    return {"ok": True}

@app.post("/api/themes/buy")
async def api_buy_theme(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    data = await request.json()
    theme_name = data.get("theme_name")
    
    result = await buy_theme(int(user["id"]), theme_name, 50)
    return result

# ==========================================
# АДМИН-ПАНЕЛЬ (API)
# ==========================================
@app.get("/api/admin/pending")
async def api_admin_pending(request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: return {"error": "Нет прав"}
    
    tracks = await get_pending_tracks(limit=20)
    return {"tracks": tracks}

@app.post("/api/admin/approve/{track_id}")
async def api_admin_approve(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: return {"error": "Нет прав"}
    
    user = await get_user(tg_id)
    await update_track_status(track_id, "approved", int(user["id"]))
    return {"ok": True, "msg": "Трек одобрен"}

@app.post("/api/admin/reject/{track_id}")
async def api_admin_reject(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: return {"error": "Нет прав"}
    
    await delete_track(track_id)
    return {"ok": True, "msg": "Трек отклонен и удален"}


@app.get("/api/admin/stats")
async def api_admin_stats(request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: return {"error": "Нет прав"}
    
    stats = await get_admin_stats()
    return stats


# ==========================================
# АДМИН-ПАНЕЛЬ: ПРЕВЬЮ, ПОЛНЫЙ ТРЕК И SOUNDCLOUD
# ==========================================

@app.get("/api/admin/preview/{track_id}")
async def api_admin_preview(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: raise HTTPException(403, "Нет прав")
    
    track = await get_track_by_id(track_id)
    if not track or not track["file_path"]: raise HTTPException(404, "Файл не найден")
    
    file_path = Path(track["file_path"])
    if not file_path.exists(): raise HTTPException(404, "Файл физически отсутствует")
    
    duration = track["duration"]
    if duration < 30:
        return FileResponse(file_path, media_type="audio/mpeg")
        
    preview_path = file_path.with_name(f"preview_{file_path.name}")
    if preview_path.exists():
        return FileResponse(preview_path, media_type="audio/mpeg")
        
    # Генерируем превью на лету
    mid_start, mid_end, end_start = (duration // 2) - 5, (duration // 2) + 5, duration - 10
    cmd = [
        str(FFMPEG_PATH), "-y", "-i", str(file_path),
        "-filter_complex",
        f"[0:a]atrim=start=0:end=10,asetpts=PTS-STARTPTS[a1]; [0:a]atrim=start={mid_start}:end={mid_end},asetpts=PTS-STARTPTS[a2]; [0:a]atrim=start={end_start}:end={duration},asetpts=PTS-STARTPTS[a3]; [a1][a2][a3]concat=n=3:v=0:a=1[outa]",
        "-map", "[outa]", "-c:a", "libmp3lame", str(preview_path)
    ]
    
    try:
        await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if preview_path.exists():
            return FileResponse(preview_path, media_type="audio/mpeg")
        return FileResponse(file_path, media_type="audio/mpeg")
    except Exception:
        return FileResponse(file_path, media_type="audio/mpeg")

@app.get("/api/admin/full/{track_id}")
async def api_admin_full(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: raise HTTPException(403, "Нет прав")
    
    track = await get_track_by_id(track_id)
    if not track or not track["file_path"]: raise HTTPException(404, "Файл не найден")
    
    file_path = Path(track["file_path"])
    if not file_path.exists(): raise HTTPException(404, "Файл отсутствует")
    return FileResponse(file_path, media_type="audio/mpeg")

@app.post("/api/admin/soundcloud/{track_id}")
async def api_admin_soundcloud(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: return {"error": "Нет прав"}
    
    track = await get_track_by_id(track_id)
    if not track: return {"error": "Трек не найден"}
    
    query = f"{track['artist']} - {track['title']}"
    link = await asyncio.to_thread(find_soundcloud_link, query)
    
    if link:
        return {"ok": True, "link": link}
    return {"error": "Не удалось найти трек на SoundCloud"}

@app.post("/api/offline/liked")
async def api_create_offline_liked(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    if not is_premium_active(user):
        return {"error": "Оффлайн-плейлисты доступны только с Premium!"}

    playlist_id = await sync_liked_playlist(int(user["id"]))

    # Отправляем заявку воркеру на создание оффлайн-темы
    chat = await get_or_create_telegram_chat(int(user["id"]), tg_id)
    if chat is None or not await create_telegram_topic(chat["id"], playlist_id, LIKED_PLAYLIST_TITLE):
        return {"error": "Оффлайн-плейлисты доступны только с Premium!"}
    
    return {"ok": True, "msg": "Тема для Лайков создается! Зайдите в чат с ботом через минуту."}

@app.post("/api/offline/{playlist_id}")
async def api_create_offline_playlist(playlist_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    if not is_premium_active(user):
        return {"error": "Оффлайн-плейлисты доступны только с Premium!"}
        
    playlist = await get_playlist_by_id(playlist_id)
    if not playlist or playlist["user_id"] != user["id"]:
        return {"error": "Плейлист не найден"}
        
    chat = await get_or_create_telegram_chat(int(user["id"]), tg_id)
    if chat is None or not await create_telegram_topic(chat["id"], playlist_id, playlist["title"]):
        return {"error": "Оффлайн-плейлисты доступны только с Premium!"}
    
    return {"ok": True, "msg": "Тема создается! Зайдите в чат с ботом через минуту."}
# ==========================================



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
