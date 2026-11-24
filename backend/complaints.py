"""Authenticated complaints and their moderation workflow."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from secrets import token_hex

from backend import artist_import, database as db
from backend.artist_catalog import connect
from backend.moderation_store import REASONS, validate_reference

logger = logging.getLogger(__name__)


async def submit(track_id, user, username, reason, url):
    url = validate_reference(url)
    if reason not in REASONS:
        raise ValueError("Выберите причину жалобы.")
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute("SELECT 1 FROM tracks WHERE id = ? AND status = 'approved'", (track_id,)) as cursor:
            if not await cursor.fetchone():
                raise LookupError("Трек не найден.")
        inserted = await conn.execute("""
            INSERT OR IGNORE INTO track_complaints(track_id, user_id, username, telegram_id, reason, reference_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (track_id, user["id"], username, user["telegram_id"], reason, url))
        if inserted.rowcount:
            await conn.execute("UPDATE tracks SET complaint_count = COALESCE(complaint_count, 0) + 1 WHERE id = ?", (track_id,))
        await conn.commit()
        return bool(inserted.rowcount)


async def queue(after: int, limit: int = 11):
    async with db.get_db() as conn, conn.execute("""
        SELECT t.id, t.title, t.artist, COUNT(*) AS total
        FROM track_complaints c JOIN tracks t ON t.id = c.track_id
        WHERE c.resolved_at IS NULL AND c.track_id > ? AND t.status = 'approved'
        GROUP BY t.id ORDER BY t.id LIMIT ?
    """, (after, limit)) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def details(track_id: int, after: int = 0):
    async with db.get_db() as conn, conn.execute("""
        SELECT * FROM track_complaints WHERE track_id = ? AND resolved_at IS NULL AND id > ?
        ORDER BY id LIMIT 5
    """, (track_id, after)) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def stage(track_id: int, revision: int, audio: artist_import.AudioCandidate) -> str:
    token = token_hex(8)
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute("""
            SELECT 1 FROM tracks WHERE id = ? AND status = 'approved' AND media_revision = ?
            AND EXISTS (SELECT 1 FROM track_complaints WHERE track_id = ? AND resolved_at IS NULL)
        """, (track_id, revision, track_id)) as cursor:
            if not await cursor.fetchone():
                raise ValueError("Трек или жалоба уже изменились. Откройте карточку заново.")
        async with conn.execute("SELECT file_path FROM complaint_audio WHERE track_id = ?", (track_id,)) as cursor:
            previous = await cursor.fetchone()
        await conn.execute("""
            INSERT INTO complaint_audio VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET token = excluded.token, media_revision = excluded.media_revision,
                file_path = excluded.file_path, bitrate = excluded.bitrate, duration = excluded.duration
        """, (track_id, token, revision, audio.path, audio.bitrate, audio.duration))
        await conn.commit()
    if previous:
        artist_import.remove_staged(previous["file_path"])
    return token


def _resolve(track_id: int, shown: tuple[int, int], token: str | None, admin_id: int) -> None:
    old_file = unused_candidate = None
    with closing(connect(Path(db.DB_PATH), True)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute("SELECT * FROM complaint_audio WHERE track_id = ?", (track_id,)).fetchone()
        if token:
            if not candidate or candidate["token"] != token or not conn.execute("""
                SELECT 1 FROM track_complaints WHERE track_id = ? AND resolved_at IS NULL
            """, (track_id,)).fetchone():
                raise ValueError("Эта замена уже обработана или устарела.")
            item = dict(candidate, source_url=None, audio_source="telegram")
            try:
                with Path(item["file_path"]).open("rb") as audio:
                    digest = hashlib.file_digest(audio, "sha256").hexdigest()
            except OSError as exc:
                raise ValueError("Новый файл недоступен. Пришлите его ещё раз.") from exc
            old_file = artist_import._replace_library_audio(
                conn, item, track_id, digest, item["media_revision"], admin_id,
            )
            if conn.execute("SELECT 1 FROM tracks WHERE file_path = ?", (old_file,)).fetchone():
                old_file = None
        conn.execute("""
            UPDATE track_complaints SET resolved_at = CURRENT_TIMESTAMP, resolution = ?
            WHERE track_id = ? AND resolved_at IS NULL AND (? OR id BETWEEN ? AND ?)
        """, ("replaced" if token else "dismissed", track_id, bool(token), *shown))
        if token or not conn.execute("""
            SELECT 1 FROM track_complaints WHERE track_id = ? AND resolved_at IS NULL
        """, (track_id,)).fetchone():
            conn.execute("DELETE FROM complaint_audio WHERE track_id = ?", (track_id,))
            if candidate and not token:
                unused_candidate = candidate["file_path"]
    if unused_candidate:
        artist_import.remove_staged(unused_candidate)
    if old_file:
        try:
            Path(old_file).unlink(missing_ok=True)
        except OSError:
            logger.warning("Не удалось удалить старое аудио трека %s", track_id, exc_info=True)


async def resolve(track_id: int, admin_id: int, *, shown: tuple[int, int] = (0, 0), token: str | None = None) -> None:
    try:
        await asyncio.to_thread(_resolve, track_id, shown, token, admin_id)
    except sqlite3.IntegrityError as exc:
        raise ValueError("Не удалось сохранить замену. Старое аудио осталось доступно.") from exc
