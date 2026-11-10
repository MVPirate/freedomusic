from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging
from pathlib import Path
from mutagen import File as MutagenFile

from aiogram import Bot
from aiogram.types import FSInputFile, LabeledPrice
from aiogram.utils.web_app import safe_parse_webapp_init_data

from backend.config import TELEGRAM_BOT_TOKEN, ADMIN_ID # <-- ДОБАВЛЕН ADMIN_ID
from backend.database import (
    get_user, claim_daily, search_tracks, check_download_limit, 
    increment_download_count, get_track_by_id, increment_track_download, 
    add_points, get_user_by_internal_id, delete_track, # <-- ДОБАВЛЕН delete_track
    create_user_playlist, get_user_playlists, add_track_to_playlist, get_playlist_tracks,
    get_level_progress, set_playlist_price, get_market_playlists, buy_playlist,
    delete_user_playlist, remove_playlist_from_market,
    toggle_like, get_user_liked_tracks, get_user_liked_track_ids, get_user_stats,
    report_track, remove_track_from_playlist
)

logger = logging.getLogger(__name__)

app = FastAPI()
bot = Bot(token=TELEGRAM_BOT_TOKEN)

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

@app.get("/api/profile")
async def get_profile(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
    
    level_info = get_level_progress(user["xp"])
        
    return {
        "id": user["telegram_id"],
        "points": user["points"],
        "xp": user["xp"],
        "level": user["level"],
        "is_premium": bool(user["is_premium"]),
        "level_progress": level_info
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
    return {"tracks": result}

@app.post("/api/download/{track_id}")
async def api_download(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных от Telegram"}
        
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
            
    limit_info = await check_download_limit(int(user["id"]), bool(user.get("is_premium")))
    if not limit_info["allowed"]: return {"error": limit_info["msg"]}
            
    track = await get_track_by_id(track_id)
    if not track: return {"error": "Трек не найден"}
            
    file_path = Path(track["file_path"])
    if not file_path.exists(): return {"error": "Файл утерян"}
            
    quality = track["quality_status"] if track["quality_status"] != 'none' else 'Verified'
    await bot.send_audio(
        chat_id=tg_id, audio=FSInputFile(file_path),
        caption=f"🎵 {track['artist']} — {track['title']}\n⭐ Качество: {quality}",
        title=track['title'], performer=track['artist']
    )
        
    await increment_download_count(int(user["id"]), 1)
    await increment_track_download(track_id)
        
    if track['uploaded_by'] != int(user["id"]):
        await add_points(track['uploaded_by'], 1, f"track_download_{track_id}")
    return {"ok": True}

@app.get("/api/stream/{track_id}")
async def api_stream(track_id: int):
    track = await get_track_by_id(track_id)
    if not track: return {"error": "Трек не найден"}
    
    file_path = Path(track["file_path"])
    if not file_path.exists(): return {"error": "Файл утерян"}
    
    return FileResponse(file_path, media_type="audio/mpeg")

@app.get("/api/cover/{track_id}")
async def api_cover(track_id: int):
    fallback_url = "https://cdn-icons-png.flaticon.com/512/3844/3844724.png"
    
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
            return Response(content=cover_data, media_type=mime_type)
            
    except Exception as e:
        logger.error(f"Ошибка получения обложки: {e}")
        
    return RedirectResponse(url=fallback_url)

# --- ЭНДПОИНТЫ ПЛЕЙЛИСТОВ ---

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
async def api_get_playlists(request: Request):
    tg_id = await get_tg_user_id(request)
    if not tg_id: return {"error": "Нет данных"}
    
    user = await get_user(tg_id)
    if not user: return {"error": "Пользователь не найден"}
        
    playlists = await get_user_playlists(int(user["id"]))
    return {"playlists": playlists}

@app.post("/api/playlists/{playlist_id}/add")
async def api_add_to_playlist(playlist_id: int, request: Request):
    data = await request.json()
    track_id = data.get("track_id")
    if not track_id: return {"error": "Нет ID трека"}
    
    success = await add_track_to_playlist(playlist_id, track_id)
    if success:
        return {"ok": True, "msg": "Трек добавлен"}
    else:
        return {"error": "Трек уже есть в этом плейлисте"}

@app.get("/api/playlists/{playlist_id}")
async def api_get_playlist_tracks(playlist_id: int):
    tracks = await get_playlist_tracks(playlist_id)
    return {"tracks": tracks}

@app.delete("/api/playlists/{playlist_id}/track/{track_id}")
async def api_remove_track_from_playlist(playlist_id: int, track_id: int):
    await remove_track_from_playlist(playlist_id, track_id)
    return {"ok": True, "msg": "Трек удален из плейлиста"}

@app.post("/api/track/report/{track_id}")
async def api_report_track(track_id: int):
    await report_track(track_id)
    return {"ok": True, "msg": "Жалоба отправлена. Спасибо, что помогаете бороться с цензурой!"}

# --- НОВЫЙ ЭНДПОИНТ ДЛЯ АДМИНА ---
@app.delete("/api/admin/delete_track/{track_id}")
async def api_admin_delete_track(track_id: int, request: Request):
    tg_id = await get_tg_user_id(request)
    if tg_id != ADMIN_ID: 
        return {"error": "Нет прав"}
    success = await delete_track(track_id)
    if success:
        return {"ok": True, "msg": "Трек удален"}
    return {"error": "Не удалось удалить"}

# --- ЭНДПОИНТЫ МАРКЕТПЛЕЙСА ---

@app.post("/api/market/sell/{playlist_id}")
async def api_sell_playlist(playlist_id: int, request: Request):
    data = await request.json()
    price = data.get("price", 0)
    if price <= 0: return {"error": "Цена должна быть больше 0"}
    
    try:
        await set_playlist_price(playlist_id, price)
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
async def api_delete_playlist(playlist_id: int):
    await delete_user_playlist(playlist_id)
    return {"ok": True, "msg": "Плейлист удален"}

@app.post("/api/market/remove/{playlist_id}")
async def api_remove_from_market(playlist_id: int):
    await remove_playlist_from_market(playlist_id)
    return {"ok": True, "msg": "Плейлист снят с продажи"}

# --- ЭНДПОИНТЫ ДЛЯ ЛАЙКОВ ---

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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)