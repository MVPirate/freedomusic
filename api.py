from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging
from html import escape as esc
from pathlib import Path
from mutagen import File as MutagenFile
import hmac
import hashlib
import time

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, LabeledPrice
from aiogram.utils.web_app import safe_parse_webapp_init_data

from backend.config import TELEGRAM_BOT_TOKEN, ADMIN_ID, STREAM_SECRET_KEY
from backend.database import (
    get_user, claim_daily, search_tracks, check_download_limit, 
    increment_download_count, get_track_by_id, increment_track_download, 
    add_points, get_user_by_internal_id, delete_track,
    create_user_playlist, get_user_playlists, add_track_to_playlist, get_playlist_tracks,
    get_level_progress, set_playlist_price, get_market_playlists, buy_playlist,
    delete_user_playlist, remove_playlist_from_market,
    toggle_like, get_user_liked_tracks, get_user_liked_track_ids, get_user_stats,
    report_track, remove_track_from_playlist, get_playlist_by_id,
    set_playlist_custom_cover, get_similar_artists, get_trending_tracks,
    get_trending_artists, get_tracks_by_artist, set_track_file_id,
    update_user_settings, buy_theme,
    get_wave_recommendations,
    is_premium_active,get_pending_tracks, update_track_status, delete_track
)

logger = logging.getLogger(__name__)

app = FastAPI()
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

async def get_tg_user_id(request: Request) -> int | None:
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
        if parsed.user:
            return parsed.user.id
        return None
    except Exception as e:
        logger.warning(f"Попытка подделки initData или ошибка парсинга: {e}")
        return None

# --- ЗАВИСИМОСТИ БЕЗОПАСНОСТИ ---
async def current_user(request: Request) -> dict:
    tg_id = await get_tg_user_id(request)
    if not tg_id: raise HTTPException(401, "Нет данных от Telegram")
    user = await get_user(tg_id)
    if not user: raise HTTPException(401, "Пользователь не найден")
    return user

async def owned_playlist(playlist_id: int, user: dict = Depends(current_user)) -> dict:
    pl = await get_playlist_by_id(playlist_id)
    if not pl or pl["user_id"] != user["id"]:
        raise HTTPException(404, "Плейлист не найден или нет прав")
    return pl
# ----------------------------------

def generate_stream_token(track_id: int, user_id: int) -> str:
    expires = int(time.time()) + 3600
    message = f"{track_id}:{user_id}:{expires}".encode('utf-8')
    sig = hmac.new(STREAM_SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()
    return f"user_id={user_id}&expires={expires}&sig={sig}"

@app.get("/api/get_stream_url/{track_id}")
async def api_get_stream_url(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
        
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
        
    track = await get_track_by_id(track_id)
    if not track or (track["status"] != "approved" and track["uploaded_by"] != user["id"]):
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
        
    message = f"{track_id}:{user_id}:{expires}".encode('utf-8')
    expected_sig = hmac.new(STREAM_SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()
    
    if not hmac.compare_digest(expected_sig, sig):
        return Response(content="Invalid signature", status_code=403)
        
    track = await get_track_by_id(track_id)
    if not track: return {"error": "Трек не найден"}
    
    file_path = Path(track["file_path"])
    if not file_path.exists(): return {"error": "Файл утерян"}
    
    return FileResponse(file_path, media_type="audio/mpeg")

@app.get("/api/profile")
async def get_profile(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    level_info = get_level_progress(user["xp"])
    
    import json
    unlocked = []
    try:
        unlocked = json.loads(user.get("unlocked_themes", '["dark"]'))
    except:
        unlocked = ["dark"]
        
    return {
        "id": user["telegram_id"],
        "points": user["points"],
        "xp": user["xp"],
        "level": user["level"],
        "is_premium": is_premium_active(user),
        "level_progress": level_info,
        "active_theme": user.get("active_theme", "dark"),
        "unlocked_themes": unlocked,
        "loop_playlist": bool(user.get("loop_playlist", 0)),
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
async def api_search(request: Request):
    query = request.query_params.get("q", "").strip()
    if not query: return {"error": "Пустой запрос"}
        
    tracks = await search_tracks(query, limit=10)
    
    tg_id = await get_tg_user_id(request)
    liked_ids = []
    if tg_id:
        user = await get_user(tg_id)
        if user:
            liked_ids = await get_user_liked_track_ids(int(user["id"]))

    result = []
    for t in tracks:
        quality = t["quality_status"] if t["quality_status"] != 'none' else 'Verified'
        result.append({
            "id": t["id"], "artist": t["artist"], "title": t["title"], "quality": quality,
            "is_liked": t["id"] in liked_ids
        })
        
    similar_artists = []
    if result:
        main_artist = result[0]["artist"].split(",")[0].strip()
        similar_artists = await get_similar_artists(main_artist, limit=5)
        
    return {"tracks": result, "similar_artists": similar_artists}

@app.get("/api/trending_artists")
async def api_get_trending_artists(request: Request):
    artists = await get_trending_artists(limit=10)
    return {"artists": artists}

@app.get("/api/wave")
async def api_get_wave(request: Request):
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
        
    return {"tracks": result}

@app.get("/api/artist_tracks")
async def api_get_artist_tracks(request: Request):
    artist = request.query_params.get("artist", "").strip()
    if not artist: return {"error": "Пустой запрос"}
    
    tracks = await get_tracks_by_artist(artist)
    
    tg_id = await get_tg_user_id(request)
    liked_ids = []
    if tg_id:
        user = await get_user(tg_id)
        if user:
            liked_ids = await get_user_liked_track_ids(int(user["id"]))

    result = []
    for t in tracks:
        quality = t["quality_status"] if t["quality_status"] != 'none' else 'Verified'
        result.append({
            "id": t["id"], "artist": t["artist"], "title": t["title"], "quality": quality,
            "is_liked": t["id"] in liked_ids
        })
    return {"tracks": result}

@app.post("/api/download/{track_id}")
async def api_download(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
        
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
            
    limit_info = await check_download_limit(int(user["id"]), is_premium_active(user))
    if not limit_info["allowed"]: return {"error": limit_info["msg"]}
            
    track = await get_track_by_id(track_id)
    if not track or (track["status"] != "approved" and track["uploaded_by"] != user["id"]):
        return {"error": "Трек не найден или на модерации"}
            
    quality = track["quality_status"] if track["quality_status"] != 'none' else 'Verified'
    caption = f"🎵 {esc(track['artist'])} — {esc(track['title'])}\n⭐ Качество: {esc(str(quality))}"
    
    if track.get("file_id"):
        await bot.send_audio(chat_id=tg_id, audio=track["file_id"], caption=caption, title=track['title'], performer=track['artist'])
    else:
        file_path = Path(track["file_path"])
        if not file_path.exists(): return {"error": "Файл утерян"}
        
        msg = await bot.send_audio(
            chat_id=tg_id, audio=FSInputFile(file_path),
            caption=caption, title=track['title'], performer=track['artist']
        )
        if msg.audio and msg.audio.file_id:
            await set_track_file_id(track_id, msg.audio.file_id)
        
    await increment_download_count(int(user["id"]), 1)
    await increment_track_download(track_id)
        
    if track['uploaded_by'] != int(user["id"]):
        await add_points(track['uploaded_by'], 1, f"track_download_{track_id}")
    return {"ok": True}

@app.get("/api/cover/{track_id}")
async def api_cover(track_id: int):
    fallback_url = "https://cdn-icons-png.flaticon.com/512/3844/3844724.png"
    
    cache_dir = Path("uploads/covers")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_jpg = cache_dir / f"track_{track_id}.jpg"
    cached_png = cache_dir / f"track_{track_id}.png"
    
    if cached_jpg.exists():
        return FileResponse(cached_jpg, media_type="image/jpeg")
    if cached_png.exists():
        return FileResponse(cached_png, media_type="image/png")
    
    track = await get_track_by_id(track_id)
    if not track: return RedirectResponse(url=fallback_url)
    
    file_path = Path(track["file_path"])
    if not file_path.exists(): return RedirectResponse(url=fallback_url)
    
    try:
        audio = MutagenFile(file_path)
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
async def api_get_playlist_tracks(playlist_id: int, pl: dict = Depends(owned_playlist)):
    tracks = await get_playlist_tracks(pl["id"])
    return {"tracks": tracks, "playlist": pl}

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

@app.get("/api/stream_trailer/{playlist_id}")
async def api_stream_trailer(playlist_id: int):
    playlist = await get_playlist_by_id(playlist_id)
    
    if playlist and playlist.get("trailer_path"):
        file_path = Path(playlist["trailer_path"])
        if file_path.exists():
            return FileResponse(file_path, media_type="audio/mpeg")

    if playlist and playlist.get("original_playlist_id"):
        orig_playlist = await get_playlist_by_id(playlist["original_playlist_id"])
        if orig_playlist and orig_playlist.get("trailer_path"):
            orig_path = Path(orig_playlist["trailer_path"])
            if orig_path.exists():
                return FileResponse(orig_path, media_type="audio/mpeg")

    return {"error": "Трейлер не найден"}

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

@app.post("/api/market/buy/{playlist_id}")
async def api_buy_playlist(playlist_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    result = await buy_playlist(int(user["id"]), playlist_id)
    
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
async def api_get_likes(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    tracks = await get_user_liked_tracks(int(user["id"]))
    return {"tracks": tracks}

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
# ==========================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
