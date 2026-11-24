"""Telegram-бот FreeDom Music Bot на aiogram 3.x (Асинхронный)."""
from __future__ import annotations

import asyncio
import re
import sys
import subprocess
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as esc
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from backend.offline import offline_purge_worker, offline_worker
from backend.playlist_sync import job_active, playlist_sync_worker
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, 
    CallbackQuery, FSInputFile, LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, KeyboardButton, MenuButtonWebApp, 
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    InlineQuery, InlineQueryResultCachedAudio, BufferedInputFile
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from dotenv import load_dotenv

from backend.config import TELEGRAM_BOT_TOKEN, ADMIN_ID
from backend.artist_import import worker as artist_import_worker
from backend.artist_import_bot import register as register_artist_import
from backend.import_messages import PLAYLIST_IMPORT_INSTRUCTIONS
from backend.database import (
    get_hunt_tracks, get_user, import_playlist, init_db, upload_track, add_points,
    claim_daily, process_referral, get_pending_tracks, update_track_status,
    check_track_exists, check_download_limit, increment_download_count,
    reset_download_limit, set_daily_limit,
    search_tracks, get_track_by_id, increment_track_download,
    get_user_by_internal_id, give_premium, remove_premium,
    collect_premium_notices, mark_premium_notice, parse_premium_expiry,
    update_track_metadata, delete_track, replace_track_file,
    create_user_playlist, add_track_to_playlist,
    get_playlist_by_id, set_playlist_custom_cover,
    set_track_file_id, get_tracks_without_file_id,
    get_user_playlists,
    reserve_downloads, refund_downloads, get_import_liked_tracks, move_likes_to_top,
    add_import_like, get_cached_import_keys, normalize_artist, norm_key
)
from backend.playlist_media import TRAILER_MAX_BYTES, upload_playlist_trailer
from backend.parser import detect_source, parse_playlist, get_yandex_client, parse_single_track
from backend.downloader import (
    download_track_from_youtube, fix_audio_tags, FFMPEG_PATH, find_soundcloud_link,
    download_from_soundcloud_url, download_from_youtube_url, ensure_track_has_cover,
    download_track_from_direct_url, download_track_smart, extract_telegram_thumbnail
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
WEBAPP_STUB_URL: str = "https://freedomusic.us.ci"

session = AiohttpSession(timeout=300)
bot: Bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp: Dispatcher = Dispatcher()
register_artist_import(dp)

waiting_for_replacement = {}
processing_full_tracks = set()

pending_imports = {}

def log(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {text}")

def _telegram_id(message: Message) -> int:
    if message.from_user is None: raise ValueError("Не удалось определить пользователя Telegram")
    return message.from_user.id

async def _ensure_user(message: Message) -> dict:
    return await get_user(_telegram_id(message))

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
        if args == "import":
            await message.answer(PLAYLIST_IMPORT_INSTRUCTIONS, reply_markup=webapp_kb)
            return
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
        limit_info = await check_download_limit(int(user["id"]))
        is_paid = limit_info["is_premium"]
        
        limit_text = "∞" if is_paid else f"{limit_info['remaining']}/{limit_info['limit']}"
        
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
    expiry = await give_premium(int(user["id"]), 30)
    await message.answer(f"🎉 <b>Оплата прошла успешно!</b>\n\n{premium_status_text(expiry)}")


def premium_status_text(expiry: datetime) -> str:
    seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
    return f"Подписка активна. Осталось: {format_premium_remaining(seconds, include_zero=True)}"


def format_premium_remaining(seconds: float, *, include_zero: bool = False) -> str:
    days, minutes = divmod(max(0, ceil(seconds / 60)), 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    for value, forms in (
        (days, ("день", "дня", "дней")),
        (hours, ("час", "часа", "часов")),
        (minutes, ("минута" if include_zero else "минуту", "минуты", "минут")),
    ):
        if value or include_zero:
            last_digit = value % 10
            form = 2 if 11 <= value % 100 <= 14 else {1: 0, 2: 1, 3: 1, 4: 1}.get(last_digit, 2)
            parts.append(f"{value} {forms[form]}")
    return " ".join(parts)


async def check_premium_lifecycle() -> None:
    for user in await collect_premium_notices():
        expiry = parse_premium_expiry(user["premium_expires_at"])
        if expiry is None:
            continue
        if user["is_premium"]:
            seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
            if seconds <= 0:
                continue
            stage = next((threshold for threshold in (1, 24, 48, 72) if seconds <= threshold * 3600), None)
            previous = user["premium_notice_hours"]
            if stage is None or (previous is not None and previous <= stage):
                continue
            remaining = format_premium_remaining(seconds)
            text = f"⚠️ Ваш Premium закончится через {remaining}. Оффлайн-темы будут удалены."
        else:
            stage = 0
            text = "Premium истек. Темы удалены"
        try:
            await bot.send_message(user["telegram_id"], text, request_timeout=10)
        except TelegramForbiddenError:
            pass
        except Exception as exc:
            log(f"[Premium] Не удалось уведомить {user['telegram_id']}: {exc}")
            continue
        await mark_premium_notice(user, stage)


async def premium_lifecycle_worker() -> None:
    while True:
        started = monotonic()
        try:
            await check_premium_lifecycle()
        except Exception as exc:
            log(f"[Premium] Ошибка проверки подписок: {exc}")
        await asyncio.sleep(max(0, 30 - (monotonic() - started)))

# ============================================================
# ОЧЕРЕДЬ СКАЧИВАНИЯ (безопасный воркер с таймаутом)
# ============================================================
download_queue = asyncio.Queue()
import_status_tasks: set[asyncio.Task] = set()

@dataclass
class ImportProgress:
    chat_id: int
    message_id: int
    destination: str
    total: int
    processed: int = 0
    points: int = 0
    next_edit_at: float = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    track_statuses: dict[int, str] = field(default_factory=dict)

    async def edit(self, text: str, *, final: bool = False) -> None:
        for attempt in range(2 if final else 1):
            try:
                await bot.edit_message_text(
                    text=text, chat_id=self.chat_id, message_id=self.message_id, request_timeout=10,
                )
                return
            except TelegramRetryAfter as exc:
                self.next_edit_at = monotonic() + exc.retry_after + 1
                if final and attempt == 0:
                    await asyncio.sleep(exc.retry_after + 1)
            except TelegramNetworkError as exc:
                log(f"[Импорт] Ошибка обновления сообщения: {exc}")
            except TelegramBadRequest as exc:
                detail = exc.message.lower()
                if final and ("message to edit not found" in detail or "message can't be edited" in detail):
                    await safe_send(self.chat_id, text)
                elif "message is not modified" not in detail:
                    log(f"[Импорт] Ошибка обновления сообщения: {exc}")
                return
            except Exception as exc:
                log(f"[Импорт] Ошибка обновления сообщения: {exc}")
                return

    async def update(self) -> None:
        if self.processed == self.total:
            await asyncio.sleep(max(0, self.next_edit_at - monotonic()))
            added = sum(status == "approved" for status in self.track_statuses.values())
            moderation = sum(status == "pending" for status in self.track_statuses.values())
            await self.edit(
                f"✅ Импорт завершен! Добавлено в {esc(self.destination)}: {added}. "
                f"На модерации: {moderation}.\n"
                f"Вы получили {self.points} поинтов за добавление новых треков",
                final=True,
            )
        elif monotonic() >= self.next_edit_at:
            percent = self.processed * 100 // self.total
            filled = percent // 10
            bar = "▓" * filled + "░" * (10 - filled)
            await self.edit(f"⏳ Загрузка: [{bar}] {percent}% ({self.processed}/{self.total})")

    async def report(self) -> None:
        if self.total:
            await self.edit(f"⏳ Начинаю загрузку {self.total} треков в {esc(self.destination)}")
            while self.processed < self.total:
                await self.changed.wait()
                self.changed.clear()
                if self.processed < self.total:
                    await self.update()
        await self.update()


async def download_import_audio(artist, title, duration, direct_url=None, skip_soundcloud=False):
    if direct_url:
        path, _, source_url = await download_track_from_direct_url(direct_url, artist, title)
    elif skip_soundcloud:
        path, source_url = None, None
    else:
        path, _, source_url = await download_track_smart({"artist": artist, "title": title, "duration": duration})
    if not path:
        return None, None, 0, 0
    await asyncio.to_thread(ensure_track_has_cover, str(path), artist, title)
    await asyncio.to_thread(fix_audio_tags, path, artist, title)
    bitrate, actual_duration = await asyncio.to_thread(get_audio_info, Path(path))
    return path, source_url, bitrate, actual_duration


async def process_task(task_data):
    user_id = task_data['user_id']
    progress = task_data['progress']
    artist, title = task_data['artist'], task_data['title']
    orig_dur = task_data.get('original_duration', 0)
    imp_src = task_data.get('import_source', 'unknown')
    pending_playlist_id = task_data.get('pending_playlist_id')
    pending_position = task_data.get('pending_position', 0)
    direct_url = task_data.get('direct_url')
    skip_soundcloud = task_data.get('skip_soundcloud', False)
    liked_after = task_data.get('liked_after')

    async def refund_reserved():
        await refund_downloads(user_id, task_data['reserved'], task_data['quota_date'])
        task_data['reserved'] = 0

    try:
        if liked_after is not None:
            existing = await check_track_exists(artist, title)
            if existing and existing > 0:
                track = await get_track_by_id(existing)
                if track and await add_import_like(user_id, existing, liked_after):
                    liked_after.append(existing)
                    progress.track_statuses[existing] = track["status"]
                await refund_reserved()
                return
            if not task_data['reserved']:
                return

        file_path, source_url, bitrate, duration = await download_import_audio(
            artist, title, orig_dur, direct_url, skip_soundcloud
        )

        if file_path:
            if duration < 60:
                Path(file_path).unlink()
                await refund_reserved()
            else:
                detected_source = "hitmo" if "hitmo" in (source_url or "").lower() else "soundcloud"

                saved = await upload_track(
                    user_id=user_id, title=title, artist=artist,
                    file_path=file_path, source=detected_source,
                    bitrate=bitrate, duration=duration,
                    original_duration=orig_dur, import_source=imp_src,
                    pending_playlist_id=pending_playlist_id,
                    pending_position=pending_position,
                    source_url=source_url, import_liked_after=liked_after
                )
                task_data['reserved'] = 0
                progress.track_statuses[saved["id"]] = saved["status"]
                if liked_after is not None and saved["import_like_added"]:
                    liked_after.append(saved["id"])
                reward = 5
                await add_points(user_id, reward, "import_track")
                progress.points += reward

        else:
            saved = await upload_track(
                user_id=user_id, title=title, artist=artist,
                file_path="", source="failed_download",
                bitrate=0, duration=0,
                original_duration=orig_dur, import_source=imp_src,
                pending_playlist_id=pending_playlist_id,
                pending_position=pending_position, import_liked_after=liked_after
            )
            progress.track_statuses[saved["id"]] = saved["status"]
            if liked_after is not None and saved["import_like_added"]:
                liked_after.append(saved["id"])
            await refund_reserved()
            
    except ValueError:
        await refund_reserved()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log(f"[process_task] Ошибка: {e}")
        await refund_reserved()

async def request_sync_download(job: dict, item: dict) -> None:
    result = asyncio.get_running_loop().create_future()
    await download_queue.put({"sync_job": job, "item": item, "result": result})
    try:
        while not result.done():
            await asyncio.wait({result}, timeout=30)
            if not result.done() and not await job_active(job, renew=True):
                return
        result.result()
    finally:
        result.cancel()


async def process_sync_download(job: dict, item: dict, result: asyncio.Future) -> None:
    if result.cancelled() or not await job_active(job):
        return
    artist, title = item["artist"], item["title"]
    if (await check_track_exists(artist, title) or 0) > 0:
        return

    path = None
    keep_file = False
    try:
        path, source_url, bitrate, duration = await download_import_audio(artist, title, item["duration"])
        if result.cancelled() or not await job_active(job):
            return
        usable = path and duration >= 60
        # If an interruption lands during commit, leave the file for the saved catalog row.
        keep_file = bool(usable)
        source = "hitmo" if "hitmo" in (source_url or "").lower() else "soundcloud"
        try:
            saved = await upload_track(
                user_id=job["user_id"], title=title, artist=artist, file_path=path if usable else "",
                source=source if usable else "failed_download", bitrate=bitrate if usable else 0,
                duration=duration if usable else 0, original_duration=item["duration"], import_source="yandex",
                source_url=source_url if usable else None, automatic=True,
            )
        except ValueError:
            keep_file = False
            raise
        keep_file = path and saved["file_path"] == path
    finally:
        if path and not keep_file:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                log(f"[Автосинхронизация] Не удалось удалить временный файл: {exc}")


async def send_sync_notice(telegram_id: int, message: str) -> None:
    try:
        await bot.send_message(telegram_id, message, parse_mode=None, request_timeout=15)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log(f"[Автосинхронизация] Чат недоступен: {type(exc).__name__}")


async def download_worker():
    log("Воркер скачивания запущен...")
    while True:
        task_data = await download_queue.get()
        if "sync_job" in task_data:
            result = task_data["result"]
            try:
                await asyncio.wait_for(process_sync_download(task_data["sync_job"], task_data["item"], result), timeout=600)
            except Exception as exc:
                if not result.done():
                    result.set_exception(exc)
            else:
                if not result.done():
                    result.set_result(None)
            finally:
                download_queue.task_done()
            continue
        try:
            await asyncio.wait_for(process_task(task_data), timeout=600)
        except Exception as e:
            log(f"[Воркер] {task_data['title']}: {type(e).__name__}: {e!r}")
            try:
                await refund_downloads(task_data['user_id'], task_data['reserved'], task_data['quota_date'])
            except Exception as exc:
                log(f"[Воркер] Ошибка возврата лимита: {exc}")
        finally:
            download_queue.task_done()
        progress = task_data['progress']
        progress.processed += 1
        progress.changed.set()
        await asyncio.sleep(1)
# ============================================================

def import_destination_menu(total_tracks: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📦 <b>Импорт подготовлен!</b>\n"
        f"Треков к загрузке: {total_tracks}\n\n"
        "👇 <b>Куда добавить эти треки?</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Мне нравится", callback_data="import_dest_liked")],
        [InlineKeyboardButton(text="📁 В плейлист", callback_data="import_dest_playlists")],
        [InlineKeyboardButton(text="🌐 Только в базу", callback_data="import_dest_db_only")],
    ])
    return text, keyboard


async def show_import_destination_menu(
    message: Message,
    user_id: int,
    tracks: list,
    quota_date: str | None,
    is_single: bool = False,
    source_type: str = "unknown",
    direct_url: str = None,
    liked_tracks: list | None = None,
):
    reserved_count = len(tracks)
    text, keyboard = import_destination_menu(reserved_count)
    try:
        menu = await message.answer(text, reply_markup=keyboard)
    except Exception:
        await refund_downloads(user_id, reserved_count, quota_date)
        raise

    previous = pending_imports.get(user_id)
    pending_imports[user_id] = {
        "tracks": tracks,
        "liked_tracks": liked_tracks if liked_tracks is not None else tracks,
        "is_single": is_single,
        "source_type": source_type,
        "direct_url": direct_url,
        "reserved_count": reserved_count,
        "quota_date": quota_date,
        "menu": (menu.chat.id, menu.message_id),
        "step": "destination",
    }
    if previous and previous["step"] not in {"creating", "starting"}:
        await refund_downloads(user_id, previous["reserved_count"], previous["quota_date"])


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

    limit_info = await check_download_limit(int(user["id"]))
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    parsed_tracks = []
    liked_tracks = []
    
    for line in lines:
        track = parse_smart_line(line)
        if track:
            liked_tracks.append(track)
            existing_id = await check_track_exists(track["artist"], track["title"])
            if existing_id is None or existing_id == -1:
                if existing_id == -1:
                    track['skip_soundcloud'] = True
                parsed_tracks.append(track)
                
    if not liked_tracks:
        return await message.answer("🤷‍♂️ Не найдено новых треков для импорта.")

    tracks_to_import = parsed_tracks[:limit_info.get("remaining", 0)]

    granted, quota_date = await reserve_downloads(int(user["id"]), len(tracks_to_import))
    tracks_to_import = tracks_to_import[:granted]

    await show_import_destination_menu(
        message, int(user["id"]), tracks=tracks_to_import, quota_date=quota_date,
        source_type="text_import", liked_tracks=liked_tracks
    )

@dp.message(Command("import"))
async def cmd_import(message: Message, command: CommandObject) -> None:
    try:
        user = await _ensure_user(message)
        url = (command.args or "").strip()
        if not url: return await message.answer("Укажи ссылку: <code>/import &lt;ссылка&gt;</code>")
        
        limit_info = await check_download_limit(int(user["id"]))
        remaining_limit = limit_info.get("remaining", 0)

        single_track = await asyncio.to_thread(parse_single_track, url)
        if single_track:
            try: src_type = detect_source(url)
            except ValueError: src_type = "direct"

            granted, quota_date = await reserve_downloads(int(user["id"]), 1)
            await show_import_destination_menu(
                message, int(user["id"]), tracks=[single_track] if granted else [], quota_date=quota_date,
                is_single=True, source_type=src_type, direct_url=url, liked_tracks=[single_track]
            )
            return

        remaining_text = "∞" if remaining_limit is None else str(remaining_limit)
        await message.answer(f"🔎 Сканирую плейлист... (Доступно сегодня: {remaining_text})")
        tracks, _ = await asyncio.to_thread(parse_playlist, url)
        source_type = detect_source(url)
        if not tracks: return await message.answer("Треков не найдено.")
        
        tracks_to_import = []
        for track in tracks:
            if remaining_limit is not None and len(tracks_to_import) >= remaining_limit:
                break
            artist, title = track.get('artist', 'Unknown'), track.get('title', 'Unknown')
            
            existing_track_id = await check_track_exists(artist, title)
            
            if existing_track_id is None or existing_track_id == -1:
                if existing_track_id == -1:
                    track['skip_soundcloud'] = True
                tracks_to_import.append(track)
                
        granted, quota_date = await reserve_downloads(int(user["id"]), len(tracks_to_import))
        tracks_to_import = tracks_to_import[:granted]

        await show_import_destination_menu(
            message, int(user["id"]), tracks=tracks_to_import, quota_date=quota_date,
            source_type=source_type, liked_tracks=tracks
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
    liked_tracks = []
    
    for t in all_tracks:
        if t['url'] in seen_urls:
            continue
        seen_urls.add(t['url'])
        liked_tracks.append(t)
        
        artist = t.get('artist', 'Unknown')
        title = t.get('title', 'Unknown')
        existing_id = await check_track_exists(artist, title)
        
        if existing_id and existing_id > 0:
            continue
        
        t['skip_soundcloud'] = True
        unique_tracks.append(t)
            
    if not liked_tracks:
        return await status_msg.edit_text("❌ Не удалось найти треки на этой странице. Проверь ссылку.")
        
    limit_info = await check_download_limit(int(user["id"]))
    remaining = limit_info.get("remaining", 0)
    tracks_to_import = unique_tracks[:remaining]
    
    granted, quota_date = await reserve_downloads(int(user["id"]), len(tracks_to_import))
    tracks_to_import = tracks_to_import[:granted]

    if len(unique_tracks) > granted:
        await status_msg.edit_text(f"⚠️ Найдено {len(unique_tracks)} новых треков, но твой лимит позволяет скачать только {granted}.")
    else:
        await status_msg.edit_text(f"✅ Найдено {len(liked_tracks)} треков.")

    await show_import_destination_menu(
        message, int(user["id"]), tracks=tracks_to_import, quota_date=quota_date,
        source_type="hitmo_artist", liked_tracks=liked_tracks
    )

@dp.callback_query(F.data.startswith("import_dest_"))
async def process_import_destination(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    message = callback.message
    if not user or not isinstance(message, Message):
        return await callback.answer("Меню импорта недоступно.", show_alert=True)

    user_id = int(user["id"])
    data = pending_imports.get(user_id)
    if not data or data["menu"] != (message.chat.id, message.message_id):
        return await callback.answer("Меню устарело. Отправь команду заново.", show_alert=True)

    action = callback.data.removeprefix("import_dest_")
    step = data["step"]
    if step == "destination" and action == "liked":
        return await prepare_liked_import(callback, message, user_id, data)
    if step == "destination" and not data["tracks"] and action in {"playlists", "db_only"}:
        return await callback.answer("Нет новых треков, доступных для скачивания.", show_alert=True)
    if step == "destination" and action == "playlists":
        playlists = {
            pl["id"]: pl["title"]
            for pl in await get_user_playlists(user_id)
            if not pl.get("original_playlist_id")
        }
        if pending_imports.get(user_id) is not data or data["step"] != step:
            return await callback.answer("Меню импорта уже изменилось.", show_alert=True)

        buttons = [
            [InlineKeyboardButton(text=f"📁 {title}", callback_data=f"import_dest_pl_{playlist_id}")]
            for playlist_id, title in playlists.items()
        ]
        buttons.extend([
            [InlineKeyboardButton(text="➕ Создать новый плейлист", callback_data="import_dest_new")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="import_dest_back")],
        ])
        data["playlists"] = playlists
        text = "📁 <b>Выбери плейлист</b>" if playlists else "📁 У тебя пока нет плейлистов."
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        next_step = "playlists"
    elif step == "playlists" and action == "back":
        text, keyboard = import_destination_menu(data["reserved_count"])
        next_step = "destination"
    elif step == "playlists" and action == "new":
        text = "✍️ Отправь название нового плейлиста текстовым сообщением."
        keyboard = None
        next_step = "title"
    else:
        playlist_id = None
        if step == "destination" and action == "db_only":
            destination = "базу"
        elif step == "playlists" and action.startswith("pl_") and action[3:].isascii() and action[3:].isdigit():
            playlist_id = int(action[3:])
            destination = data["playlists"].get(playlist_id)
            if destination is None:
                return await callback.answer("Плейлист недоступен.", show_alert=True)
        else:
            return await callback.answer("Этот вариант сейчас недоступен.", show_alert=True)

        del pending_imports[user_id]
        await execute_import(data, user_id, playlist_id, destination)
        await callback.answer()
        return

    data["step"] = "editing"
    try:
        await callback.answer()
        await message.edit_text(text, reply_markup=keyboard)
    except Exception:
        data["step"] = step
        raise
    data["step"] = next_step


async def prepare_liked_import(callback: CallbackQuery, message: Message, user_id: int, data: dict):
    data["step"] = "checking_likes"
    try:
        liked = await get_import_liked_tracks(user_id)
        if pending_imports.get(user_id) is not data:
            return await callback.answer("Меню импорта уже изменилось.", show_alert=True)

        seen = set()
        duplicates = []
        tracks = []
        for track in data["liked_tracks"]:
            key = import_track_key(track)
            if key in seen:
                continue
            seen.add(key)
            if key in liked:
                duplicates.append(liked[key])
            else:
                tracks.append(track)

        data["new_liked_tracks"] = tracks
        data["duplicate_ids"] = duplicates
        if duplicates:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬆️ Переместить наверх", callback_data="import_dup_move")],
                [InlineKeyboardButton(text="⏭ Пропустить (оставить на месте)", callback_data="import_dup_skip")],
            ])
            await message.edit_text(
                "⚠️ В вашем импорте есть треки, которые вы уже лайкнули. Что сделать с ними?",
                reply_markup=keyboard,
            )
            data["step"] = "duplicates"
            await callback.answer()
        else:
            await finish_liked_import(callback, user_id, data)
    finally:
        if data["step"] == "checking_likes":
            data["step"] = "destination"


@dp.callback_query(F.data.in_({"import_dup_move", "import_dup_skip"}))
async def process_import_duplicates(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    message = callback.message
    if not user or not isinstance(message, Message):
        return await callback.answer("Меню импорта недоступно.", show_alert=True)

    user_id = int(user["id"])
    data = pending_imports.get(user_id)
    if not data or data["step"] != "duplicates" or data["menu"] != (message.chat.id, message.message_id):
        return await callback.answer("Меню устарело или выбор уже обработан.", show_alert=True)

    data["step"] = "resolving_duplicates"
    move = callback.data == "import_dup_move"
    try:
        if move:
            await move_likes_to_top(user_id, data["duplicate_ids"])
        if pending_imports.get(user_id) is not data:
            return await callback.answer("Меню импорта уже изменилось.", show_alert=True)
        await finish_liked_import(callback, user_id, data, moved=move)
    finally:
        if data["step"] == "resolving_duplicates":
            data["step"] = "duplicates"


async def finish_liked_import(callback: CallbackQuery, user_id: int, data: dict, *, moved: bool = False):
    reserved = {id(track) for track in data["tracks"]}
    tracks = data["new_liked_tracks"]
    cached = await get_cached_import_keys([import_track_key(track) for track in tracks if id(track) not in reserved])
    if pending_imports.get(user_id) is not data:
        return await callback.answer("Меню импорта уже изменилось.", show_alert=True)
    tracks = [track for track in tracks if id(track) in reserved or import_track_key(track) in cached]
    used = sum(id(track) in reserved for track in tracks)
    previous_step = data["step"]
    data["step"] = "starting"
    try:
        await refund_downloads(user_id, data["reserved_count"] - used, data["quota_date"])
    except Exception:
        data["step"] = previous_step
        if pending_imports.get(user_id) is not data:
            await refund_downloads(user_id, data["reserved_count"], data["quota_date"])
        raise
    if pending_imports.get(user_id) is data:
        del pending_imports[user_id]
    moved_likes = tuple(data["duplicate_ids"]) if moved else ()
    await execute_import({**data, "tracks": tracks}, user_id, None, "«Мне нравится»",
                         reserved=reserved, moved_likes=moved_likes)
    await callback.answer()


def import_track_key(track: dict) -> tuple[str, str]:
    return (norm_key(normalize_artist(track.get("artist", "Unknown"))),
            norm_key(track.get("title", "Unknown")))


async def execute_import(
    data: dict, user_id: int, playlist_id: int | None, destination: str, *,
    reserved: set[int] | None = None, moved_likes: tuple[int, ...] = (),
):
    tracks = data["tracks"]
    quota_date = data["quota_date"]
    if tracks and quota_date is None:
        downloadable = tracks if reserved is None else [track for track in tracks if id(track) in reserved]
        granted, quota_date = await reserve_downloads(user_id, len(downloadable))
        if reserved is None:
            tracks = tracks[:granted]
        else:
            reserved = {id(track) for track in downloadable[:granted]}
    is_single = data["is_single"]
    source_type = data["source_type"]
    direct_url = data.get("direct_url")
    progress = ImportProgress(*data["menu"], destination, len(tracks))
    reporter = asyncio.create_task(progress.report())
    import_status_tasks.add(reporter)
    reporter.add_done_callback(import_status_tasks.discard)
    # В Move сначала идут перемещённые лайки. Далее воркер дописывает только новые лайки этой партии.
    liked_after = list(moved_likes) if reserved is not None else None
    
    for idx, track in enumerate(tracks):
        task = {
            'user_id': user_id, 'progress': progress,
            'artist': track.get('artist', 'Unknown'), 'title': track.get('title', 'Unknown'),
            'original_duration': track.get('duration', 0), 'import_source': source_type,
            'pending_playlist_id': playlist_id,
            'pending_position': idx,
            'liked_after': liked_after,
            'reserved': int(reserved is None or id(track) in reserved),
            'quota_date': quota_date,
            'skip_soundcloud': track.get('skip_soundcloud', False)
        }
        
        if is_single and source_type == "soundcloud" and direct_url:
            task['direct_url'] = direct_url
            
        await download_queue.put(task)
        
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
    limit_info = await check_download_limit(int(user["id"]))
    if not limit_info["allowed"]:
        return await callback.answer("⛔ Лимит исчерпан", show_alert=True)
    track = await get_track_by_id(track_id)
    if not track or not track["file_path"] or not Path(track["file_path"]).exists(): return await callback.answer("Трек не найден.", show_alert=True)
    await callback.answer("Отправляю файл...")
    try:
        await callback.message.answer_audio(audio=FSInputFile(Path(track["file_path"])), caption=f"🎵 {esc(track['artist'])} — {esc(track['title'])}", title=track['title'], performer=track['artist'])
        if not limit_info["is_premium"]:
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
        limit_info = await check_download_limit(int(user["id"]))
        if not limit_info["allowed"]: return await message.answer("⛔ Лимит исчерпан")
        track = await get_track_by_id(track_id)
        if not track or not track["file_path"] or not Path(track["file_path"]).exists(): return await message.answer("❌ Трек или файл не найден.")
        await message.answer_audio(audio=FSInputFile(Path(track["file_path"])), caption=f"🎵 {esc(track['artist'])} — {esc(track['title'])}", title=track['title'], performer=track['artist'])
        if not limit_info["is_premium"]:
            await increment_download_count(int(user["id"]), 1)
        await increment_track_download(track_id)
        if track['uploaded_by'] != int(user["id"]):
            await add_points(track['uploaded_by'], 1, f"track_download_{track_id}")
    except Exception as e:
        log(f"Ошибка /get_track: {e}")

@dp.message(Command("admin"))
async def cmd_admin_panel(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎧 Модерация", callback_data="admin_moderate")],
        [InlineKeyboardButton(text="🎵 Импорт артистов", callback_data="yaimp:list")],
        [InlineKeyboardButton(text="📊 Баланс Stars", callback_data="admin_stars")],
        [InlineKeyboardButton(text="📖 Справка", callback_data="admin_help")],
    ])
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
            "<code>/admin</code>\n"
            "<code>/moderate</code>\n"
            "<code>/import_artist_yandex [ID или ссылка]</code> — партии импорта и модерация артистов\n"
            "<code>/deletetrack &lt;id&gt;</code>\n"
            "<code>/addlimit &lt;id&gt; &lt;число&gt;</code>\n"
            "<code>/resetlimit</code>\n"
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
    else: await message.answer("Использование: <code>/addlimit [&lt;id&gt;] &lt;число&gt;</code>")

@dp.message(Command("givepremium"))
async def cmd_givepremium(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip().split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit(): return await message.answer("Использование: <code>/givepremium &lt;id&gt; &lt;дней&gt;</code>")
    target = await get_user(int(args[0]))
    if not target: return await message.answer("Пользователь не найден.")
    days = int(args[1])
    if days == 0:
        return await message.answer("Укажи срок подписки от одного дня.")
    expiry = await give_premium(int(target["id"]), days)
    status = premium_status_text(expiry)
    await message.answer(f"✅ Выдан Премиум на {days} дней.\n{status}")
    if target["telegram_id"] != message.chat.id:
        await safe_send(target["telegram_id"], status, request_timeout=10)

@dp.message(Command("removepremium"))
async def cmd_removepremium(message: Message, command: CommandObject) -> None:
    if message.from_user.id != ADMIN_ID: return
    args = (command.args or "").strip().split()
    if len(args) != 1 or not args[0].isdigit(): return await message.answer("Использование: <code>/removepremium &lt;id&gt;</code>")
    target = await get_user(int(args[0]))
    if not target: return await message.answer("Пользователь не найден.")
    await remove_premium(int(target["id"]))
    await message.answer("✅ Premium снят. Оффлайн-темы поставлены на удаление.")

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
        
    status_msg = await message.answer(f"⏳ Начинаю обработку {len(tracks)} треков. Это займет время...")
    
    success_count = 0
    fail_count = 0
    sent_messages = [] 

    for i, track in enumerate(tracks):
        file_path = Path(track["file_path"])
        if not file_path.exists():
            fail_count += 1
            continue
            
        # 1. Проверяем/вшиваем обложку в файл (на всякий случай)
        await asyncio.to_thread(ensure_track_has_cover, str(file_path), track["artist"], track["title"])
        
        # 2. ДОСТАЕМ ОБЛОЖКУ ДЛЯ ТЕЛЕГРАМА
        cover_bytes = await asyncio.to_thread(extract_telegram_thumbnail, str(file_path))
        thumbnail = BufferedInputFile(cover_bytes, filename="cover.jpg") if cover_bytes else None
            
        msg = None
        try:
            # 3. Отправляем с явным параметром thumbnail
            msg = await bot.send_audio(
                chat_id=message.chat.id,
                audio=FSInputFile(file_path),
                thumbnail=thumbnail, # <--- ГЛАВНАЯ МАГИЯ
                title=track['title'], 
                performer=track['artist'],
                disable_notification=True
            )
            
            if msg.audio and msg.audio.file_id:
                await set_track_file_id(track["id"], msg.audio.file_id)
                success_count += 1
                sent_messages.append(msg.message_id)
            else:
                fail_count += 1
                
        except Exception as e:
            log(f"Ошибка кеширования трека {track['id']}: {e}")
            fail_count += 1
            
        await asyncio.sleep(1.5) 
        
        if (i + 1) % 10 == 0:
            try:
                await status_msg.edit_text(f"⏳ Прогресс: {i + 1} / {len(tracks)}...\n✅ Успешно: {success_count} | ❌ Ошибок: {fail_count}")
            except: pass
            
    # Удаляем все отправленные сообщения пачками по 100 штук в конце
    for i in range(0, len(sent_messages), 100):
        chunk = sent_messages[i:i+100]
        try:
            await bot.delete_messages(chat_id=message.chat.id, message_ids=chunk)
        except Exception:
            pass

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
            f"📥 <b>Отправь мне MP3-файл</b> (как документ или аудио), или <b>пришли прямую ссылку на SoundCloud или YouTube</b>.\n\n"
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

def is_trailer_reply(message: Message) -> bool:
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == bot.id and
                (reply.text or "").startswith("🎵 Отправь мне аудиофайл для трейлера плейлиста"))


async def process_trailer(message: Message, file_obj: Any):
    try:
        text = message.reply_to_message.text
        match = re.search(r"\(ID: (\d+)\)\n", text)
        if not match: 
            await message.answer("❌ Не удалось распознать ID плейлиста. Попробуй заново.")
            return
            
        playlist_id = int(match.group(1))
        
        if file_obj.file_size and file_obj.file_size > TRAILER_MAX_BYTES:
            await message.answer("❌ Файл слишком большой! Максимум 3 МБ.")
            return
            
        user = await _ensure_user(message)
        playlist = await get_playlist_by_id(playlist_id)
        if not playlist or playlist["user_id"] != user["id"] or playlist["is_system"] or playlist["original_playlist_id"]:
            return await message.answer("Трейлер может менять только автор плейлиста.")

        file = await bot.download(file_obj)
        await upload_playlist_trailer(playlist_id, user["id"], file.getvalue())
        await message.answer("Трейлер сохранён и выбран для предпросмотра.")
    except (ValueError, PermissionError) as exc:
        await message.answer(esc(str(exc)))
    except Exception as e:
        log(f"Ошибка сохранения трейлера: {e}")
        await message.answer("❌ Ошибка при сохранении трейлера. Попробуй еще раз.")

@dp.message(F.audio)
async def handle_audio(message: Message) -> None:
    if is_trailer_reply(message):
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
            
        if is_trailer_reply(message):
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

    user_id = int(user["id"])
    data = pending_imports.get(user_id)
    if data and data["step"] == "title" and data["menu"][0] == message.chat.id:
        title = message.text.strip()
        if not title:
            return await message.answer("✍️ Название не должно быть пустым. Отправь его ещё раз.")

        data["step"] = "creating"
        try:
            new_pl = await create_user_playlist(user_id, title)
        except Exception:
            if pending_imports.get(user_id) is data:
                data["step"] = "title"
            else:
                await refund_downloads(user_id, data["reserved_count"], data["quota_date"])
            raise

        if pending_imports.get(user_id) is data:
            del pending_imports[user_id]
        await execute_import(data, user_id, new_pl["id"], new_pl["title"])
        return

    if message.from_user.id in waiting_for_replacement:
        url = message.text.strip()
        is_youtube = False
        try:
            parsed_url = urlsplit(url if "://" in url else f"https://{url.lstrip('/')}")
            host = parsed_url.hostname or ""
            is_youtube = parsed_url.scheme in ("http", "https") and (
                host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com")
            )
        except ValueError:
            pass

        if is_youtube or "soundcloud.com" in url:
            if is_youtube:
                url = parsed_url.geturl()
            downloader = download_from_youtube_url if is_youtube else download_from_soundcloud_url
            youtube_error = "❌ Не удалось скачать с YouTube"
            track_id = waiting_for_replacement.pop(message.from_user.id)
            track = await get_track_by_id(track_id)
            
            if not track:
                return await message.answer("❌ Трек не найден в базе.")
                
            await message.answer("⏳ Скачиваю трек по ссылке...")
            
            try:
                file_path, _, src_url = await downloader(url, track["artist"], track["title"])
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
                    waiting_for_replacement.setdefault(message.from_user.id, track_id)
                    await message.answer(
                        youtube_error if is_youtube else
                        "❌ Не удалось скачать трек по этой ссылке (файл поврежден или пустой). "
                        "Скорее всего, SoundCloud его заблокировал. Попробуй скинуть файлом."
                    )
            except Exception as e:
                log(f"Ошибка скачивания по ссылке: {e}")
                waiting_for_replacement.setdefault(message.from_user.id, track_id)
                await message.answer(
                    youtube_error if is_youtube else "❌ Ошибка сервера при скачивании. Попробуй скинуть файлом."
                )
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

async def register_bot_commands() -> None:
    public_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="search", description="Поиск в базе"),
        BotCommand(command="import", description="Импорт плейлиста"),
        BotCommand(command="import_text", description="Импорт текстом (ВК, Spotify)"),
        BotCommand(command="import_artist_hitmo", description="Импорт всех треков артиста с Hitmo"),
        BotCommand(command="daily", description="Забрать ежедневный бонус"),
        BotCommand(command="invite", description="Получить реферальную ссылку"),
        BotCommand(command="buypremium", description="Купить Premium"),
        BotCommand(command="help", description="Справка по командам"),
    ]
    await bot.set_my_commands(commands=public_commands, scope=BotCommandScopeDefault())
    if ADMIN_ID > 0:
        admin_commands = [
            BotCommand(command="admin", description="Админ-панель"),
            BotCommand(command="moderate", description="Модерация треков"),
            BotCommand(command="import_artist_yandex", description="Импорт артиста из Яндекс Музыки и проверка аудио"),
            BotCommand(command="deletetrack", description="Удалить трек · <id трека>"),
            BotCommand(command="addlimit", description="Установить лимит · [<id пользователя>] <число>"),
            BotCommand(command="resetlimit", description="Сбросить свой счётчик скачиваний"),
            BotCommand(command="givepremium", description="Выдать Premium · <id пользователя> <дней>"),
            BotCommand(command="removepremium", description="Снять Premium · <id пользователя>"),
            BotCommand(command="starbalance", description="Баланс Telegram Stars"),
            BotCommand(command="cache_all", description="Кешировать треки и обложки"),
        ]
        await bot.set_my_commands(commands=admin_commands + public_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN: sys.exit(1)
    log("Инициализация базы данных...")
    await init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎵 Открыть плеер", web_app=WebAppInfo(url=WEBAPP_STUB_URL)))
    await register_bot_commands()
    asyncio.create_task(download_worker())
    asyncio.create_task(offline_worker(bot))
    asyncio.create_task(offline_purge_worker(bot))
    asyncio.create_task(premium_lifecycle_worker())
    sync_worker = asyncio.create_task(playlist_sync_worker(request_sync_download, send_sync_notice))
    artist_worker = asyncio.create_task(artist_import_worker())
    log("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        sync_worker.cancel()
        artist_worker.cancel()
        await asyncio.gather(sync_worker, artist_worker, return_exceptions=True)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): log("Бот остановлен.")
