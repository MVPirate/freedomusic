"""Telegram administrator screens for artist import and listening review."""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from html import escape
from pathlib import Path
from uuid import uuid4

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery, ForceReply, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from backend import artist_import as imports, database as db
from backend.artist_catalog import parse_artist_id
from backend.config import ADMIN_ID
from backend.downloader import download_from_soundcloud_url, is_soundcloud_url

logger = logging.getLogger(__name__)
PREFIX = "yaimp"
REPLY_MARKER = re.compile(r"\nЗаявка FM #(\d+)\.(\d+)\Z")
DRAFT = {}
FETCHING = set()

HELP = (
    "<code>/import_artist_yandex ID_или_ссылка</code> — загрузить дискографию для предпросмотра.\n"
    "Чтобы выбрать другое имя страницы FreeDom Music, добавьте <code>| Имя страницы</code>."
)


def is_admin_private(event, chat) -> bool:
    return event.from_user is not None and event.from_user.id == ADMIN_ID and chat.type == "private"


def button(label: str, action: str, *parts) -> InlineKeyboardButton:
    data = ":".join([PREFIX, action, *map(str, parts)])
    return InlineKeyboardButton(text=label, callback_data=data)


def keyboard(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def short(text: str, size=150) -> str:
    return escape(text[:size] + ("…" if len(text) > size else ""))


async def show_batches(message: Message) -> None:
    jobs = await imports.batches()
    rows = [[button(job["artist_name"][:45], "status", job["id"])] for job in jobs]
    await message.answer(
        ("<b>Импорт артистов</b>\nВыберите партию, чтобы посмотреть ход поиска и разобрать кандидатов.\n\n"
         if rows else "<b>Импорт артистов</b>\n\n") + HELP,
        reply_markup=keyboard(*rows) if rows else None,
    )


async def command_import(message: Message, command: CommandObject) -> None:
    if not is_admin_private(message, message.chat):
        await message.answer("Команда доступна администратору в личном чате.")
        return
    args = (command.args or "").strip()
    if not args:
        await show_batches(message)
        return
    raw_id, _, name = args.partition("|")
    try:
        artist_id = parse_artist_id(raw_id)
    except argparse.ArgumentTypeError:
        await message.answer("Укажите ссылку на артиста Яндекс Музыки или его числовой ID.\n\n" + HELP)
        return
    admin = message.from_user.id
    if admin in FETCHING:
        await message.answer("Предыдущий запрос каталога ещё выполняется.")
        return
    FETCHING.add(admin)
    DRAFT.pop(admin, None)
    try:
        progress = await message.answer("Читаю дискографию и список треков Яндекс Музыки…")
        try:
            plan = await asyncio.to_thread(imports.read_plan, artist_id, name.strip())
        except ValueError as exc:
            await progress.edit_text(short(str(exc), 350))
            return
        except Exception:
            logger.exception("Не удалось прочитать каталог Яндекс Музыки")
            await progress.edit_text("Яндекс Музыка недоступна или вернула неполные данные. Повторите команду позже.")
            return
        token = uuid4().hex[:16]
        DRAFT[admin] = (token, plan)
        await progress.edit_text(
            f"<b>{short(plan.source_name)}</b> · Яндекс Музыка #{plan.artist_id}\n"
            f"Страница FreeDom Music — <b>{short(plan.local_name)}</b>\n"
            f"Релизов: {len(plan.releases)} · записей: {len(plan.tracks)}\n\n"
            "Бот сначала проверит локальную базу, затем поищет недостающие записи "
            "в SoundCloud и Hitmo. Найденные файлы откроются в очереди прослушивания.",
            reply_markup=keyboard(
                [button("Начать поиск аудио", "confirm", token), button("Отмена", "cancel", token)],
            ),
        )
    finally:
        FETCHING.discard(admin)


async def dashboard(message: Message, batch_id: int, *, edit=False) -> None:
    result = await imports.status(batch_id)
    if result is None:
        await message.answer("Партия импорта не найдена.")
        return
    batch, counts = result
    queued, searching, found, missing, published, rejected, existing = (
        counts.get(state, 0) for state in ("queued", "searching", "found", "missing", "approved", "rejected", "existing")
    )
    text = (
        f"<b>{short(batch['artist_name'])} · импорт #{batch_id}</b>\n"
        f"Всего записей: {sum(counts.values())}\n"
        f"В очереди: {queued} · идёт поиск: {searching}\n"
        f"На прослушивание: {found} · нужен файл: {missing}\n"
        f"Опубликовано: {published} · уже в базе: {existing} · отклонено: {rejected}"
    )
    if batch["waiting_for_review"]:
        text += f"\nЖдут решения в другой партии: {batch['waiting_for_review']}"
    if batch["paused"]:
        text += "\n\nПоиск приостановлен."
    rows = []
    if found:
        rows.append([button(f"🎧 Прослушать ({found})", "found", batch_id, 0)])
    if missing:
        rows.append([button(f"Нужен файл ({missing})", "missing", batch_id, 0),
                     button("Повторить поиск", "retry", batch_id)])
    if rejected:
        rows.append([button(f"Отклонённые ({rejected})", "rejected", batch_id, 0)])
    controls = [button("Обновить", "status", batch_id)]
    if batch["paused"]:
        controls.append(button("Продолжить", "resume", batch_id))
    elif queued or searching:
        controls.append(button("Пауза", "pause", batch_id))
    rows.extend([controls, [button("Все импорты", "list")]])
    markup = keyboard(*rows)
    if edit and message.text:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    await message.answer(text, reply_markup=markup)


def format_duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02}" if seconds else "неизвестно"


async def review_card(message: Message, item: dict) -> None:
    item_id, revision, batch_id = (item[key] for key in ("id", "attempt", "batch_id"))
    artist = short(item["artist"], 100)
    title = short(item["title"], 135)
    caption = f"<b>{artist} — {title}</b>\n"
    if item["release_title"]:
        caption += f"Релиз: {short(item['release_title'], 90)}\n"
    caption += f'<a href="https://music.yandex.ru/track/{item["source_id"]}">Метаданные Яндекс Музыки</a>\n'
    expected = round((item["duration_ms"] or 0) / 1000)
    state = item["state"]
    rows = []
    if state == "rejected":
        if await imports.duplicate_candidates(item):
            rows.append([button("Проверить трек из базы", "existing", item_id, revision)])
        rows.append([button("Следующий", state, batch_id, item_id), button("К партии", "status", batch_id)])
        await message.answer(caption + "Файл из импорта отклонён.", reply_markup=keyboard(*rows))
        return
    if state == "found" and item["file_path"] and Path(item["file_path"]).is_file():
        source = short(item["audio_source"])
        link = item["source_url"]
        if link and link.startswith(("https://", "http://")):
            source = f'<a href="{escape(link, quote=True)}">{source}</a>'
        actual = item["duration"] or 0
        caption += (
            f"Аудио: {source} · {item['bitrate']} кбит/с\n"
            f"Длительность: {format_duration(actual)} · Яндекс: {format_duration(expected)}\n"
        )
        if expected and abs(actual - expected) > 2:
            caption += f"⚠️ Разница длительности {abs(actual - expected)} с. Сверьте версию.\n"
        caption += "\nПрослушайте полную запись и проверьте, есть ли запики."
        rows.append([button("✅ Одобрить без запиков", "approve", item_id, revision)])
        media = FSInputFile(item["file_path"], filename=f"track-{item_id}.mp3")
    else:
        reasons = {
            "not_found": "Подходящий MP3 не найден", "wrong_version": "Найденная версия не совпала по длительности",
            "error": "Источники не дали пригодный MP3", "network": "Ошибка доступа к источнику",
        }
        problem = ("Подготовленный MP3 отсутствует" if state == "found"
                   else short(reasons.get(item["issue"], item["issue"] or "Подходящий MP3 не найден"), 150))
        caption += f"{problem}.\nЯндекс: {format_duration(expected)}"
        media = None
    rows.extend([
        [button("Прикрепить замену" if media else "Прикрепить MP3 или ссылку", "manual", item_id, revision),
         button("Отклонить", "reject", item_id, revision)],
        [button("Следующий", state, batch_id, item_id), button("К партии", "status", batch_id)],
    ])
    if media and item["audio_source"] == "SoundCloud":
        rows.insert(1, [button("Искать другую версию на Hitmo", "hitmo", item_id, revision)])
    if await imports.duplicate_candidates(item):
        rows.insert(1 if media else 0, [button("Проверить трек из базы", "existing", item_id, revision)])
    if media:
        await message.answer_audio(
            media, caption=caption, title=item["title"][:64], performer=item["artist"][:64],
            reply_markup=keyboard(*rows),
        )
    else:
        await message.answer(caption, reply_markup=keyboard(*rows))


async def show_next(message: Message, batch_id: int, state: str, after: int) -> None:
    item = await imports.next_review(batch_id, state, after)
    if item is None:
        await dashboard(message, batch_id)
    else:
        await review_card(message, item)


async def show_existing_track(message: Message, item_id: int, revision: int, track_id: int | None = None) -> None:
    item = await imports.get_item(item_id)
    if not item or item["attempt"] != revision or item["state"] not in {"found", "missing", "rejected"}:
        raise ValueError("Заявка уже изменилась. Откройте её заново.")
    candidates = await imports.duplicate_candidates(item, track_id)
    if not candidates:
        raise ValueError("Похожих одобренных записей в базе не найдено.")
    if len(candidates) > 1:
        rows = [[button(f"#{track['id']} · {track['title'][:35]} · {format_duration(track['duration'])}",
                        "existing", item_id, revision, track["id"])] for track in candidates[:8]]
        await message.answer("Выберите запись из базы, которую хотите сравнить.", reply_markup=keyboard(*rows))
        return

    old = candidates[0]
    text = (
        f"<b>Из базы · #{old['id']}</b>\n{short(old['artist'])} — {short(old['title'])}\n"
        f"Длительность: {format_duration(old['duration'])}\n\n"
        "Если это та же версия, привяжите существующий трек к релизам."
    )
    markup = keyboard([button("Это та же версия. Привязать", "link", item_id, revision, old["id"])])
    path = Path(old["file_path"])
    if path.stat().st_size <= imports.MAX_AUDIO_BYTES:
        await message.answer_audio(FSInputFile(path), caption=text, reply_markup=markup)
    else:
        await message.answer(text + "\nАудио этой записи можно открыть в WebApp.", reply_markup=markup)


async def hide_buttons(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def action_import(callback: CallbackQuery) -> None:
    message = callback.message
    if not isinstance(message, Message) or not is_admin_private(callback, message.chat):
        await callback.answer("Только для администратора в личном чате.", show_alert=True)
        return
    action, *args = callback.data.split(":")[1:]
    admin = callback.from_user.id
    await callback.answer()
    try:
        if action in {"confirm", "cancel"}:
            draft = DRAFT.get(admin)
            if not draft or draft[0] != args[0]:
                await message.answer("Предпросмотр устарел. Запустите команду заново.")
                return
            DRAFT.pop(admin)
            if action == "confirm":
                batch_id = await asyncio.to_thread(imports.enqueue_plan, draft[1])
                await hide_buttons(message)
                await dashboard(message, batch_id)
            else:
                await message.edit_text("Импорт отменён.")
        elif action == "list":
            await show_batches(message)
        elif action == "status":
            await dashboard(message, int(args[0]), edit=True)
        elif action in {"pause", "resume", "retry"}:
            await imports.control_batch(int(args[0]), action)
            await dashboard(message, int(args[0]), edit=True)
        elif action in {"found", "missing", "rejected"}:
            await show_next(message, int(args[0]), action, int(args[1]))
        elif action == "existing":
            await show_existing_track(message, int(args[0]), int(args[1]), int(args[2]) if len(args) == 3 else None)
        elif action == "hitmo":
            batch_id = await imports.search_hitmo_instead(int(args[0]), int(args[1]))
            await hide_buttons(message)
            await message.answer("Поставил поиск другой версии на Hitmo в очередь.")
            await dashboard(message, batch_id)
        elif action in {"approve", "reject", "link"}:
            user = await db.get_user(admin)
            if not user:
                await message.answer("Сначала зарегистрируйтесь командой /start.")
                return
            item = await imports.review(int(args[0]), int(args[1]), action != "reject", int(user["id"]),
                                        existing_track_id=int(args[2]) if action == "link" else None)
            await hide_buttons(message)
            if item["reused"]:
                await message.answer("Трек из базы привязан к релизам.")
            else:
                await message.answer("Трек опубликован." if action == "approve" else "Запись отклонена.")
            await show_next(message, item["batch_id"], item["state"], item["id"])
        elif action == "manual":
            item = await imports.get_item(int(args[0]))
            if not item or item["attempt"] != int(args[1]) or item["state"] not in {"found", "missing"}:
                raise ValueError("Заявка уже изменилась. Откройте её заново.")
            await message.answer(
                f"Ответьте на это сообщение MP3-файлом или ссылкой на SoundCloud.\n"
                f"<b>{short(item['artist'])} — {short(item['title'])}</b>"
                f"\nЗаявка FM #{item['id']}.{item['attempt']}",
                reply_markup=ForceReply(),
            )
    except ValueError as exc:
        await message.answer(short(str(exc), 350))
    except Exception:
        logger.exception("Ошибка в меню импорта артиста")
        await message.answer("Не удалось выполнить действие. Откройте партию повторно через /import_artist_yandex.")


def is_import_reply(message: Message) -> bool:
    reply = message.reply_to_message
    return bool(is_admin_private(message, message.chat) and reply and reply.from_user
                and reply.from_user.id == message.bot.id and REPLY_MARKER.search(reply.text or "")
                and (message.audio or message.document or message.text))


async def handle_import_reply(message: Message) -> None:
    match = REPLY_MARKER.search(message.reply_to_message.text)
    item_id, revision = map(int, match.groups())
    item = await imports.get_item(item_id)
    if not item or item["attempt"] != revision or item["state"] not in {"found", "missing"}:
        await message.answer("Заявка уже изменилась. Откройте её заново.")
        return
    file = message.audio or message.document
    if not file and not is_soundcloud_url((message.text or "").strip()):
        await message.answer("Пришлите MP3 или ссылку на SoundCloud ответом на исходное сообщение.")
        return
    path = imports.staged_path(item_id)
    keep = False
    try:
        await message.answer("Подготавливаю замену для прослушивания…")
        if file:
            if file.file_size and file.file_size > imports.MAX_AUDIO_BYTES:
                raise ValueError("MP3 слишком большой для отправки на прослушивание.")
            await message.bot.download(file, destination=path)
            source, url = "Telegram", None
        else:
            url = message.text.strip()
            candidate, _, audio_url = await download_from_soundcloud_url(
                url, item["artist"], item["title"], uploads_dir=path.parent,
            )
            if not candidate:
                raise ValueError("SoundCloud не вернул полный MP3. Пришлите другой файл или ссылку.")
            Path(candidate).replace(path)
            source, url = "SoundCloud", audio_url if audio_url and is_soundcloud_url(audio_url) else url
        audio = await asyncio.to_thread(imports.prepare_audio, path, item, source, url)
        await imports.replace_candidate(item, audio)
        keep = True
        updated = await imports.get_item(item_id)
        if updated and updated["state"] == "found":
            await review_card(message, updated)
    except asyncio.CancelledError:
        keep = True
        raise
    except ValueError as exc:
        await message.answer(short(str(exc), 350))
    except Exception:
        logger.exception("Не удалось прикрепить аудио к заявке %s", item_id)
        await message.answer("Не удалось добавить замену. Повторите отправку ответом на исходное сообщение.")
    finally:
        if not keep:
            imports.remove_staged(path)


def register(dispatcher: Dispatcher) -> None:
    dispatcher.message.register(command_import, Command("import_artist_yandex"))
    dispatcher.message.register(handle_import_reply, is_import_reply)
    dispatcher.callback_query.register(action_import, F.data.startswith(PREFIX + ":"))
