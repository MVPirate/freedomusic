"""Album contributions use the artist import queue and listening review."""
from __future__ import annotations

import json
import logging
import secrets
import time

from backend import database as db
from backend.artist_catalog_store import metadata_key

logger = logging.getLogger(__name__)
REWARD = 10
REQUEST_LIFETIME = 24 * 60 * 60

MISSING_TRACKS_SQL = """
    SELECT e.yandex_track_id, e.artist, e.title, e.duration_ms
    FROM artist_catalog_tracks e
    LEFT JOIN yandex_recordings y ON y.source_id = e.yandex_track_id
    LEFT JOIN tracks t ON t.id = e.track_id AND t.status = 'approved'
    LEFT JOIN tracks linked ON linked.id = y.track_id AND linked.status = 'approved'
    WHERE e.artist_key = ? AND e.album_id = ? AND t.id IS NULL AND linked.id IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM artist_catalog_tracks another JOIN tracks local ON local.id = another.track_id
        WHERE another.yandex_track_id = e.yandex_track_id AND local.status = 'approved'
      )
    ORDER BY e.disc, e.position
"""


async def missing_tracks(conn, artist_key, album_id):
    async with conn.execute(MISSING_TRACKS_SQL, (artist_key, album_id)) as cursor:
        return {row["yandex_track_id"]: row for row in await cursor.fetchall()}


async def create_request(user_id: int, artist: str, album_id: str) -> str:
    key = metadata_key(artist)
    now = int(time.time())
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT title FROM artist_catalog_albums WHERE artist_key = ? AND album_id = ?", (key, album_id),
        ) as cursor:
            album = await cursor.fetchone()
        if not album:
            raise LookupError("Релиз не найден.")
        missing = await missing_tracks(conn, key, album_id)
        if not missing:
            raise ValueError("В релизе уже доступны все треки.")
        async with conn.execute("""
            SELECT code FROM album_import_requests
            WHERE user_id = ? AND artist_key = ? AND album_id = ? AND state = 'created' AND expires_at > ?
            ORDER BY id DESC LIMIT 1
        """, (user_id, key, album_id, now)) as cursor:
            existing = await cursor.fetchone()
        if existing:
            return existing["code"]
        code = secrets.token_urlsafe(18)
        await conn.execute("""
            INSERT INTO album_import_requests(user_id, artist_key, album_id, album_title, code, expires_at, eligible)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, key, album_id, album["title"], code, now + REQUEST_LIFETIME, json.dumps(list(missing))))
        await conn.commit()
        return code


async def open_request(user_id: int, code: str) -> dict | None:
    async with db.get_db() as conn, conn.execute("""
        UPDATE album_import_requests SET state = 'opened'
        WHERE user_id = ? AND code = ? AND state = 'created' AND expires_at > ?
        RETURNING id, album_title
    """, (user_id, code, int(time.time()))) as cursor:
        request = await cursor.fetchone()
        await cursor.fetchall()
        await conn.commit()
        return dict(request) if request else None


async def _opened_request(conn, user_id: int, request_id: int):
    async with conn.execute("""
        SELECT * FROM album_import_requests
        WHERE user_id = ? AND id = ? AND state = 'opened' AND expires_at > ?
    """, (user_id, request_id, int(time.time()))) as cursor:
        request = await cursor.fetchone()
    if request is None:
        raise ValueError("Заявка уже запущена или срок её действия истёк.")
    return request


async def prepare_request(user_id: int, request_id: int, source_ids: list[str]) -> tuple[str, int]:
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        request = await _opened_request(conn, user_id, request_id)
        missing = await missing_tracks(conn, request["artist_key"], request["album_id"])
        eligible = set(json.loads(request["eligible"]))
        selected = [source_id for source_id in dict.fromkeys(source_ids) if source_id in eligible and source_id in missing]
        revision = secrets.token_hex(6)
        await conn.execute("UPDATE album_import_requests SET draft = ? WHERE id = ?",
                           (json.dumps([revision, selected]), request_id))
        await conn.commit()
        return revision, len(selected)


async def start_request(user_id: int, request_id: int, revision: str) -> int:
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        request = await _opened_request(conn, user_id, request_id)
        token, selected = json.loads(request["draft"] or "[null, []]")
        if token != revision:
            raise ValueError("Предпросмотр устарел. Отправьте ссылку снова.")
        key, album_id = request["artist_key"], request["album_id"]
        missing = await missing_tracks(conn, key, album_id)
        rows = [missing[source_id] for source_id in selected if source_id in missing]
        if not rows:
            raise ValueError("Эти треки уже появились в каталоге. Проверьте состав альбома.")
        await conn.execute("INSERT OR IGNORE INTO artist_import_batches(artist_key) VALUES (?)", (key,))
        async with conn.execute("SELECT id FROM artist_import_batches WHERE artist_key = ?", (key,)) as cursor:
            batch_id = (await cursor.fetchone())[0]
        await conn.executemany("""
            INSERT INTO artist_import_items
                (batch_id, source_id, artist, title, duration_ms, release_title, album_request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ((batch_id, row["yandex_track_id"], row["artist"], row["title"], row["duration_ms"],
               request["album_title"], request_id) for row in rows))
        await conn.execute("UPDATE album_import_requests SET state = 'queued', draft = NULL WHERE id = ?", (request_id,))
        await conn.commit()
        return len(rows)


def award_publication(conn, source_id: str, request_id: int, user_id: int) -> int:
    inserted = conn.execute("""
        INSERT INTO album_import_rewards(source_id, request_id, user_id, amount) VALUES (?, ?, ?, ?)
        ON CONFLICT(source_id) DO NOTHING
    """, (source_id, request_id, user_id, REWARD)).rowcount
    if not inserted:
        return 0
    conn.execute(db.POINTS_WITH_XP_SQL, (REWARD, REWARD, user_id))
    conn.execute(db.POINTS_TRANSACTION_SQL, (user_id, REWARD, f"album_import_{source_id}"))
    xp = conn.execute("SELECT xp FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    conn.execute("UPDATE users SET level = ? WHERE id = ?", (db.get_level_by_points(xp), user_id))
    return REWARD


async def send_results(send_notice) -> None:
    async with db.get_db() as conn, conn.execute("""
        SELECT r.id, r.album_title, u.telegram_id, SUM(i.state = 'approved') AS approved,
            SUM(i.reward) AS points, COUNT(*) AS total
        FROM album_import_requests r JOIN users u ON u.id = r.user_id
        JOIN artist_import_items i ON i.album_request_id = r.id
        WHERE r.state = 'queued' AND r.notified = 0
        GROUP BY r.id
        HAVING SUM(i.state IN ('queued', 'searching', 'found', 'missing')) = 0
        ORDER BY r.id LIMIT 10
    """) as cursor:
        results = await cursor.fetchall()
    for result in results:
        try:
            await send_notice(dict(result))
        except Exception:
            logger.exception("Не удалось сообщить итог заявки №%s", result["id"])
            continue
        async with db.get_db() as conn:
            await conn.execute("UPDATE album_import_requests SET notified = 1, state = 'done' WHERE id = ?",
                               (result["id"],))
            await conn.commit()
