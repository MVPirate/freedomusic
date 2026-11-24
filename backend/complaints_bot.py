"""Complaint review inside /moderate."""
from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from pathlib import Path

from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery, ForceReply, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backend import artist_import, complaints, database as db
from backend.artist_import_bot import format_duration, is_admin_private
from backend.moderation_store import REASONS
from backend.moderation_menu import render_panel

logger = logging.getLogger(__name__)
FILE_REPLY = re.compile(r"\nЗамена по жалобе #(\d+)\.(\d+)\Z")


def button(text, action):
    return InlineKeyboardButton(text=text, callback_data=action)


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[item] for item in rows])


async def show_queue(message, after=0):
    rows = await complaints.queue(after)
    buttons = [button(f"{row['title'][:44]} · {row['total']}", f"complaint:view:{row['id']}:0") for row in rows[:10]]
    if len(rows) > 10:
        buttons.append(button("Следующие треки →", f"complaint:list:{rows[9]['id']}"))
    if after:
        buttons.append(button("← К началу списка", "complaint:list:0"))
    buttons.append(button("← К модерации", "admin_moderate"))
    await render_panel(message, "<b>Жалобы на треки</b>" + ("\nВыберите трек." if rows else "\nОткрытых жалоб на этой странице нет."),
                       markup=keyboard(*buttons))


async def show_track(message, track_id, after):
    track = await db.get_track_by_id(track_id, approved_only=True)
    reports = await complaints.details(track_id, after)
    if not track or not reports:
        raise ValueError("Открытых жалоб на этот трек больше нет.")
    caption = f"<b>{escape(track['artist'][:140])} — {escape(track['title'][:180])}</b>"
    path = Path(track["file_path"] or "")
    if not after and path.is_file() and path.stat().st_size <= artist_import.MAX_AUDIO_BYTES:
        await message.answer_audio(FSInputFile(path), caption=f"{caption}\nТекущая версия · {format_duration(track['duration'])}")
    lines = [caption]
    for report in reports[:4]:
        name = f"@{report['username']}" if report["username"] else f"Telegram ID {report['telegram_id']}"
        lines.append(f"{escape(name)} · {REASONS[report['reason']]}\n"
                     f"<a href=\"{escape(report['reference_url'], quote=True)}\">Предложенная версия</a>")
    first, last = reports[0]["id"], reports[:4][-1]["id"]
    buttons = [button("Прислать MP3 на замену", f"complaint:file:{track_id}:{track['media_revision']}"),
               button("Закрыть показанные без замены", f"complaint:dismiss:{track_id}:{first}:{last}")]
    if len(reports) > 4:
        buttons.append(button("Ещё жалобы →", f"complaint:view:{track_id}:{reports[3]['id']}"))
    buttons.append(button("← К жалобам", "complaint:list:0"))
    await render_panel(message, "\n\n".join(lines), markup=keyboard(*buttons))


async def action(callback: CallbackQuery):
    message = callback.message
    if not isinstance(message, Message) or not is_admin_private(callback, message.chat):
        return await callback.answer("Только для администратора в личном чате.", show_alert=True)
    await callback.answer()
    _, name, *args = callback.data.split(":")
    try:
        if name == "list":
            await show_queue(message, int(args[0]))
        elif name == "view":
            await show_track(message, int(args[0]), int(args[1]))
        elif name == "file":
            track_id, revision = map(int, args)
            await message.answer(
                f"Ответьте на это сообщение MP3-файлом для сравнения.\nЗамена по жалобе #{track_id}.{revision}",
                reply_markup=ForceReply(),
            )
        elif name in {"dismiss", "replace"}:
            track_id = int(args[0])
            admin = await db.get_user(callback.from_user.id)
            if name == "replace":
                await complaints.resolve(track_id, admin["id"], token=args[1])
            else:
                await complaints.resolve(track_id, admin["id"], shown=(int(args[1]), int(args[2])))
            await message.answer("Аудио заменено, жалобы закрыты." if name == "replace" else "Жалобы закрыты.")
            await show_queue(message)
    except (LookupError, ValueError) as exc:
        await message.answer(escape(str(exc)))


def is_file_reply(message: Message) -> bool:
    reply = message.reply_to_message
    return bool(is_admin_private(message, message.chat) and reply and reply.from_user
                and reply.from_user.id == message.bot.id and FILE_REPLY.search(reply.text or ""))


async def receive_file(message: Message):
    track_id, revision = map(int, FILE_REPLY.search(message.reply_to_message.text).groups())
    file = message.audio or message.document
    track = await db.get_track_by_id(track_id, approved_only=True)
    if not file:
        return await message.answer("Пришлите MP3-файл ответом на сообщение о замене.")
    if not track or track["media_revision"] != revision:
        return await message.answer("Трек изменился. Откройте жалобу заново.")
    if file.file_size and file.file_size > artist_import.MAX_AUDIO_BYTES:
        return await message.answer("MP3 слишком большой для отправки на прослушивание.")
    path = artist_import.staged_path(track_id)
    saved = False
    try:
        await message.bot.download(file, destination=path)
        audio = await asyncio.to_thread(artist_import.prepare_audio, path, track, "Telegram")
        token = await complaints.stage(track_id, revision, audio)
        saved = True
        await message.answer_audio(
            FSInputFile(path), caption=f"Кандидат для трека #{track_id} · {format_duration(audio.duration)} · {audio.bitrate} кбит/с",
            reply_markup=keyboard(button("Подтвердить замену", f"complaint:replace:{track_id}:{token}"),
                                  button("← К жалобе", f"complaint:view:{track_id}:0")),
        )
    except asyncio.CancelledError:
        saved = True
        raise
    except ValueError as exc:
        await message.answer(escape(str(exc)))
    except Exception:
        logger.exception("Не удалось принять файл по жалобе на трек %s", track_id)
        await message.answer("Не удалось подготовить MP3. Попробуйте отправить его ещё раз.")
    finally:
        if not saved:
            artist_import.remove_staged(path)


def register(dispatcher: Dispatcher) -> None:
    dispatcher.message.register(receive_file, is_file_reply)
    dispatcher.callback_query.register(action, F.data.startswith("complaint:"))
