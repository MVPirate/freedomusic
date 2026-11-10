"""Telegram-бот FreeDom Music Bot на aiogram 3.x (Асинхронный)."""
from __future__ import annotations

import asyncio
import re
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, 
    CallbackQuery, FSInputFile, LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, KeyboardButton, MenuButtonWebApp, 
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)
from aiogram.client.session.aiohttp import AiohttpSession
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
    set_playlist_trailer, set_playlist_custom_cover
)
from backend.parser import detect_source, parse_playlist, get_yandex_client
from backend.downloader import (
    download_track_from_youtube, fix_audio_tags, FFMPEG_PATH, find_soundcloud_link,
    download_from_soundcloud_url
)

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
WEBAPP_STUB_URL: str = "https://flattery-defuse-yogurt.ngrok-free.dev"

session = AiohttpSession(timeout=300)
bot: Bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session)
dp: Dispatcher = Dispatcher()

waiting_for_replacement = {}
processing_full_tracks = set()

def log(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {text}")

def _telegram_id(message: Message) -> int:
    if message.from_user is None: raise ValueError("Не удалось определить пользователя Telegram")
    return message.from_user.id

async def _ensure_user(message: Message) -> dict:
    return await get_user(_telegram_id(message))

def _is_premium(user: dict) -> bool:
    return bool(user.get("is_premium"))

async def _notify_level_up(message: Message, result: dict):
    if result.get("leveled_up"):
        await message.answer(f"🎉 **Новый уровень!**\n\nПоздравляем, теперь ты — **{result['level']}**!")

def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    return cleaned.strip("._") or "track"

def format_time(seconds: int) -> str:
    if not seconds: return "0 сек"
    m, s = divmod(seconds, 60)
    if m > 0: return f"{m} мин {s} сек"
    return f"{s} сек"

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
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0: return None
        return preview_path
    except Exception as e:
        log(f"Ошибка создания превью: {e}")
        return None

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
    await message.answer("Команды:\n/import <ссылка>\n/search <название>\n/buypremium\n/daily\n/invite\n/profile")

@dp.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    try:
        user = await _ensure_user(message)
        is_paid = _is_premium(user)
        limit_info = await check_download_limit(int(user["id"]), is_paid)
        limit_text = "∞ (Премиум)" if is_paid else f"{limit_info.get('used', 0)}/{limit_info.get('limit', 5)}"
        await message.answer(f"👤 Профиль\nID: {user['telegram_id']}\nПоинты: {user.get('points', 0)}\nУровень: {user.get('level', 'Новичок')}\nРефералов: {user.get('referrals_count', 0)}\nСкачиваний сегодня: {limit_text}\nПремиум: {'да' if is_paid else 'нет'}")
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
    await message.answer("🎉 **Оплата прошла успешно!**\n\nТебе активирован Премиум на 30 дней.")

# --- ОЧЕРЕДЬ СКАЧИВАНИЯ ---
download_queue = asyncio.Queue()

async def download_worker():
    log("Воркер скачивания запущен...")
    while True:
        task_data = await download_queue.get()
        user_id, tg_id = task_data['user_id'], task_data['tg_id']
        artist, title = task_data['artist'], task_data['title']
        orig_dur = task_data.get('original_duration', 0)
        imp_src = task_data.get('import_source', 'unknown')
        pending_playlist_id = task_data.get('pending_playlist_id')
        pending_position = task_data.get('pending_position', 0)

        try:
            file_path, err_type, source_url = await download_track_from_youtube(artist, title)
            if file_path:
                await asyncio.to_thread(fix_audio_tags, file_path, artist, title)
                bitrate, duration = await asyncio.to_thread(get_audio_info, Path(file_path))
                if duration < 60:
                    Path(file_path).unlink()
                    await bot.send_message(tg_id, f"❌ Пропущено (слишком короткий трек): {title}")
                else:
                    await upload_track(
                        user_id=user_id, title=title, artist=artist, 
                        file_path=file_path, source="soundcloud", 
                        bitrate=bitrate, duration=duration,
                        original_duration=orig_dur, import_source=imp_src,
                        pending_playlist_id=pending_playlist_id,
                        pending_position=pending_position,
                        source_url=source_url
                    )
                    await bot.send_message(tg_id, f"✅ Скачано: {title}")
                    await add_points(user_id, 5, "import_track")
            else:
                log_msg = "🔒 Защищено DRM" if err_type == "drm" else "❌ Не удалось найти"
                await bot.send_message(tg_id, f"{log_msg}: {title}. Добавлено в очередь на ручную модерацию.")
                
                await upload_track(
                    user_id=user_id, title=title, artist=artist, 
                    file_path="", source="failed_download",
                    bitrate=0, duration=0,
                    original_duration=orig_dur, import_source=imp_src,
                    pending_playlist_id=pending_playlist_id,
                    pending_position=pending_position
                )
        except ValueError:
             await bot.send_message(tg_id, f"⏩ Пропущено (дубликат): {title}")
        except Exception as e:
            log(f"[Воркер] Ошибка: {e}")
            await bot.send_message(tg_id, f"❌ Ошибка сервера: {title}")
        finally:
            download_queue.task_done()
            await asyncio.sleep(1)
            
            if task_data.get('is_last'):
                if task_data.get('limit_reached'):
                    await bot.send_message(tg_id, "⚠️ **Достигнут дневной лимит!**\nОстальные треки из плейлиста не были добавлены.\n\nПовысьте свой уровень или купите **Премиум**, чтобы снять все ограничения.")
                else:
                    await bot.send_message(tg_id, "✅ Все треки из плейлиста успешно скачаны и добавлены в базу!")

@dp.message(Command("import"))
async def cmd_import(message: Message, command: CommandObject) -> None:
    try:
        user = await _ensure_user(message)
        url = (command.args or "").strip()
        if not url: return await message.answer("Укажи ссылку: /import <ссылка>")
        is_paid = _is_premium(user)
        limit_info = await check_download_limit(int(user["id"]), is_paid)
        if not limit_info["allowed"]: return await message.answer(f"⛔ {limit_info['msg']}")
        
        remaining_limit = limit_info["remaining"]
        await message.answer(f"🔎 Сканирую плейлист... (Доступно сегодня: {remaining_limit})")
        
        tracks, playlist_title = await asyncio.to_thread(parse_playlist, url)
        source_type = detect_source(url)
        if not tracks: return await message.answer("Треков не найдено.")
        
        existing_pl = await get_user_playlist_by_title(int(user["id"]), playlist_title)
        if existing_pl:
            new_pl_id = existing_pl["id"]
            await message.answer(f"📁 Найден существующий плейлист **{playlist_title}**. Дополняю его...")
        else:
            new_pl = await create_user_playlist(int(user["id"]), playlist_title)
            new_pl_id = new_pl["id"]
            await message.answer(f"📁 Создан плейлист **{playlist_title}**. Начинаю загрузку...")
        
        await import_playlist(int(user["id"]), url, source_type, is_paid)
        
        added_to_queue = 0
        limit_reached = False
        tracks_to_download = []
        
        for idx, track in enumerate(tracks):
            if added_to_queue >= remaining_limit:
                limit_reached = True
                break 
                
            artist, title = track.get('artist', 'Unknown'), track.get('title', 'Unknown')
            
            existing_track_id = await check_track_exists(artist, title)
            
            if existing_track_id:
                success = await add_track_to_playlist(new_pl_id, existing_track_id, position=idx)
                if success:
                    await message.answer(f"⏩ Добавлен из базы в плейлист: {artist} - {title}")
                else:
                    await message.answer(f"⏩ Уже в этом плейлисте: {artist} - {title}")
                continue 
            
            tracks_to_download.append(track)
            added_to_queue += 1
            
        if added_to_queue > 0:
            await increment_download_count(int(user["id"]), added_to_queue)
            await message.answer(f"🎉 Добавлено в очередь: {added_to_queue} треков.")
            
            for idx, track in enumerate(tracks_to_download):
                is_last = (idx == len(tracks_to_download) - 1)
                artist, title = track.get('artist', 'Unknown'), track.get('title', 'Unknown')
                
                real_position = tracks.index(track)
                
                await download_queue.put({
                    'user_id': int(user["id"]), 'tg_id': message.from_user.id,
                    'artist': artist, 'title': title,
                    'original_duration': track.get('duration', 0), 'import_source': source_type,
                    'is_last': is_last, 'limit_reached': limit_reached,
                    'pending_playlist_id': new_pl_id,
                    'pending_position': real_position
                })
                await message.answer(f"⏳ В очереди: {artist} - {title}...")
                
        else:
            await message.answer("🤷‍♂️ Не найдено новых треков для скачивания.")
            if limit_reached:
                await message.answer("⚠️ **Достигнут дневной лимит!**\nОстальные треки из плейлиста не были добавлены.\n\nПовысьте свой уровень или купите **Премиум**, чтобы снять все ограничения.")
            
    except ValueError as exc: await message.answer(f"Некорректная ссылка: {exc}")
    except Exception as exc:
        log(f"Ошибка /import: {exc}")
        await message.answer("Не удалось импортировать плейлист.")

@dp.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query: return await message.answer("Укажи название: /search Баста")
    await message.answer(f"🔎 Ищу в базе: {query}...")
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
        await callback.message.answer_audio(audio=FSInputFile(Path(track["file_path"])), caption=f"🎵 {track['artist']} — {track['title']}", title=track['title'], performer=track['artist'])
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
        await message.answer_audio(audio=FSInputFile(Path(track["file_path"])), caption=f"🎵 {track['artist']} — {track['title']}", title=track['title'], performer=track['artist'])
        await increment_download_count(int(user["id"]), 1)
        await increment_track_download(track_id)
        if track['uploaded_by'] != int(user["id"]):
            await add_points(track['uploaded_by'], 1, f"track_download_{track_id}")
    except Exception as e:
        log(f"Ошибка /get_track: {e}")

# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin_panel(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎧 Модерация", callback_data="admin_moderate")], [InlineKeyboardButton(text="📊 Баланс Stars", callback_data="admin_stars")], [InlineKeyboardButton(text="📖 Справка", callback_data="admin_help")]])
    await message.answer("👑 **Админ-панель**", reply_markup=kb, parse_mode="Markdown")

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
            await callback.message.answer(f"⭐ **Баланс звёзд:**\n{star_amount.amount} Stars", parse_mode="Markdown")
        except: await callback.message.answer("❌ Ошибка баланса.")
    elif action == "help":
        await callback.message.answer("**Админ-команды:**\n`/moderate`\n`/deletetrack <id>`\n`/addlimit <id> <число>`\n`/givepremium <id> <дней>`\n`/removepremium <id>`\n`/starbalance`", parse_mode="Markdown")

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
    else: await message.answer("Использование: /addlimit <число>")

@dp.message(Command("givepremium"))
async def cmd_givepremium(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip().split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit(): return await message.answer("Использование: /givepremium <id> <дней>")
    target = await get_user(int(args[0]))
    if not target: return await message.answer("Пользователь не найден.")
    await give_premium(int(target["id"]), int(args[1]))
    await message.answer(f"✅ Выдан Премиум на {args[1]} дней.")

@dp.message(Command("removepremium"))
async def cmd_removepremium(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip().split()
    if len(args) != 1 or not args[0].isdigit(): return await message.answer("Использование: /removepremium <id>")
    target = await get_user(int(args[0]))
    if not target: return await message.answer("Пользователь не найден.")
    await remove_premium(int(target["id"]))
    await message.answer(f"✅ Снят Премиум.")

@dp.message(Command("starbalance"))
async def cmd_starbalance(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return
    try:
        star_amount = await bot.get_my_star_balance()
        await message.answer(f"⭐ **Баланс звёзд:**\n{star_amount.amount} Stars", parse_mode="Markdown")
    except: await message.answer("❌ Ошибка баланса.")

@dp.message(Command("deletetrack"))
async def cmd_deletetrack(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip()
    if not args or not args.isdigit(): return await message.answer("Использование: /deletetrack <id>")
    if await delete_track(int(args)): await message.answer(f"🗑️ Трек {args} удален.")
    else: await message.answer(f"❌ Трек {args} не найден.")

# --- МОДЕРАЦИЯ ---
async def _send_next_moderation_track(message: Message, admin_internal_id: int):
    tracks = await get_pending_tracks(limit=1)
    if not tracks: return await message.answer("🎉 Очередь на модерацию пуста!")
    track = tracks[0]
    
    file_path = Path(track["file_path"]) if track["file_path"] else None

    if not file_path:
        orig_dur = track.get("original_duration", 0)
        imp_src = track.get("import_source", "unknown")
        source_names = {"yandex": "Яндекс.Музыка", "vk": "ВКонтакте", "spotify": "Spotify", "telegram": "Файл пользователя"}
        src_display = source_names.get(imp_src, "Неизвестно")
        
        caption = (
            f"⚠️ **Трек не скачался автоматически!**\n\n"
            f"🎵 **{track['artist']} — {track['title']}**\n"
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
        
        await message.answer(caption, reply_markup=kb, parse_mode="Markdown")
        return

    if not file_path.exists():
        await update_track_status(track["id"], "rejected", admin_internal_id)
        await message.answer(f"⚠️ Файл {track['title']} утерян. Статус: rejected.")
        return await _send_next_moderation_track(message, admin_internal_id)

    if track["bitrate"] == 0 or track["duration"] == 0:
        br, dur = await asyncio.to_thread(get_audio_info, file_path)
        if br > 0 or dur > 0:
            await update_track_metadata(track["id"], br, dur)
            track["bitrate"], track["duration"] = br, dur

    orig_dur = track.get("original_duration", 0)
    imp_src = track.get("import_source", "telegram")
    source_names = {"yandex": "Яндекс.Музыка", "vk": "ВКонтакте", "spotify": "Spotify", "telegram": "Файл пользователя"}
    src_display = source_names.get(imp_src, "Неизвестно")
    
    dur_warning = ""
    if orig_dur > 0:
        diff = track['duration'] - orig_dur
        orig_str = format_time(orig_dur)
        down_str = format_time(track['duration'])
        if abs(diff) < 2:
            dur_warning = f"\n✅ Треки совпадают по таймлайну ({orig_str})"
        elif diff > 0:
            dur_warning = f"\n⚠️ **Внимание!** Скачанный трек ДЛИННЕЕ на {diff} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"
        else:
            dur_warning = f"\n⚠️ **Внимание!** Скачанный трек КОРОЧЕ на {abs(diff)} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"

    src_url = track.get("source_url")
    src_link_text = f"\n🔗 [Источник]({src_url})" if src_url else ""

    caption = (
        f"🎵 **{track['artist']} — {track['title']}**\n"
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

    if is_preview: caption += "\n🎧 *30-сек превью*"

    kb_buttons = [[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"mod_approve_{track['id']}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_reject_{track['id']}")]]
    if is_preview:
        kb_buttons.append([InlineKeyboardButton(text="🔊 Прислать весь трек", callback_data=f"mod_full_{track['id']}")])
        
    kb_buttons.append([InlineKeyboardButton(text="📥 Скачать вручную (файл/ссылка)", callback_data=f"mod_manual_{track['id']}")])
        
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    try:
        await message.answer_audio(audio=FSInputFile(audio_to_send), caption=caption, reply_markup=kb, parse_mode="Markdown")
        await loading_msg.delete()
    except Exception as e:
        await loading_msg.edit_text(f"❌ Ошибка отправки: {e}")
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
                f"🔍 **Найдено на SoundCloud:**\n"
                f"🎵 {track['artist']} — {track['title']}\n"
                f"🔗 [Открыть ссылку]({link})\n\n"
                f"Скачайте трек и отправьте его боту, чтобы заменить текущую версию.",
                parse_mode="Markdown",
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
        
        source_names = {"yandex": "Яндекс.Музыка", "vk": "ВКонтакте", "spotify": "Spotify", "telegram": "Файл пользователя"}
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
                dur_warning = f"\n⚠️ **Внимание!** Трек ДЛИННЕЕ на {diff} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"
            else:
                dur_warning = f"\n⚠️ **Внимание!** Трек КОРОЧЕ на {abs(diff)} сек.\n   В импорте: {orig_str} | Скачано: {down_str}"
        
        src_url = track.get("source_url")
        src_link_text = f"\n🔗 [Источник]({src_url})" if src_url else ""

        caption = f"🎵 **{track['artist']} — {track['title']}**\nБитрейт: {track['bitrate']} kbps | Длительность: {track['duration']} сек.\nИсточник: {src_display}\nОтправил: ID {track['uploaded_by']}\n\n🎧 *Полный трек*{dur_warning}{src_link_text}"
        
        success = False
        try:
            for attempt in range(3):
                try:
                    await bot.send_audio(
                        chat_id=chat_id, 
                        audio=FSInputFile(Path(track["file_path"])), 
                        caption=caption, 
                        reply_markup=kb, 
                        parse_mode="Markdown"
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
            f"🎵 **{track['artist']} — {track['title']}**\n\n"
            f"📥 **Отправь мне MP3-файл** (как документ или аудио), ИЛИ **пришли прямую ссылку на SoundCloud** (например: `https://soundcloud.com/artist/track`).\n\n"
            f"Бот скачает трек, заменит текущую версию и **отправит на повторную модерацию**!"
        )
        
        try:
            await callback.message.delete()
        except: pass
        
        await callback.message.answer(caption, parse_mode="Markdown")
        return

    await callback.answer("Обрабатываю...")
    try:
        if action == "approve":
            await update_track_status(track_id, "approved", int(admin_user["id"]))
            new_caption = callback.message.caption + "\n\n✅ **ОДОБРЕНО**"
            status_text = "✅ Одобрен!"
        else:
            await delete_track(track_id)
            new_caption = callback.message.caption + "\n\n❌ **ОТКЛОНЕНО**"
            status_text = "❌ Отклонен."

        await callback.message.edit_caption(caption=new_caption, reply_markup=None)
        author = await get_user_by_internal_id(track["uploaded_by"])
        if author:
            try: await bot.send_message(author["telegram_id"], f"Твой трек **{track['artist']} — {track['title']}** прошел модерацию.\nСтатус: {status_text}")
            except: pass
    except Exception as e:
        log(f"Ошибка при обновлении статуса: {e}")

    await _send_next_moderation_track(callback.message, int(admin_user["id"]))

# --- НОВАЯ ЛОГИКА ДЛЯ ТРЕЙЛЕРОВ ---
async def process_trailer(message: Message, file_obj: Any):
    """Логика сохранения трейлера"""
    try:
        text = message.reply_to_message.text
        # Извлекаем ID плейлиста с помощью регулярки для надежности
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

# --- ОБРАБОТЧИК АУДИО ---
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
            return await message.answer(f"❌ Ошибка: {e}")

        result = await add_points(int(user["id"]), points_reward, "upload_track")
        await message.answer(f"🎵 **{artist} — {title}**\nБитрейт: {bitrate} kbps | Длительность: {duration} сек.\n\n✅ Отправлен на проверку!\n💰 +{points_reward} поинтов.")
        await _notify_level_up(message, result)
    except Exception as exc:
        log(f"Ошибка обработки аудио: {exc}")
        await message.answer("Не удалось отправить трек.")

# --- НОВЫЙ ОБРАБОТЧИК ДОКУМЕНТОВ ---
@dp.message(F.document)
async def handle_document(message: Message) -> None:
    if message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
        
        # Если это ответ на запрос обложки
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
            
        # Если это ответ на запрос трейлера
        if "ID:" in text:
            await process_trailer(message, message.document)
            return
            
    await message.answer("Я пока учусь! Используй /help")

# --- ОБРАБОТЧИК ФОТО (ОБЛОЖКИ ПЛЕЙЛИСТОВ) ---
@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    if message.reply_to_message and message.reply_to_message.text and "ID:" in message.reply_to_message.text and "обложки" in message.reply_to_message.text:
        try:
            text = message.reply_to_message.text
            match = re.search(r"ID: (\d+)", text)
            if not match: 
                return await message.answer("❌ Не удалось распознать ID плейлиста.")
                
            playlist_id = int(match.group(1))
            
            # Берем картинку в максимальном разрешении
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

# --- ОБРАБОТЧИК ТЕКСТА (ССЫЛКИ НА SOUNDCLOUD) ---
@dp.message(F.text)
async def handle_text(message: Message) -> None:
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

    await _ensure_user(message)
    await message.answer("Я пока учусь! Используй /help")

async def main() -> None:
    if not TELEGRAM_BOT_TOKEN: sys.exit(1)
    log("Инициализация базы данных...")
    await init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎵 Открыть плеер", web_app=WebAppInfo(url=WEBAPP_STUB_URL)))
    await bot.set_my_commands(commands=[BotCommand(command="start", description="Главное меню"), BotCommand(command="profile", description="Мой профиль"), BotCommand(command="import", description="Импорт плейлиста"), BotCommand(command="search", description="Поиск в базе")], scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands=[BotCommand(command="admin", description="👑 Админ-панель"), BotCommand(command="moderate", description="⚙️ Модерация")], scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    asyncio.create_task(download_worker())
    log("Запуск бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): log("Бот остановлен.")