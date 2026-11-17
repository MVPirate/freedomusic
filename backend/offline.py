import asyncio
import time
from collections import defaultdict
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile

from backend.database import (
    claim_topics, get_topic, finish_topic, set_topic_thread_id,
    add_topic_track, get_topic_tracks, drop_topic_tracks,
    get_playlist_tracks, set_track_file_id
)

TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]

class TopicGone(Exception): pass
class NeedRebuild(Exception): pass

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
                    res = await make_coro()
                    self._last[chat_id] = time.monotonic()
                    return res
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
            raise RuntimeError("Слишком много RetryAfter")

pacer = ChatPacer()

async def offline_worker(bot, max_parallel: int = 3):
    print("[Offline Worker] Запущен")
    sem = asyncio.Semaphore(max_parallel)
    
    async def run(t):
        async with sem:
            try:
                await sync_topic(bot, t["id"])
                await finish_topic(t["id"], t["rev"])
            except Exception as e:
                print(f"[Offline Worker] Ошибка темы {t['id']}: {e}")
                
    while True:
        try:
            batch = await claim_topics(limit=5)
            if not batch:
                await asyncio.sleep(5)
                continue
            await asyncio.gather(*(run(t) for t in batch))
        except Exception as e:
            print(f"[Offline Worker] Критическая ошибка: {e}")
            await asyncio.sleep(10)

async def create_topic(bot, chat_id: int, title: str, seed: int = 0) -> int:
    t = await pacer.call(chat_id, lambda: bot.create_forum_topic(
        chat_id=chat_id, name=title[:128], icon_color=TOPIC_COLORS[seed % 6]
    ))
    return t.message_thread_id

async def send_track(bot, chat_id: int, thread_id: int | None, tr: dict) -> int:
    async def _send(audio):
        return await pacer.call(chat_id, lambda: bot.send_audio(
            chat_id=chat_id, message_thread_id=thread_id, audio=audio,
            title=tr["title"], performer=tr["artist"], disable_notification=True
        ))
        
    uploaded = not tr.get("file_id")
    try:
        msg = await _send(tr["file_id"] or FSInputFile(tr["file_path"]))
    except TelegramBadRequest as e:
        if "thread not found" in str(e).lower():
            raise TopicGone
        raise
        
    if uploaded:
        await set_track_file_id(tr["id"], msg.audio.file_id)
    return msg.message_id

async def sync_topic(bot, topic_id: int):
    t = await get_topic(topic_id)
    if not t["playlist_id"]:
        return

    # Получаем треки (переворачиваем, чтобы первый трек был в конце чата и играл первым)
    tracks_db = await get_playlist_tracks(t["playlist_id"])
    wanted = list(reversed(tracks_db))
    want_ids = [x["id"] for x in wanted]
    
    if not t["thread_id"]:
        thread_id = await create_topic(bot, t["tg_chat_id"], t["title"], t["id"])
        await set_topic_thread_id(t["id"], thread_id)
        t["thread_id"] = thread_id

    have = await get_topic_tracks(topic_id)
    have_ids = [h["track_id"] for h in have]
    
    # Для упрощения на первом этапе: если списки не совпадают, пересобираем
    if want_ids != have_ids:
        # Удаляем старые сообщения
        for h in have:
            try:
                await pacer.call(t["tg_chat_id"], lambda: bot.delete_message(t["tg_chat_id"], h["message_id"]))
            except: pass
        await drop_topic_tracks(topic_id, have_ids)
        
        # Отправляем заново
        for tr in wanted:
            mid = await send_track(bot, t["tg_chat_id"], t["thread_id"], tr)
            await add_topic_track(topic_id, tr["id"], mid)