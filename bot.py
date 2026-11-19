"""Telegram-бот FreeDom Music Bot на aiogram 3.x (Асинхронный)."""
from __future__ import annotations

import asyncio
import re
import sys
import subprocess
import unicodedata
from datetime import datetime
from html import escape as esc
from pathlib import Path
from typing import Any

from backend.offline import offline_worker
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, 
    CallbackQuery, FSInputFile, LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, KeyboardButton, MenuButtonWebApp, 
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    InlineQuery, InlineQueryResultCachedAudio
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter
from dotenv import load_dotenv

from backend.config import TELEGRAM_BOT_TOKEN, ADMIN_ID
from backend.database import (
    get_hunt_tracks, get_user, import_playlist, init_db, upload_track, add_points,
    claim_daily, process_referral, get_pending_tracks, update_track_status,
    check_track_exists, check_download_limit, increment_download_count,
    reset_download_limit, set_daily_limit,
    search_tracks, get_track_by_id, increment_track_download,
    get_user_by_internal_id, give_premium, remove_premium,
    update_track_metadata, delete_track, replace_track_file,
    create_user_playlist, get_user_playlist_by_title, add_track_to_playlist,
    set_playlist_trailer, set_playlist_custom_cover,
    set_track_file_id, get_tracks_without_file_id,
    toggle_like, get_user_playlists, is_premium_active,
    reserve_downloads, refund_downloads
)
from backend.parser import detect_source, parse_playlist, get_yandex_client, parse_single_track
from backend.downloader import (
    download_track_from_youtube, fix_audio_tags, FFMPEG_PATH, find_soundcloud_link,
    download_from_soundcloud_url, ensure_track_has_cover, download_track_from_direct_url,download_track_smart 
)
from backend.hitmo_parser import download_from_hitmo, parse_artist_page_hitmo

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

load_dotenv()

BASE_DIR: Path = Path(__file__).resolve().parent
UPLOADS_DIR: Path = BASE_DIR / "uploads"
TRAILERS_DIR: Path = UPLOADS_DIR / "trailers"
WEBAPP_STUB_URL: str = "https://freedomusic.us.ci"

session = AiohttpSession(timeout=300)
bot: Bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp: Dispatcher = Dispatcher()

waiting_for_replacement = {}
processing_full_tracks = set()

pending_imports = {}
waiting_for_new_playlist = {}

def log(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {text}")

def _telegram_id(message: Message) -> int:
    if message.from_user is None: raise ValueError("Не удалось определить пользователя Telegram")
    return message.from_user.id

async def _ensure_user(message: Message) -> dict:
    return await get_user(_telegram_id(message))

def _is_premium(user: dict) -> bool:
    return is_premium_active(user)

async def _notify_level_up(message: Message, result: dict):
    if result.get("leveled_up"):
        await message.answer(f"🎉 <b>Новый уровень!</b>\n\nПоздравляем, теперь ты — <b>{esc(str(result['level']))}</b>!")

def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    return cleaned.strip("._") or "track"

def format_time(seconds: int) -> str:
    if not seconds: return "0 сек"
    m, s = divmod(seconds, 60)
    if m > 0: return f"{m} мин {s} сек"
    return f"{s} сек"

def parse_smart_line(raw: str) -> dict | None:
    ZW_RE   = re.compile(r'[\u200b-\u200f\u2060\ufeff]')
    URL_RE  = re.compile(r'(?:https?://|www\.|t\.me/)\S+', re.I)
    NUM_RE  = re.compile(r'^\s*\d{1,3}\s*[.)]\s+')
    TIME_RE = re.compile(r'[\s(\[]*\b\d{1,2}:\d{2}(?::\d{2})?\b[\s)\]]*$')
    JUNK_RE = re.compile(
        r'\s*[(\[][^()\[\]]*?\b(?:mp3|flac|wav|kbps|official\s+(?:video|audio)|lyrics?|'
        r'clip|hd|hq|4k|explicit|текст|клип|скачать)\b[^()\[\]]*?[)\]]', re.I)
    PROD_RE = re.compile(r'\s*[(\[]\s*prod\.?(?:\s*by)?\b[^)\]]*[)\]]', re.I)
    FEAT_RE = re.compile(
        r'\s*(?:[(\[]\s*)?\b(?:feat\.?|ft\.?|featuring|при\s+уч\.?|с\s+уч\.?)\s+'
        r'([^)\]]+?)\s*(?:[)\]]|$)', re.I)
    SEP_RE  = re.compile(r'\s+[-–—―−]\s+')

    s = unicodedata.normalize('NFKC', raw)
    for rx in (ZW_RE, URL_RE, NUM_RE, TIME_RE, JUNK_RE, PROD_RE):
        s = rx.sub('', s)
    s = ' '.join(s.split()).strip(' -–—·•|')
    
    if len(s) < 2 or not re.search(r'\w', s): return None
    s = FEAT_RE.sub('', s)
        
    parts = SEP_RE.split(s, maxsplit=1)
    if len(parts) == 2:
        return {"artist": parts[0].strip(), "title": parts[1].strip()}
    return {"artist": "Unknown", "title": s}

def get_audio_info(file_path: Path) -> tuple[int, int]:
    if not MUTAGEN_AVAILABLE: return 0, 0
    try:
        from mutagen import File as MutagenFile
        af = MutagenFile(file_path)
        if af is None: return 0, 0
        return int(getattr(af.info, 'bitrate', 0) / 1000), int(getattr(af.info, 'length', 0))
    except Exception as e:
        log(f"Ошибка анализа файла {file_path}: {e}")
        return 0, 0

async def create_preview_audio(file_path: Path, duration: int) -> Path | None:
    if duration < 30: return None
    preview_path = file_path.with_name(f"preview_{file_path.name}")
    mid_start, mid_end, end_start = (duration // 2) - 5, (duration // 2) + 5, duration - 10
    cmd = [
        str(FFMPEG_PATH), "-y", "-i", str(file_path),
        "-filter_complex",
        f"[0:a]atrim=start=0:end=10,asetpts=PTS-STARTPTS[a1]; [0:a]atrim=start={mid_start}:end={mid_end},asetpts=PTS-STARTPTS[a2]; [0:a]atrim=start={end_start}:end={duration},asetpts=PTS-STARTPTS[a3]; [a1][a2][a3]concat=n=3:v=0:a=1[outa]",
        "-map", "[outa]", "-c:a", "libmp3lame", str(preview_path)
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd, 
            capture_output=True, text=True, 
            encoding='utf-8', errors='replace'
        )
        if result.returncode != 0: return None
        return preview_path
    except Exception as e:
        log(f"Ошибка создания превью: {e}")
        return None

# ============================================================
# БЕЗОПАСНАЯ ОТПРАВКА СООБЩЕНИЙ (не роняет воркер)
# ============================================================
async def safe_send(chat_id: int, text: str, **kw) -> None:
    """Уведомление, которое никогда не роняет воркер (блокировка бота, flood, сеть)."""
    for _ in range(2):
        try:
            await bot.send_message(chat_id, text, **kw)
            return
        except TelegramRetryAfter as e:
            try: await asyncio.sleep(e.retry_after + 1)
            except asyncio.CancelledError: raise
        except Exception as e:
            log(f"safe_send({chat_id}): {e!r}")
            return
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
    try:
        user = await _ensure_user(message)
        args = command.args
        if args and args.startswith("ref_") and not user.get("referrer_id"):
            try:
                referrer_tg_id = int(args.split("_")[1])
                if referrer_tg_id != user["telegram_id"]:
                    result = await process_referral(int(user["id"]), referrer_tg_id)
                    if result["ok"]: await message.answer(f"🎁 Ты пришел по приглашению! Твой друг получил {result['bonus']} поинтов.")
            except ValueError: pass
            except Exception as e: log(f"Ошибка рефералки: {e}")
        webapp_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🎵 Открыть плеер", web_app=WebAppInfo(url=WEBAPP_STUB_URL))]], resize_keyboard=True)
        await message.answer("Привет! Я FreeDom Music Bot 🎵.\n\nЗарабатывай поинты, качай треки и повышай уровень!\nЖми на кнопку ниже.", reply_markup=webapp_kb)
    except Exception as exc:
        log(f"Ошибка /start: {exc}")
        await message.answer("Не удалось обработать /start.")

@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _ensure_user(message)
    await message.answer(
        "Команды:\n"
        "<code>/import &lt;ссылка&gt;</code>\n"
        "<code>/import_text &lt;список&gt;</code>\n"
        "<code>/import_artist_hitmo &lt;ссылка&gt;</code>\n"
        "<code>/search &lt;название&gt;</code>\n"
        "<code>/buypremium</code>\n"
        "<code>/daily</code>\n"
        "<code>/invite</code>\n"
        "<code>/profile</code>"
    )

@dp.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    try:
        user = await _ensure_user(message)
        is_paid = _is_premium(user)
        limit_info = await check_download_limit(int(user["id"]), is_paid)
        
        limit_text = "∞ (Премиум)" if is_paid else f"{limit_info.get('remaining', 0)}/{limit_info.get('limit', 5)}"
        
        await message.answer(
            f"👤 Профиль\n"
            f"ID: {user['telegram_id']}\n"
            f"Поинты: {user.get('points', 0)}\n"
            f"Уровень: {esc(user.get('level', 'Новичок'))}\n"
            f"Рефералов: {user.get('referrals_count', 0)}\n"
            f"Скачиваний осталось: {limit_text}\n"
            f"Премиум: {'да' if is_paid else 'нет'}"
        )
    except Exception as exc:
        log(f"Ошибка /profile: {exc}")
        await message.answer("Ошибка загрузки профиля.")

@dp.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    try:
        user = await _ensure_user(message)
        result = await claim_daily(int(user["id"]))
        if result["ok"]:
            await message.answer(f"🎁 Ежедневный бонус получен!\n+{result['reward']} поинтов.\nТвой баланс: {result['user']['points']}")
            await _notify_level_up(message, result["user"])
        else: await message.answer(f"⏳ {result['msg']}")
    except Exception as exc:
        log(f"Ошибка /daily: {exc}")
        await message.answer("Не удалось получить бонус.")

@dp.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    try:
        user = await _ensure_user(message)
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{user['telegram_id']}"
        await message.answer(f"👥 Твоя реферальная ссылка:\n{ref_link}\n\nПриглашай друзей! Ты получишь 100 поинтов.")
    except Exception as exc:
        log(f"Ошибка /invite: {exc}")

@dp.message(Command("buypremium"))
async def cmd_buypremium(message: Message) -> None:
    prices = [LabeledPrice(label="Премиум на 30 дней", amount=1)]
    await message.answer_invoice(title="Премиум подписка", description="Снимает все лимиты на скачивание треков на 30 дней!", payload="premium_30_days", provider_token="", currency="XTR", prices=prices)

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    user = await _ensure_user(message)
    await give_premium(int(user["id"]), 30)
    await message.answer("🎉 <b>Оплата прошла успешно!</b>\n\nТебе активирован Премиум на 30 дней.")

# ============================================================
# ОЧЕРЕДЬ СКАЧИВАНИЯ (безопасный воркер с таймаутом)
# ============================================================
download_queue = asyncio.Queue()

async def process_task(task_data):
    """Обработка одного трека. Все сообщения через safe_send, чтобы не ронять воркер."""
    user_id, tg_id = task_data['user_id'], task_data['tg_id']
    artist, title = task_data['artist'], task_data['title']
    orig_dur = task_data.get('original_duration', 0)
    imp_src = task_data.get('import_source', 'unknown')
    pending_playlist_id = task_data.get('pending_playlist_id')
    pending_position = task_data.get('pending_position', 0)
    direct_url = task_data.get('direct_url')
    skip_soundcloud = task_data.get('skip_soundcloud', False)

    try:
        if direct_url:
            file_path, err_type, source_url = await download_track_from_direct_url(direct_url, artist, title)
        elif skip_soundcloud:
            file_path, err_type, source_url = None, "skipped_sc", None
        else:
            # --- НОВАЯ ЛОГИКА ОТ КЛОДА: Hitmo -> SoundCloud ---
            smart_task = {
                "artist": artist,
                "title": title,
                "duration": orig_dur,  # Передаем длительность из Яндекса для отсева slowed/sped up
            }
            file_path, err_type, source_url = await download_track_smart(smart_task)
            # --------------------------------------------------

        if file_path:
            await asyncio.to_thread(ensure_track_has_cover, str(file_path), artist, title)
            await asyncio.to_thread(fix_audio_tags, file_path, artist, title)
            bitrate, duration = await asyncio.to_thread(get_audio_info, Path(file_path))
            
            if duration < 60:
                Path(file_path).unlink()
                await safe_send(tg_id, f"❌ Пропущено (слишком короткий трек): {esc(title)}")
                await refund_downloads(user_id, 1)
            else:
                detected_source = "hitmo" if "hitmo" in (source_url or "").lower() else "soundcloud"

                new_track = await upload_track(
                    user_id=user_id, title=title, artist=artist,
                    file_path=file_path, source=detected_source,
                    bitrate=bitrate, duration=duration,
                    original_duration=orig_dur, import_source=imp_src,
                    pending_playlist_id=pending_playlist_id,
                    pending_position=pending_position,
                    source_url=source_url
                )
                await safe_send(tg_id, f"✅ Скачано: {esc(title)}")
                await add_points(user_id, 5, "import_track")

                # АВТО-МОДЕРАЦИЯ ОТКЛЮЧЕНА (Теперь всё падает в ручную модерацию)
                # duration_diff = abs(duration - orig_dur) if orig_dur > 0 else 0
                # if tg_id == ADMIN_ID and duration_diff <= 15:
                #     await update_track_status(new_track["id"], "approved", user_id)

                if task_data.get('add_to_liked') and new_track:
                    pos = task_data.get('pending_position', 0)
                    await toggle_like(user_id, new_track["id"], position=pos)
        else:
            log_msg = "🔒 Защищено DRM" if err_type == "drm" else "❌ Не удалось найти"
            await safe_send(tg_id, f"{log_msg}: {esc(title)}. Добавлено в очередь на ручную модерацию.")

            await upload_track(
                user_id=user_id, title=title, artist=artist,
                file_path="", source="failed_download",
                bitrate=0, duration=0,
                original_duration=orig_dur, import_source=imp_src,
                pending_playlist_id=pending_playlist_id,
                pending_position=pending_position
            )
            await refund_downloads(user_id, 1)
            
    except ValueError:
        await safe_send(tg_id, f"⏩ Пропущено (дубликат): {esc(title)}")
        await refund_downloads(user_id, 1)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log(f"[process_task] Ошибка: {e}")
        await safe_send(tg_id, f"❌ Ошибка сервера: {esc(title)}")
        await refund_downloads(user_id, 1)

async def download_worker():
    log("Воркер скачивания запущен...")
    while True:
        task_data = await download_queue.get()
        try:
            await asyncio.wait_for(process_task(task_data), timeout=600)
        except asyncio.TimeoutError:
            log(f"[Воркер] Таймаут обработки: {task_data.get('title')}")
            try:
                await refund_downloads(task_data['user_id'], 1)
                await safe_send(task_data['tg_id'], f"⏰ Таймаут при скачивании: {esc(str(task_data.get('title')))}")
            except Exception as e:
                log(f"[Воркер] Ошибка при обработке таймаута: {e}")
        except Exception as e:
            log(f"[Воркер] Ошибка: {type(e).__name__}: {e!r}")
            try:
                await refund_downloads(task_data['user_id'], 1)
                await safe_send(task_data['tg_id'], f"❌ Не удалось обработать трек: {esc(str(task_data.get('title')))}")
            except Exception as e2:
                log(f"[Воркер] Ошибка при уведомлении: {e2}")
        finally:
            download_queue.task_done()
            await asyncio.sleep(1)

            if task_data.get('is_last'):
                if task_data.get('limit_reached'):
                    await safe_send(task_data['tg_id'], "⚠️ <b>Достигнут дневной лимит!</b>\nОстальные треки из плейлиста не были добавлены.\n\nПовысьте свой уровень или купите <b>Премиум</b>, чтобы снять все ограничения.")
                else:
                    if task_data.get('is_single'):
                        await safe_send(task_data['tg_id'], "✅ Одиночный трек успешно скачан и добавлен в базу!")
                    else:
                        await safe_send(task_data['tg_id'], "✅ Все треки из плейлиста успешно скачаны и добавлены в базу!")
# ============================================================

async def show_import_destination_menu(message: Message, user_id: int, tracks: list, is_single: bool = False, source_type: str = "unknown", direct_url: str = None, playlist_title: str = None):
    existing = pending_imports.get(user_id)
    if existing and existing.get("reserved_count", 0) > 0:
        await refund_downloads(user_id, existing["reserved_count"])

    reserved_count = len(tracks) if isinstance(tracks, list) else 1
    pending_imports[user_id] = {
        "tracks": tracks,
        "is_single": is_single,
        "source_type": source_type,
        "direct_url": direct_url,
        "playlist_title": playlist_title,
        "reserved_count": reserved_count
    }
    
    playlists = await get_user_playlists(user_id)
    editable_playlists = [pl for pl in playlists if not pl.get("original_playlist_id")]
    
    kb_buttons = []
    
    suggested_playlist_exists = False
    if playlist_title:
        for pl in editable_playlists:
            if pl['title'].lower() == playlist_title.lower():
                suggested_playlist_exists = True
                break
                
    kb_buttons.append([InlineKeyboardButton(text="➕ Создать новый плейлист", callback_data="import_dest_new")])
    
    if playlist_title and not suggested_playlist_exists:
        short_title = playlist_title[:20] + ('...' if len(playlist_title) > 20 else '')
        kb_buttons.append([InlineKeyboardButton(text=f"📥 Сохранить как «{short_title}»", callback_data="import_dest_suggested")])
        
    kb_buttons.append([InlineKeyboardButton(text="❤️ Мне нравится", callback_data="import_dest_liked")])
    
    kb_buttons.append([InlineKeyboardButton(text="🌐 Только в базу (без плейлиста)", callback_data="import_dest_db_only")])
    
    for pl in editable_playlists:
        kb_buttons.append([InlineKeyboardButton(text=f"📁 {pl['title']}", callback_data=f"import_dest_pl_{pl['id']}")])
        
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    total_tracks = len(tracks) if isinstance(tracks, list) else 1
    
    text = (
        f"📦 <b>Импорт подготовлен!</b>\n"
        f"Треков к загрузке: {total_tracks}\n\n"
        f"👇 <b>Куда добавить эти треки?</b>"
    )
    
    await message.answer(text, reply_markup=kb)

@dp.message(Command("import_text"))
async def cmd_import_text(message: Message, command: CommandObject) -> None:
    user = await _ensure_user(message)
    text = (command.args or "").strip()
    
    if not text:
        return await message.answer(
            "📝 <b>Умная вставка</b>\n\n"
            "Скопируй список треков и отправь:\n"
            "<code>/import_text Исполнитель - Название</code>"
        )

    limit_info = await check_download_limit(int(user["id"]), _is_premium(user))
    if not limit_info["allowed"]: return await message.answer(f"⛔ {esc(str(limit_info['msg']))}")
    
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    parsed_tracks = []
    already_in_db = 0
    
    for line in lines:
        track = parse_smart_line(line)
        if track:
            existing_id = await check_track_exists(track["artist"], track["title"])
            if existing_id and existing_id > 0:
                already_in_db += 1
            else:
                if existing_id == -1:
                    track['skip_soundcloud'] = True
                parsed_tracks.append(track)
                
    if not parsed_tracks:
        return await message.answer("🤷‍♂️ Не найдено новых треков для импорта.")

    if already_in_db > 0:
        await message.answer(f"ℹ️ Пропущено {already_in_db} треков (уже есть в базе).")
        
    tracks_to_import = parsed_tracks[:limit_info["remaining"]]

    granted = await reserve_downloads(int(user["id"]), len(tracks_to_import))
    if granted == 0:
        return await message.answer("⛔ Лимит скачиваний на сегодня исчерпан.")
    tracks_to_import = tracks_to_import[:granted]

    await show_import_destination_menu(
        message, int(user["id"]), tracks=tracks_to_import, is_single=False, source_type="text_import"
    )

@dp.message(Command("import"))
async def cmd_import(message: Message, command: CommandObject) -> None:
    try:
        user = await _ensure_user(message)
        url = (command.args or "").strip()
        if not url: return await message.answer("Укажи ссылку: <code>/import &lt;ссылка&gt;</code>")
        
        limit_info = await check_download_limit(int(user["id"]), _is_premium(user))
        if not limit_info["allowed"]: return await message.answer(f"⛔ {esc(str(limit_info['msg']))}")
        
        remaining_limit = limit_info["remaining"]

        single_track = await asyncio.to_thread(parse_single_track, url)
        if single_track:
            try: src_type = detect_source(url)
            except ValueError: src_type = "direct"

            granted = await reserve_downloads(int(user["id"]), 1)
            if granted == 0:
                return await message.answer("⛔ Лимит скачиваний на сегодня исчерпан.")

            await show_import_destination_menu(
                message, int(user["id"]), tracks=[single_track], is_single=True, source_type=src_type, direct_url=url
            )
            return

        await message.answer(f"🔎 Сканирую плейлист... (Доступно сегодня: {remaining_limit})")
        tracks, playlist_title = await asyncio.to_thread(parse_playlist, url)
        source_type = detect_source(url)
        if not tracks: return await message.answer("Треков не найдено.")
        
        tracks_to_import = []
        already_in_db = 0
        
        for track in tracks:
            if len(tracks_to_import) >= remaining_limit: break
            artist, title = track.get('artist', 'Unknown'), track.get('title', 'Unknown')
            
            existing_track_id = await check_track_exists(artist, title)
            
            if existing_track_id and existing_track_id > 0:
                already_in_db += 1
            else:
                if existing_track_id == -1:
                    track['skip_soundcloud'] = True
                tracks_to_import.append(track)
                
        if not tracks_to_import:
            return await message.answer("🤷‍♂️ Не найдено новых треков (все уже есть в базе).")

        if already_in_db > 0:
            await message.answer(f"ℹ️ Пропущено {already_in_db} треков (уже есть в базе).")

        granted = await reserve_downloads(int(user["id"]), len(tracks_to_import))
        if granted == 0:
            return await message.answer("⛔ Лимит скачиваний на сегодня исчерпан.")
        tracks_to_import = tracks_to_import[:granted]

        await show_import_destination_menu(
            message, int(user["id"]), tracks=tracks_to_import, is_single=False, source_type=source_type, playlist_title=playlist_title
        )
            
    except ValueError as exc: await message.answer(f"Некорректная ссылка: {esc(str(exc))}")
    except Exception as exc:
        log(f"Ошибка /import: {exc}")
        await message.answer("Не удалось импортировать плейлист.")

@dp.message(Command("import_artist_hitmo"))
async def cmd_import_artist_hitmo(message: Message, command: CommandObject) -> None:
    user = await _ensure_user(message)
    urls_input = (command.args or "").strip()
    
    if not urls_input:
        return await message.answer(
            "Укажи ссылку на страницу артиста с Hitmo. "
            "Тебе достаточно скинуть одну ссылку, бот сам пролистает все страницы:\n"
            "<code>/import_artist_hitmo https://ru.hitmox.net/artist/noggano</code>"
        )
        
    urls = urls_input.split()
    status_msg = await message.answer(
        "🔎 Парсю страницы Hitmo. Это может занять несколько минут (бот сам листает страницы 1, 2, 3...)..."
    )
    
    all_tracks = []
    for url in urls:
        tracks = await parse_artist_page_hitmo(url)
        all_tracks.extend(tracks)
        
    seen_urls = set()
    unique_tracks = []
    already_in_db = 0
    
    for t in all_tracks:
        if t['url'] in seen_urls:
            continue
        seen_urls.add(t['url'])
        
        artist = t.get('artist', 'Unknown')
        title = t.get('title', 'Unknown')
        existing_id = await check_track_exists(artist, title)
        
        if existing_id and existing_id > 0:
            already_in_db += 1
            continue
        
        t['skip_soundcloud'] = True
        unique_tracks.append(t)
            
    if not unique_tracks:
        if already_in_db > 0:
            return await status_msg.edit_text(f"ℹ️ Все найденные треки ({already_in_db}) уже есть в базе.")
        return await status_msg.edit_text("❌ Не удалось найти треки на этой странице. Проверь ссылку.")
        
    limit_info = await check_download_limit(int(user["id"]), _is_premium(user))
    if not limit_info["allowed"]: return await status_msg.edit_text(f"⛔ {esc(str(limit_info['msg']))}")
    
    remaining = limit_info["remaining"]
    tracks_to_import = unique_tracks[:remaining]
    
    if already_in_db > 0:
        await message.answer(f"ℹ️ Пропущено {already_in_db} треков (уже есть в базе).")

    granted = await reserve_downloads(int(user["id"]), len(tracks_to_import))
    if granted == 0:
        return await status_msg.edit_text("⛔ Лимит скачиваний на сегодня исчерпан.")
    tracks_to_import = tracks_to_import[:granted]

    if len(unique_tracks) > granted:
        await status_msg.edit_text(f"⚠️ Найдено {len(unique_tracks)} новых треков, но твой лимит позволяет скачать только {granted}.")
    else:
        await status_msg.edit_text(f"✅ Найдено {len(unique_tracks)} новых треков. Начинаю импорт...")

    await show_import_destination_menu(
        message, int(user["id"]), tracks=tracks_to_import, is_single=False, source_type="hitmo_artist"
    )

@dp.callback_query(F.data.startswith("import_dest_"))
async def process_import_destination(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    data = pending_imports.get(int(user["id"]))
    
    if not data:
        return await callback.answer("Время ожидания истекло. Отправь команду заново.", show_alert=True)
        
    action = callback.data.replace("import_dest_", "")
    
    if action == "suggested":
        playlist_title = data.get("playlist_title")
        if not playlist_title:
            return await callback.answer("Ошибка: название не найдено.", show_alert=True)
            
        existing = await get_user_playlist_by_title(int(user["id"]), playlist_title)
        if existing:
            dest_playlist_id = existing["id"]
        else:
            new_pl = await create_user_playlist(int(user["id"]), playlist_title)
            dest_playlist_id = new_pl["id"]
            
        await callback.message.edit_text(f"⏳ Начинаю загрузку в <b>{esc(playlist_title)}</b>...")
        await execute_import(callback.message, int(user["id"]), callback.from_user.id, dest_playlist_id, add_to_liked=False)
        return
    
    if action == "new":
        waiting_for_new_playlist[int(user["id"])] = True
        await callback.message.edit_text("✍️ Введи название для нового плейлиста (просто отправь текст сообщением):")
        return
    
    if action == "db_only":
        dest_playlist_id = None
        add_to_liked = False
        dest_name = "Общую базу"
        await callback.message.edit_text(f"⏳ Начинаю загрузку в <b>{dest_name}</b>...")
        await execute_import(callback.message, int(user["id"]), callback.from_user.id, dest_playlist_id, add_to_liked)
        return
        
    if action == "liked":
        dest_playlist_id = None
        add_to_liked = True
        dest_name = "Мне нравится"
        await callback.message.edit_text(f"⏳ Начинаю загрузку в <b>{dest_name}</b>...")
        await execute_import(callback.message, int(user["id"]), callback.from_user.id, dest_playlist_id, add_to_liked)
        return
    else:
        dest_playlist_id = int(action.replace("pl_", ""))
        add_to_liked = False
        dest_name = "Плейлист"

    await callback.message.edit_text(f"⏳ Начинаю загрузку в <b>{dest_name}</b>...")
    await execute_import(callback.message, int(user["id"]), callback.from_user.id, dest_playlist_id, add_to_liked)

@dp.callback_query(F.data == "import_dest_cancel")
async def cancel_import(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    data = pending_imports.get(int(user["id"]))
    if data:
        reserved = data.get("reserved_count", 0)
        if reserved > 0:
            await refund_downloads(int(user["id"]), reserved)
        del pending_imports[int(user["id"])]
    await callback.message.edit_text("❌ Импорт отменен.")

async def execute_import(message: Message, user_id: int, tg_id: int, playlist_id: int | None, add_to_liked: bool):
    data = pending_imports.get(user_id)
    if not data: return
    
    tracks = data["tracks"]
    is_single = data["is_single"]
    source_type = data["source_type"]
    direct_url = data.get("direct_url")
    
    for idx, track in enumerate(tracks):
        is_last = (idx == len(tracks) - 1)
        
        task = {
            'user_id': user_id, 'tg_id': tg_id,
            'artist': track.get('artist', 'Unknown'), 'title': track.get('title', 'Unknown'),
            'original_duration': track.get('duration', 0), 'import_source': source_type,
            'is_last': is_last, 'limit_reached': False,
            'pending_playlist_id': playlist_id,
            'pending_position': idx,
            'is_single': is_single,
            'add_to_liked': add_to_liked,
            'skip_soundcloud': track.get('skip_soundcloud', False)
        }
        
        if is_single and source_type == "soundcloud" and direct_url:
            task['direct_url'] = direct_url
            
        await download_queue.put(task)
        
    del pending_imports[user_id]

@dp.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query: return await message.answer("Укажи название: <code>/search Баста</code>")
    await message.answer(f"🔎 Ищу в базе: {esc(query)}...")
    tracks = await search_tracks(query, limit=5)
    if not tracks: return await message.answer("Ничего не найдено.")
    keyboard = [[InlineKeyboardButton(text=f"{t['artist']} - {t['title']}"[:40], callback_data=f"dl_{t['id']}")] for t in tracks]
    await message.answer(f"Найдено {len(tracks)} трек(ов):", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("dl_"))
async def process_download(callback: CallbackQuery):
    track_id = int(callback.data.split("_")[1])
    user = await get_user(callback.from_user.id)
    if not (await check_download_limit(int(user["id"]), _is_premium(user)))["allowed"]:
        return await callback.answer("⛔ Лимит исчерпан", show_alert=True)
    track = await get_track_by_id(track_id)
    if not track or not track["file_path"] or not Path(track["file_path"]).exists(): return await callback.answer("Трек не найден.", show_alert=True)
    await callback.answer("Отправляю файл...")
    try:
        await callback.message.answer_audio(audio=FSInputFile(Path(track["file_path"])), caption=f"🎵 {esc(track['artist'])} — {esc(track['title'])}", title=track['title'], performer=track['artist'])
        await increment_download_count(int(user["id"]), 1)
        await increment_track_download(track_id)
        if track['uploaded_by'] != int(user["id"]):
            await add_points(track['uploaded_by'], 1, f"track_download_{track_id}")
    except Exception as e:
        log(f"Ошибка отправки трека {track_id}: {e}")

@dp.message(F.text.startswith("/get_track_"))
async def cmd_get_track(message: Message) -> None:
    try:
        track_id = int(message.text.split("_")[2])
        user = await _ensure_user(message)
        if not (await check_download_limit(int(user["id"]), _is_premium(user)))["allowed"]: return await message.answer("⛔ Лимит исчерпан")
        track = await get_track_by_id(track_id)
        if not track or not track["file_path"] or not Path(track["file_path"]).exists(): return await message.answer("❌ Трек или файл не найден.")
        await message.answer_audio(audio=FSInputFile(Path(track["file_path"])), caption=f"🎵 {esc(track['artist'])} — {esc(track['title'])}", title=track['title'], performer=track['artist'])
        await increment_download_count(int(user["id"]), 1)
        await increment_track_download(track_id)
        if track['uploaded_by'] != int(user["id"]):
            await add_points(track['uploaded_by'], 1, f"track_download_{track_id}")
    except Exception as e:
        log(f"Ошибка /get_track: {e}")

@dp.message(Command("admin"))
async def cmd_admin_panel(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎧 Модерация", callback_data="admin_moderate")], [InlineKeyboardButton(text="📊 Баланс Stars", callback_data="admin_stars")], [InlineKeyboardButton(text="📖 Справка", callback_data="admin_help")]])
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin_"))
async def process_admin_panel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ Нет прав", show_alert=True)
    action = callback.data.split("_")[1]
    await callback.answer()
    if action == "moderate":
        admin_user = await get_user(callback.from_user.id)
        await _send_next_moderation_track(callback.message, int(admin_user["id"]))
    elif action == "stars":
        try:
            star_amount = await bot.get_my_star_balance()
            await callback.message.answer(f"⭐ <b>Баланс звёзд:</b>\n{star_amount.amount} Stars")
        except: await callback.message.answer("❌ Ошибка баланса.")
    elif action == "help":
        await callback.message.answer(
            "<b>Админ-команды:</b>\n"
            "<code>/moderate</code>\n"
            "<code>/deletetrack &lt;id&gt;</code>\n"
            "<code>/addlimit &lt;id&gt; &lt;число&gt;</code>\n"
            "<code>/givepremium &lt;id&gt; &lt;дней&gt;</code>\n"
            "<code>/removepremium &lt;id&gt;</code>\n"
            "<code>/starbalance</code>\n"
            "<code>/cache_all</code>"
        )

@dp.message(Command("resetlimit"))
async def cmd_resetlimit(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return
    user = await _ensure_user(message)
    await reset_download_limit(int(user["id"]))
    await message.answer("🔄 Лимит сброшен!")

@dp.message(Command("addlimit"))
async def cmd_addlimit(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip().split()
    if len(args) == 1 and args[0].isdigit():
        user = await _ensure_user(message)
        await set_daily_limit(int(user["id"]), int(args[0]))
        await message.answer(f"✅ Твой лимит: {args[0]}")
    elif len(args) == 2 and args[0].isdigit() and args[1].isdigit():
        target = await get_user(int(args[0]))
        if not target: return await message.answer("❌ Пользователь не найден.")
        await set_daily_limit(int(target["id"]), int(args[1]))
        await message.answer(f"✅ Пользователю {args[0]} установлен лимит {args[1]}")
    else: await message.answer("Использование: <code>/addlimit &lt;число&gt;</code>")

@dp.message(Command("givepremium"))
async def cmd_givepremium(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip().split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit(): return await message.answer("Использование: <code>/givepremium &lt;id&gt; &lt;дней&gt;</code>")
    target = await get_user(int(args[0]))
    if not target: return await message.answer("Пользователь не найден.")
    await give_premium(int(target["id"]), int(args[1]))
    await message.answer(f"✅ Выдан Премиум на {args[1]} дней.")

@dp.message(Command("removepremium"))
async def cmd_removepremium(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip().split()
    if len(args) != 1 or not args[0].isdigit(): return await message.answer("Использование: <code>/removepremium &lt;id&gt;</code>")
    target = await get_user(int(args[0]))
    if not target: return await message.answer("Пользователь не найден.")
    await remove_premium(int(target["id"]))
    await message.answer(f"✅ Снят Премиум.")

@dp.message(Command("starbalance"))
async def cmd_starbalance(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return
    try:
        star_amount = await bot.get_my_star_balance()
        await message.answer(f"⭐ <b>Баланс звёзд:</b>\n{star_amount.amount} Stars")
    except: await message.answer("❌ Ошибка баланса.")

@dp.message(Command("deletetrack"))
async def cmd_deletetrack(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip()
    if not args or not args.isdigit(): return await message.answer("Использование: <code>/deletetrack &lt;id&gt;</code>")
    if await delete_track(int(args)): await message.answer(f"🗑️ Трек {args} удален.")
    else: await message.answer(f"❌ Трек {args} не найден.")

@dp.message(Command("cache_all"))
async def cmd_cache_all(message: Message):
    if message.from_user.id != ADMIN_ID: return
    
    tracks = await get_tracks_without_file_id()
    if not tracks:
        return await message.answer("✅ Все треки уже имеют file_id. Кеширование не требуется.")
        
    status_msg = await message.answer(f"⏳ Начинаю обработку {len(tracks)} треков (кеширование + обложки). Это займет время...")
    
    success_count = 0
    fail_count = 0
    
    for i, track in enumerate(tracks):
        file_path = Path(track["file_path"])
        if not file_path.exists():
            fail_count += 1
            continue
            
        await asyncio.to_thread(ensure_track_has_cover, str(file_path), track["artist"], track["title"])
            
        msg = None
        try:
            msg = await bot.send_audio(
                chat_id=message.chat.id,
                audio=FSInputFile(file_path),
                caption=f"🎵 {esc(track['artist'])} — {esc(track['title'])}",
                title=track['title'], 
                performer=track['artist']
            )
            
            if msg.audio and msg.audio.file_id:
                await set_track_file_id(track["id"], msg.audio.file_id)
                success_count += 1
            else:
                fail_count += 1
                
        except Exception as e:
            log(f"Ошибка кеширования трека {track['id']}: {e}")
            fail_count += 1
            
        finally:
            if msg:
                try: await msg.delete()
                except: pass
                
        await asyncio.sleep(2.0)
        
        if (i + 1) % 10 == 0:
            try:
                await status_msg.edit_text(f"⏳ Прогресс: {i + 1} / {len(tracks)}...\n✅ Успешно: {success_count} | ❌ Ошибок: {fail_count}")
            except: pass
            
    await status_msg.edit_text(f"✅ <b>Обработка завершена!</b>\n\nУспешно: {success_count}\nОшибок: {fail_count}\n\nТеперь все эти треки имеют обложки и доступны в инлайн-поиске!")

async def _send_next_moderation_track(message: Message, admin_internal_id: int):
    tracks = await get_pending_tracks(limit=1)
    if not tracks: return await message.answer("🎉 Очередь на модерацию пуста!")
    track = tracks[0]
    
    file_path = Path(track["file_path"]) if track["file_path"] else None

    if not file_path:
        orig_dur = track.get("original_duration", 0)
        imp_src = track.get("import_source", "unknown")
        source_names = {"yandex": "Яндекс.Музыка", "vk": "ВКонтакте", "spotify": "Spotify", "telegram": "Файл пользователя", "soundcloud": "SoundCloud"}
        src_display = source_names.get(imp_src, "Неизвестно")
        
        caption = (
            f"⚠️ <b>Трек не скачался автоматически!</b>\n\n"
            f"🎵 <b>{esc(track['artist'])} — {esc(track['title'])}</b>\n"
            f"Источник: {src_display}\n"
            f"Отправил: ID {track['uploaded_by']}\n"
            f"Оригинальная длительность: {format_time(orig_dur) if orig_dur else 'Неизвестно'}\n\n"
            f"Скачайте его вручную и отправьте боту, или нажмите кнопку ниже, чтобы найти ссылку на SoundCloud."
        )
        
        kb_buttons = [
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_reject_{track['id']}")],
            [InlineKeyboardButton(text="📥 Скачать вручную (файл/ссылка)", callback_data=f"mod_manual_{track['id']}")],
            [InlineKeyboardButton(text="🔍 Найти на SoundCloud", callback_data=f"mod_soundcloud_{track['id']}")]
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        
        await message.answer(caption, reply_markup=kb)
        return

    if not file_path.exists():
        await update_track_status(track["id"], "rejected", admin_internal_id)
        await message.answer(f"⚠️ Файл {esc(track['title'])} утерян. Статус: rejected.")
        return await _send_next_moderation_track(message, admin_internal_id)

    if track["bitrate"] == 0 or track["duration"] == 0:
        br, dur = await asyncio.to_thread(get_audio_info, file_path)
        if br > 0 or dur > 0:
            await update_track_metadata(track["id"], br, dur)
            track["bitrate"], track["duration"] = br, dur

    orig_dur = track.get("original_duration", 0)
    imp_src = track.get("import_source", "telegram")
    source_names = {"yandex": "Яндекс.Музыка", "vk": "ВКонтакте", "spotify": "Spotify", "telegram": "Файл пользователя", "soundcloud": "SoundCloud"}
    src_display = source_names.get(imp_src, "Неизвестно")
    
    dur_warning = ""
    if orig_dur > 0:
        diff = track['duration'] - orig_dur
        orig_str = format_time(orig_dur)
        down_str = format_time(track['duration'])
        if abs(diff) < 2:
            dur_warning = f"\n✅ Треки совпадают по таймлайну ({orig_str})"
        elif diff > 0:
            dur_warning = f"\n⚠️ <b>Внимание!</b> Скачанный трек ДЛИННЕЕ на {diff} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"
        else:
            dur_warning = f"\n⚠️ <b>Внимание!</b> Скачанный трек КОРОЧЕ на {abs(diff)} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"

    src_url = track.get("source_url")
    src_link_text = f'\n🔗 <a href="{esc(src_url)}">Источник</a>' if src_url else ""

    caption = (
        f"🎵 <b>{esc(track['artist'])} — {esc(track['title'])}</b>\n"
        f"Битрейт: {track['bitrate']} kbps | Длительность: {track['duration']} сек.\n"
        f"Источник: {src_display}\n"
        f"Отправил: ID {track['uploaded_by']}"
        f"{dur_warning}"
        f"{src_link_text}"
    )

    loading_msg = await message.answer("⏳ Создаю превью...")
    preview_path = await create_preview_audio(file_path, track['duration'])
    audio_to_send = preview_path if preview_path else file_path
    is_preview = preview_path is not None

    if is_preview: caption += "\n🎧 <i>30-сек превью</i>"

    kb_buttons = [[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"mod_approve_{track['id']}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_reject_{track['id']}")]]
    if is_preview:
        kb_buttons.append([InlineKeyboardButton(text="🔊 Прислать весь трек", callback_data=f"mod_full_{track['id']}")])
        
    kb_buttons.append([InlineKeyboardButton(text="📥 Скачать вручную (файл/ссылка)", callback_data=f"mod_manual_{track['id']}")])
        
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    try:
        await message.answer_audio(audio=FSInputFile(audio_to_send), caption=caption, reply_markup=kb)
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.edit_text(f"❌ Ошибка отправки: {esc(str(e))}")
    finally:
        if preview_path and preview_path.exists(): preview_path.unlink()

@dp.message(Command("moderate"))
async def cmd_moderate(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return await message.answer("⛔ Нет прав.")
    admin_user = await get_user(message.from_user.id)
    await _send_next_moderation_track(message, int(admin_user["id"]))

@dp.callback_query(F.data.startswith("mod_"))
async def process_moderation(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ Нет прав", show_alert=True)
    
    parts = callback.data.split("_")
    action, track_id = parts[1], int(parts[2])
    admin_user = await get_user(callback.from_user.id)
    track = await get_track_by_id(track_id)

    if not track: return await callback.answer("Трек не найден.", show_alert=True)

    if action == "soundcloud":
        await callback.answer("Ищу на SoundCloud...")
        query = f"{track['artist']} - {track['title']}"
        link = await asyncio.to_thread(find_soundcloud_link, query)
        
        if link:
            await callback.message.answer(
                f"🔍 <b>Найдено на SoundCloud:</b>\n"
                f"🎵 {esc(track['artist'])} — {esc(track['title'])}\n"
                f'🔗 <a href="{esc(link)}">Открыть ссылку</a>\n\n'
                f"Скачайте трек и отправьте его боту, чтобы заменить текущую версию.",
                disable_web_page_preview=True
            )
        else:
            await callback.answer("❌ Не удалось найти трек на SoundCloud.", show_alert=True)
        return

    if action == "full":
        if track_id in processing_full_tracks:
            await callback.answer("⏳ Трек уже загружается, подождите...", show_alert=True)
            return
        
        processing_full_tracks.add(track_id)
        await callback.answer("Загружаю полный трек...")
        
        chat_id = callback.message.chat.id
        try: await callback.message.delete()
        except: pass
        
        status_msg = await bot.send_message(chat_id, "⏳ Загружаю полный трек...")
            
        kb_buttons = [[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"mod_approve_{track_id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_reject_{track_id}")]]
        kb_buttons.append([InlineKeyboardButton(text="📥 Скачать вручную (файл/ссылка)", callback_data=f"mod_manual_{track_id}")])
            
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        
        source_names = {"yandex": "Яндекс.Музыка", "vk": "ВКонтакте", "spotify": "Spotify", "telegram": "Файл пользователя", "soundcloud": "SoundCloud"}
        src_display = source_names.get(track.get('import_source', 'unknown'), "Неизвестно")

        orig_dur = track.get("original_duration", 0)
        dur_warning = ""
        if orig_dur > 0:
            diff = track['duration'] - orig_dur
            orig_str = format_time(orig_dur)
            down_str = format_time(track['duration'])
            if abs(diff) < 2:
                dur_warning = f"\n✅ Треки совпадают по таймлайну ({orig_str})"
            elif diff > 0:
                dur_warning = f"\n⚠️ <b>Внимание!</b> Трек ДЛИННЕЕ на {diff} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"
            else:
                dur_warning = f"\n⚠️ <b>Внимание!</b> Трек КОРОЧЕ на {abs(diff)} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"
        
        src_url = track.get("source_url")
        src_link_text = f'\n🔗 <a href="{esc(src_url)}">Источник</a>' if src_url else ""

        caption = (
            f"🎵 <b>{esc(track['artist'])} — {esc(track['title'])}</b>\n"
            f"Битрейт: {track['bitrate']} kbps | Длительность: {track['duration']} сек.\n"
            f"Источник: {src_display}\n"
            f"Отправил: ID {track['uploaded_by']}\n\n"
            f"🎧 <i>Полный трек</i>{dur_warning}{src_link_text}"
        )
        
        success = False
        try:
            for attempt in range(3):
                try:
                    await bot.send_audio(
                        chat_id=chat_id, 
                        audio=FSInputFile(Path(track["file_path"])), 
                        caption=caption, 
                        reply_markup=kb
                    )
                    success = True
                    break
                except Exception as e:
                    log(f"Попытка {attempt+1} отправить полный трек не удалась: {e}")
                    if attempt < 2:
                        try:
                            await status_msg.edit_text(f"⏳ Ошибка сети. Пробую отправить еще раз через 5 секунд (попытка {attempt+2}/3)...")
                        except: pass
                        await asyncio.sleep(5)
        except Exception as final_e:
            log(f"Критическая ошибка при отправке полного трека: {final_e}")
        finally:
            if success:
                try: await status_msg.delete()
                except: pass
            else:
                try:
                    await status_msg.edit_text(
                        "❌ Не удалось отправить полный трек из-за плохого соединения.\n\n"
                        "Вы можете одобрить или отклонить трек по превью, либо попробовать позже.",
                        reply_markup=kb
                    )
                except: pass
            processing_full_tracks.discard(track_id)
        return

    if action == "manual":
        await callback.answer()
        waiting_for_replacement[callback.from_user.id] = track_id
        
        caption = (
            f"🎵 <b>{esc(track['artist'])} — {esc(track['title'])}</b>\n\n"
            f"📥 <b>Отправь мне MP3-файл</b> (как документ или аудио), ИЛИ <b>пришли прямую ссылку на SoundCloud</b> (например: <code>https://soundcloud.com/artist/track</code>).\n\n"
            f"Бот скачает трек, заменит текущую версию и <b>отправит на повторную модерацию</b>!"
        )
        
        try:
            await callback.message.delete()
        except: pass
        
        await callback.message.answer(caption)
        return

    await callback.answer("Обрабатываю...")
    try:
        if action == "approve":
            await update_track_status(track_id, "approved", int(admin_user["id"]))
            new_caption = (callback.message.caption or "") + "\n\n✅ <b>ОДОБРЕНО</b>"
            status_text = "✅ Одобрен!"
        else:
            await delete_track(track_id)
            new_caption = (callback.message.caption or "") + "\n\n❌ <b>ОТКЛОНЕНО</b>"
            status_text = "❌ Отклонен."

        await callback.message.edit_caption(caption=new_caption, reply_markup=None)
        author = await get_user_by_internal_id(track["uploaded_by"])
        if author:
            try: await bot.send_message(author["telegram_id"], f"Твой трек <b>{esc(track['artist'])} — {esc(track['title'])}</b> прошел модерацию.\nСтатус: {status_text}")
            except: pass
    except Exception as e:
        log(f"Ошибка при обновлении статуса: {e}")

    await _send_next_moderation_track(callback.message, int(admin_user["id"]))

async def process_trailer(message: Message, file_obj: Any):
    try:
        text = message.reply_to_message.text
        match = re.search(r"ID: (\d+)", text)
        if not match: 
            await message.answer("❌ Не удалось распознать ID плейлиста. Попробуй заново.")
            return
            
        playlist_id = int(match.group(1))
        
        if file_obj.file_size > 3 * 1024 * 1024:
            await message.answer("❌ Файл слишком большой! Максимум 3 МБ.")
            return
            
        TRAILERS_DIR.mkdir(parents=True, exist_ok=True)
        
        ext = Path(file_obj.file_name or "trailer.mp3").suffix or ".mp3"
        if ext.lower() not in ['.mp3', '.wav', '.ogg', '.m4a']:
             ext = ".mp3"
             
        dest = TRAILERS_DIR / f"trailer_{playlist_id}_{file_obj.file_unique_id}{ext}"
        
        await message.answer("⏳ Скачиваю и сохраняю трейлер...")
        await bot.download(file_obj, destination=dest)
        
        await set_playlist_trailer(playlist_id, str(dest))
        
        await message.answer(f"✅ Трейлер успешно прикреплен к плейлисту ID: {playlist_id}!\nЗайди в приложение, чтобы послушать.")
    except Exception as e:
        log(f"Ошибка сохранения трейлера: {e}")
        await message.answer("❌ Ошибка при сохранении трейлера. Попробуй еще раз.")

@dp.message(F.audio)
async def handle_audio(message: Message) -> None:
    if message.reply_to_message and message.reply_to_message.text and "ID:" in message.reply_to_message.text:
        await process_trailer(message, message.audio)
        return

    try:
        user = await _ensure_user(message)
        audio = message.audio
        if audio is None: return

        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        ext = Path(audio.file_name or "track.mp3").suffix or ".mp3"
        dest = UPLOADS_DIR / _safe_filename(f"{audio.file_unique_id}{ext}")

        await message.answer("📥 Скачиваю и проверяю качество трека...")
        await bot.download(audio, destination=dest)

        title, artist, bitrate, duration = "Unknown", "Unknown", 0, 0
        if MUTAGEN_AVAILABLE:
            def analyze():
                try:
                    from mutagen import File as MutagenFile
                    af = MutagenFile(dest)
                    if af is None: return "Unknown", "Unknown", 0, 0
                    t, a = "Unknown", "Unknown"
                    if af.tags:
                        if hasattr(af.tags, 'get'):
                            if af.tags.get("TIT2"): t = str(af.tags.get("TIT2").text[0]).strip()
                            if af.tags.get("TPE1"): a = str(af.tags.get("TPE1").text[0]).strip()
                        elif 'title' in af.tags: t = str(af.tags['title'][0]).strip()
                        elif 'artist' in af.tags: a = str(af.tags['artist'][0]).strip()
                    return t, a, int(getattr(af.info, 'bitrate', 0) / 1000), int(getattr(af.info, 'length', 0))
                except: return "Unknown", "Unknown", 0, 0
            title, artist, bitrate, duration = await asyncio.to_thread(analyze)

        if title == "Unknown" or not title: title = (audio.title or "Unknown").strip()
        if artist == "Unknown" or not artist: artist = (audio.performer or "Unknown").strip()

        if title == "Unknown" or artist == "Unknown":
            stem = Path(audio.file_name or "track.mp3").stem
            if " - " in stem:
                parts = stem.split(" - ", 1)
                if artist == "Unknown": artist = parts[0].strip()
                if title == "Unknown": title = parts[1].strip()
            elif title == "Unknown": title = stem

        is_replacement = message.from_user.id in waiting_for_replacement

        if not is_replacement and (duration < 60 or (bitrate > 0 and bitrate < 192)):
            dest.unlink()
            return await message.answer(f"❌ Трек отклонен: {'длительность < минуты' if duration < 60 else 'битрейт < 192 kbps'}.")

        if title == "Unknown" or artist == "Unknown":
            dest.unlink()
            return await message.answer("❌ Трек отклонен: нет метаданных.")

        new_filename = f"{_safe_filename(artist)} - {_safe_filename(title)}.mp3"
        final_dest = UPLOADS_DIR / new_filename
        if dest != final_dest:
            if final_dest.exists(): final_dest = UPLOADS_DIR / f"{_safe_filename(artist)} - {_safe_filename(title)}_{audio.file_unique_id[:4]}.mp3"
            dest.rename(final_dest)
            dest = final_dest

        if is_replacement:
            track_id_to_replace = waiting_for_replacement.pop(message.from_user.id)
            old_path = await replace_track_file(track_id_to_replace, str(dest), bitrate, duration)
            
            if old_path and old_path != str(dest) and Path(old_path).exists():
                try: Path(old_path).unlink()
                except: pass
                
            await message.answer(f"✅ Файл успешно заменен! Новая длительность: {duration} сек.\n\n⏳ Отправляю на повторную модерацию...")
            
            admin_user = await get_user(message.from_user.id)
            await _send_next_moderation_track(message, int(admin_user["id"]))
            return

        points_reward = 15 if (bitrate >= 320 or ext.lower() in ['.flac', '.wav']) and duration >= 90 else 5
        try:
            await upload_track(
                user_id=int(user["id"]), title=title, artist=artist, 
                file_path=str(dest), source="telegram", bitrate=bitrate, duration=duration,
                original_duration=0, import_source="telegram"
            )
        except ValueError as e:
            dest.unlink()
            return await message.answer(f"❌ Ошибка: {esc(str(e))}")

        result = await add_points(int(user["id"]), points_reward, "upload_track")
        await message.answer(f"🎵 <b>{esc(artist)} — {esc(title)}</b>\nБитрейт: {bitrate} kbps | Длительность: {duration} сек.\n\n✅ Отправлен на проверку!\n💰 +{points_reward} поинтов.")
        await _notify_level_up(message, result)
    except Exception as exc:
        log(f"Ошибка обработки аудио: {exc}")
        await message.answer("Не удалось отправить трек.")

@dp.message(F.document)
async def handle_document(message: Message) -> None:
    if message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
        
        if "ID:" in text and "обложки" in text:
            try:
                match = re.search(r"ID: (\d+)", text)
                if not match: return await message.answer("❌ Не удалось распознать ID плейлиста.")
                playlist_id = int(match.group(1))
                
                if message.document.file_size > 5 * 1024 * 1024:
                    return await message.answer("❌ Файл слишком большой! Максимум 5 МБ.")
                    
                COVERS_DIR = UPLOADS_DIR / "covers"
                COVERS_DIR.mkdir(parents=True, exist_ok=True)
                dest = COVERS_DIR / f"cover_{playlist_id}.jpg"
                
                await message.answer("⏳ Скачиваю и сохраняю обложку...")
                await bot.download(message.document, destination=dest)
                await set_playlist_custom_cover(playlist_id, str(dest))
                await message.answer(f"✅ Обложка успешно прикреплена к плейлисту ID: {playlist_id}!\nОбнови приложение, чтобы увидеть результат.")
            except Exception as e:
                log(f"Ошибка сохранения обложки: {e}")
                await message.answer("❌ Ошибка при сохранении обложки.")
            return
            
        if "ID:" in text:
            await process_trailer(message, message.document)
            return
            
    await message.answer("Я пока учусь! Используй <code>/help</code>")

@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    if message.reply_to_message and message.reply_to_message.text and "ID:" in message.reply_to_message.text and "обложки" in message.reply_to_message.text:
        try:
            text = message.reply_to_message.text
            match = re.search(r"ID: (\d+)", text)
            if not match: 
                return await message.answer("❌ Не удалось распознать ID плейлиста.")
                
            playlist_id = int(match.group(1))
            
            photo = message.photo[-1]
            
            if photo.file_size > 5 * 1024 * 1024:
                return await message.answer("❌ Файл слишком большой! Максимум 5 МБ.")
                
            COVERS_DIR = UPLOADS_DIR / "covers"
            COVERS_DIR.mkdir(parents=True, exist_ok=True)
            
            dest = COVERS_DIR / f"cover_{playlist_id}.jpg"
            
            await message.answer("⏳ Скачиваю и сохраняю обложку...")
            await bot.download(photo, destination=dest)
            
            await set_playlist_custom_cover(playlist_id, str(dest))
            
            await message.answer(f"✅ Обложка успешно прикреплена к плейлисту ID: {playlist_id}!\nОбнови приложение, чтобы увидеть результат.")
        except Exception as e:
            log(f"Ошибка сохранения обложки: {e}")
            await message.answer("❌ Ошибка при сохранении обложки. Попробуй еще раз.")

@dp.message(F.text)
async def handle_text(message: Message) -> None:
    user = await _ensure_user(message)

    if int(user["id"]) in waiting_for_new_playlist:
        del waiting_for_new_playlist[int(user["id"])]
        title = message.text.strip()
        
        new_pl = await create_user_playlist(int(user["id"]), title)
        await message.answer(f"📁 Плейлист <b>{esc(title)}</b> создан! Начинаю загрузку...")
        
        await execute_import(message, int(user["id"]), message.from_user.id, new_pl["id"], add_to_liked=False)
        return

    if message.from_user.id in waiting_for_replacement:
        url = message.text.strip()
        if "soundcloud.com" in url:
            track_id = waiting_for_replacement.pop(message.from_user.id)
            track = await get_track_by_id(track_id)
            
            if not track:
                return await message.answer("❌ Трек не найден в базе.")
                
            await message.answer("⏳ Скачиваю трек по ссылке...")
            
            try:
                file_path, err, src_url = await download_from_soundcloud_url(url, track["artist"], track["title"])
                if file_path and Path(file_path).exists() and Path(file_path).stat().st_size > 50_000:
                    await asyncio.to_thread(ensure_track_has_cover, file_path, track["artist"], track["title"])
                
                if file_path and Path(file_path).exists() and Path(file_path).stat().st_size > 50_000:
                    br, dur = await asyncio.to_thread(get_audio_info, Path(file_path))
                    await asyncio.to_thread(fix_audio_tags, file_path, track["artist"], track["title"])
                    
                    old_path = await replace_track_file(track_id, file_path, br, dur, new_source_url=src_url)
                    
                    if old_path and old_path != file_path and Path(old_path).exists():
                        try: Path(old_path).unlink()
                        except: pass
                        
                    await message.answer(f"✅ Трек успешно скачан и заменен!\nБитрейт: {br} kbps | Длительность: {dur} сек.\n\n⏳ Отправляю на повторную модерацию...")
                    
                    admin_user = await get_user(message.from_user.id)
                    await _send_next_moderation_track(message, int(admin_user["id"]))
                else:
                    await message.answer("❌ Не удалось скачать трек по этой ссылке (файл поврежден или пустой). Скорее всего, SoundCloud его заблокировал. Попробуй скинуть файлом.")
                    waiting_for_replacement[message.from_user.id] = track_id
            except Exception as e:
                log(f"Ошибка скачивания по ссылке: {e}")
                await message.answer("❌ Ошибка сервера при скачивании. Попробуй скинуть файлом.")
                waiting_for_replacement[message.from_user.id] = track_id
            return

    await message.answer("Я пока учусь! Используй <code>/help</code>")

@dp.inline_query()
async def inline_query_handler(inline_query: InlineQuery) -> None:
    query = inline_query.query.strip()
    if not query:
        return
        
    tracks = await search_tracks(query, limit=20)
    results = []
    
    for t in tracks:
        if not t.get("file_id"):
            continue
            
        quality = t["quality_status"] if t["quality_status"] != 'none' else 'Verified'
        
        results.append(
            InlineQueryResultCachedAudio(
                id=str(t["id"]),
                audio_file_id=t["file_id"],
                caption=f"🎵 {t['artist']} — {t['title']}\n⭐ Качество: {quality}"
            )
        )
        
        if len(results) >= 10:
            break
            
    await inline_query.answer(results, cache_time=5, is_personal=True)

async def main() -> None:
    if not TELEGRAM_BOT_TOKEN: sys.exit(1)
    log("Инициализация базы данных...")
    await init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎵 Открыть плеер", web_app=WebAppInfo(url=WEBAPP_STUB_URL)))
    await bot.set_my_commands(commands=[
        BotCommand(command="start", description="Главное меню"), 
        BotCommand(command="profile", description="Мой профиль"), 
        BotCommand(command="import", description="Импорт плейлиста"), 
        BotCommand(command="import_text", description="Импорт текстом (ВК, Spotify)"),
        BotCommand(command="import_artist_hitmo", description="Импорт всех треков артиста с Hitmo"),
        BotCommand(command="search", description="Поиск в базе")
    ], scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands=[BotCommand(command="admin", description="👑 Админ-панель"), BotCommand(command="moderate", description="⚙️ Модерация"), BotCommand(command="cache_all", description="🖼 Кеширование треков + обложки")], scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    asyncio.create_task(download_worker())
    asyncio.create_task(offline_worker(bot))
    log("Запуск бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): log("Бот остановлен.")
