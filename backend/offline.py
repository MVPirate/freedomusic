import asyncio
import json
import math
import time
from bisect import bisect_left
from collections import defaultdict

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile

from backend.database import (
    TOPIC_LEASE_SECONDS, TopicClaimLost, add_topic_track, claim_topic_purges, claim_topics, drop_topic_track,
    finish_topic, finish_topic_purge, get_db, get_topic_sync_snapshot, renew_topic_claim, retry_topic,
    retry_topic_purge, save_topic_moves, set_topic_thread_id,
)
from backend.downloader import extract_telegram_thumbnail

TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]


class ChatPacer:
    def __init__(self, interval: float = 1.1):
        self.interval, self._last = interval, {}
        self._locks = defaultdict(asyncio.Lock)

    async def call(self, chat_id: int, make_coro):
        async with self._locks[chat_id]:
            for _ in range(6):
                pause = self._last.get(chat_id, 0) + self.interval - time.monotonic()
                if pause > 0:
                    await asyncio.sleep(pause)
                try:
                    return await make_coro()
                except TelegramRetryAfter as e:
                    retry_after = e.retry_after
                finally:
                    self._last[chat_id] = time.monotonic()
                await asyncio.sleep(retry_after + 1)
            raise RuntimeError("Слишком много RetryAfter")


pacer = ChatPacer()


async def _sync_with_heartbeat(bot, topic_id: int, sync_token: str) -> None:
    async def heartbeat():
        while True:
            await asyncio.sleep(TOPIC_LEASE_SECONDS / 4)
            await renew_topic_claim(topic_id, sync_token)

    sync = asyncio.create_task(sync_topic(bot, topic_id, sync_token))
    lease = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait((sync, lease), return_when=asyncio.FIRST_COMPLETED)
        if sync in done:
            sync.result()
        else:
            try:
                lease.result()
            except TopicClaimLost:
                # Дожидаемся ответа на уже начатый запрос. Новые запросы проверяют право на синхронизацию.
                # Если Telegram успел создать тему, sync_topic поставит её в очередь удаления.
                await sync
                raise
    finally:
        for task in (sync, lease):
            task.cancel()
        await asyncio.gather(sync, lease, return_exceptions=True)


async def delete_forum_topic(bot, chat_id: int, thread_id: int) -> None:
    try:
        deleted = await bot.delete_forum_topic(
            chat_id=chat_id, message_thread_id=thread_id, request_timeout=15,
        )
    except TelegramBadRequest as exc:
        if any(message in exc.message.lower() for message in (
            "topic not found", "message thread not found", "topic_id_invalid", "message_thread_not_found",
        )):
            return
        raise
    if not deleted:
        raise RuntimeError(f"Telegram не подтвердил удаление темы {thread_id}")


async def purge_topic(bot, claim: dict) -> None:
    topic_id, sync_token = claim["id"], claim["sync_token"]
    thread_ids = {claim["thread_id"], claim["stale_thread_id"]} - {None}
    try:
        for thread_id in thread_ids:
            await delete_forum_topic(bot, claim["tg_chat_id"], thread_id)
    except Exception as exc:
        delay = math.ceil(exc.retry_after) + 1 if isinstance(exc, TelegramRetryAfter) else 30
        print(f"[Offline Worker] Ошибка удаления темы {topic_id} в чате {claim['tg_chat_id']} "
              f"(thread_id={sorted(thread_ids)}): {exc}", flush=True)
        await retry_topic_purge(topic_id, sync_token, delay, str(exc))
        return
    await finish_topic_purge(topic_id, sync_token)


async def delete_pending_topics(bot, limit: int):
    async with get_db() as conn:
        async with conn.execute("""
            SELECT chat_id, thread_id FROM telegram_topic_deletions
            WHERE run_after <= CURRENT_TIMESTAMP ORDER BY run_after LIMIT ?
        """, (limit,)) as cursor:
            pending = await cursor.fetchall()
    for chat_id, thread_id in pending:
        try:
            await delete_forum_topic(bot, chat_id, thread_id)
        except Exception as exc:
            print(f"[Offline Worker] Ошибка удаления темы {thread_id}: {exc}", flush=True)
            delay = math.ceil(exc.retry_after) + 1 if isinstance(exc, TelegramRetryAfter) else 30
            async with get_db() as conn:
                await conn.execute("""
                    UPDATE telegram_topic_deletions SET run_after = datetime('now', ?)
                    WHERE chat_id = ? AND thread_id = ?
                """, (f"+{delay} seconds", chat_id, thread_id))
                await conn.commit()
            continue
        async with get_db() as conn:
            await conn.execute("DELETE FROM telegram_topic_deletions WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id))
            await conn.commit()


async def offline_purge_worker(bot, max_parallel: int = 3):
    while True:
        try:
            await delete_pending_topics(bot, max_parallel)
            claims = await claim_topic_purges(max_parallel)
            if claims:
                await asyncio.gather(*(purge_topic(bot, claim) for claim in claims))
                continue
            await asyncio.sleep(1)
        except Exception as exc:
            print(f"[Offline Worker] Ошибка очереди удаления тем: {exc}", flush=True)
            await asyncio.sleep(1)


async def offline_worker(bot, max_parallel: int = 3):
    print("[Offline Worker] Запущен", flush=True)

    async def run(claim):
        topic_id, sync_token = claim["id"], claim["sync_token"]
        if claim["state"] == "syncing":
            print(f"[Offline Worker] Тема {topic_id}: возобновление синхронизации", flush=True)
        try:
            await _sync_with_heartbeat(bot, topic_id, sync_token)
        except TopicClaimLost:
            print(f"[Offline Worker] Тема {topic_id}: задача уже передана другому воркеру", flush=True)
        except Exception as e:
            print(f"[Offline Worker] Ошибка темы {topic_id}: {e}", flush=True)
            await retry_topic(topic_id, sync_token, str(e))

    while True:
        try:
            batch = await claim_topics(limit=max_parallel)
            if not batch:
                await asyncio.sleep(5)
                continue
            await asyncio.gather(*(run(t) for t in batch))
        except Exception as e:
            print(f"[Offline Worker] Критическая ошибка: {e}", flush=True)
            await asyncio.sleep(10)

async def create_topic(bot, chat_id: int, title: str, seed: int, check_claim) -> int:
    async def create():
        await check_claim()
        return await bot.create_forum_topic(
            chat_id=chat_id, name=title[:128], icon_color=TOPIC_COLORS[seed % len(TOPIC_COLORS)],
            request_timeout=15,
        )

    t = await pacer.call(chat_id, create)
    return t.message_thread_id

async def send_track(bot, chat_id: int, thread_id: int, track: dict, check_claim) -> tuple[int, str | None]:
    audio = track["file_id"]
    thumbnail = None
    if not audio:
        if not track["file_path"]:
            raise ValueError(f"У трека {track['id']} нет файла для отправки")
        cover = await asyncio.to_thread(extract_telegram_thumbnail, track["file_path"])
        if cover:
            thumbnail = BufferedInputFile(cover, filename="cover.jpg")
        audio = FSInputFile(track["file_path"])

    async def send():
        await check_claim()
        return await bot.send_audio(
            chat_id=chat_id, message_thread_id=thread_id, audio=audio, thumbnail=thumbnail,
            title=track["title"], performer=track["artist"], disable_notification=True,
            duration=int(track["duration"] or 0),
        )

    message = await pacer.call(chat_id, send)
    file_id = message.audio.file_id if message.audio and not track["file_id"] else None
    return message.message_id, file_id


def _tracks_in_previous_order(wanted: list[dict], mapped: dict[int, dict]) -> set[int]:
    existing = [track["id"] for track in wanted if track["id"] in mapped and mapped[track["id"]]["position"] is not None]
    previous_order = sorted(existing, key=lambda track_id: (mapped[track_id]["position"], -mapped[track_id]["message_id"]))
    previous_indices = {track_id: index for index, track_id in enumerate(previous_order)}

    # Наибольшая подпоследовательность в прежнем порядке отделяет перемещение
    # одного трека вниз от сдвига всех треков, через которые он прошёл.
    tails = []
    predecessor = {}
    for track_id in existing:
        index = previous_indices[track_id]
        length = bisect_left(tails, index)
        predecessor[index] = tails[length - 1] if length else None
        if length == len(tails):
            tails.append(index)
        else:
            tails[length] = index

    kept = set()
    index = tails[-1] if tails else None
    while index is not None:
        kept.add(previous_order[index])
        index = predecessor[index]
    return kept


def _tracks_to_move(wanted: list[dict], mapped: dict[int, dict]) -> set[int]:
    kept = _tracks_in_previous_order(wanted, mapped)
    moves = set()
    newer_message_id = 0
    first_track_id = wanted[0]["id"] if wanted else None
    for track in reversed(wanted):
        track_id = track["id"]
        message = mapped.get(track_id)
        if message is None:
            continue
        previous = message["position"]
        if previous is None:
            # Для старых незавершённых тем известен только порядок уже отправленных сообщений.
            moved_up = track_id == first_track_id and track["position"] == 0
        else:
            moved_up = track_id not in kept and track["position"] < previous
        # Сдвиг индексов при вставке или удалении не требует переотправки.
        if moved_up and message["message_id"] < newer_message_id:
            moves.add(track_id)
        newer_message_id = max(newer_message_id, message["message_id"])
    return moves


async def sync_topic(bot, topic_id: int, sync_token: str) -> None:
    topic, wanted, messages = await get_topic_sync_snapshot(topic_id, sync_token)
    chat_id, thread_id = topic["tg_chat_id"], topic["thread_id"]

    async def check_claim():
        await renew_topic_claim(topic_id, sync_token)

    if topic["playlist_id"] is not None and thread_id is None:
        thread_id = await create_topic(bot, chat_id, topic["title"], topic_id, check_claim)
        try:
            await set_topic_thread_id(topic_id, sync_token, thread_id)
        except TopicClaimLost:
            async with get_db() as conn:
                await conn.execute("INSERT OR IGNORE INTO telegram_topic_deletions (chat_id, thread_id) VALUES (?, ?)", (chat_id, thread_id))
                await conn.commit()
            raise

    next_positions = {track["id"]: track["position"] for track in wanted}
    mapped = {message["track_id"]: message for message in messages}
    move_plan = json.loads(topic["move_plan"]) if topic["move_plan"] else {}
    if move_plan.get("rev") == topic["rev"]:
        moves = next_positions.keys() & move_plan["tracks"]
    else:
        moves = _tracks_to_move(wanted, mapped)
        await save_topic_moves(topic_id, sync_token, topic["rev"], moves)

    deleted = sent = moved = 0
    blocked = []

    async def delete_synced_message(message_id: int) -> bool:
        await check_claim()
        return await bot.delete_message(chat_id, message_id)

    for track_id in (mapped.keys() - next_positions.keys()) | (mapped.keys() & moves):
        message_id = mapped[track_id]["message_id"]
        if track_id in moves and mapped[track_id]["position"] == next_positions[track_id]:
            continue
        try:
            if not await pacer.call(chat_id, lambda: delete_synced_message(message_id)):
                raise RuntimeError(f"Telegram не подтвердил удаление сообщения {message_id}")
            deleted += 1
        except TelegramBadRequest as e:
            detail = e.message.lower()
            if "message can't be deleted" in detail:
                blocked.append(message_id)
                print(f"[Offline Worker] Пропущено удаление сообщения (лимит 48ч) для трека {track_id}", flush=True)
                continue
            if "message to delete not found" not in detail:
                raise
            # Удаление успело выполниться в Telegram до сохранения результата в SQLite.
        await drop_topic_track(topic_id, sync_token, track_id, message_id)
        del mapped[track_id]

    positions = []
    # Отправляем от конца списка, чтобы первые треки были среди самых свежих сообщений.
    for track in reversed(wanted):
        track_id, position = track["id"], track["position"]
        if track_id in mapped:
            if mapped[track_id]["position"] != position:
                positions.append((track_id, position))
            continue
        message_id, file_id = await send_track(bot, chat_id, thread_id, track, check_claim)
        await add_topic_track(topic_id, sync_token, track_id, message_id, position, file_id)
        sent += 1
        moved += track_id in moves

    counts = f"удалено {deleted}, отправлено {sent}, перемещено {moved}"
    warning = f"Telegram не удалил старые сообщения (лимит 48ч): {blocked}" if blocked else None
    await finish_topic(topic_id, sync_token, topic["rev"], positions, warning=warning)
    print(f"[Offline Worker] Тема {topic_id}, rev {topic['rev']}: {counts}", flush=True)
