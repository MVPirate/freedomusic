"""Durable Yandex playlist checks and delivery to a user's library."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from time import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend import database as db
from backend.parser import YandexPlaylistUnavailable, parse_yandex_playlist, yandex_sync_url

logger = logging.getLogger(__name__)
POLL_SECONDS = 10
LEASE_SECONDS = 120
IMPORT_BATCH = 20
DAY = 86400
ISSUES = {
    "source": "Плейлист Яндекс Музыки закрыт или недоступен. Откройте доступ по ссылке или замените ссылку.",
    "temporary": "Яндекс Музыка временно недоступна. Повторим проверку позже.",
    "destination": "Плейлист назначения недоступен. Выберите другое место в настройках автосинхронизации.",
    "processing": "Не удалось завершить перенос. Повторим попытку позже.",
}


class SyncError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def next_daily(now: int) -> int:
    local = datetime.fromtimestamp(now, ZoneInfo("Europe/Moscow"))
    today = local.replace(hour=5, minute=0, second=0, microsecond=0)
    return int((today if today > local else today + timedelta(days=1)).timestamp())


async def fetch_source(url: str) -> tuple[list[dict], str]:
    return await asyncio.to_thread(parse_yandex_playlist, url, for_sync=True)


async def preview_source(raw_url: str) -> dict:
    try:
        url = yandex_sync_url(raw_url)
    except ValueError as exc:
        raise SyncError(400, str(exc)) from exc
    try:
        tracks, title = await fetch_source(url)
    except YandexPlaylistUnavailable as exc:
        raise SyncError(422, str(exc)) from exc
    except Exception as exc:
        logger.warning("Yandex playlist preview failed: %s", type(exc).__name__)
        raise SyncError(503, ISSUES["temporary"]) from exc
    return {"source_url": url, "source_title": title, "source_count": len(tracks)}


async def _one(conn, sql: str, params: tuple = ()) -> dict | None:
    async with conn.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _track_counts(conn, sync_id: int) -> dict:
    async with conn.execute("""
        SELECT state, COUNT(*) FROM playlist_sync_tracks
        WHERE sync_id = ? AND (source_position IS NOT NULL OR batch = 0) GROUP BY state
    """, (sync_id,)) as cursor:
        return dict(await cursor.fetchall())


async def get_user_source(user_id: int, sync_id: int | None = None) -> dict | None:
    async with db.get_db() as conn:
        source = await _one(conn, """
            SELECT s.id, s.source_url, s.source_title, s.source_count, s.destination_id,
                   s.paused, s.last_checked_at, s.last_added, s.issue, s.next_scan_at,
                   s.lease_until, p.title AS destination_title
            FROM playlist_sync_sources s
            LEFT JOIN user_playlists p ON p.id = s.destination_id AND p.user_id = s.user_id AND p.is_system = 0
            WHERE s.user_id = ? AND (? IS NULL OR s.id = ?) ORDER BY s.id LIMIT 1
        """, (user_id, sync_id, sync_id))
        if not source:
            return None
        counts = await _track_counts(conn, source["id"])
    if source["destination_id"] is None:
        source["destination_title"] = "Мне нравится"
    issue = source.pop("issue")
    if source["destination_title"] is None:
        issue = "destination"
    source["error"] = ISSUES.get(issue)
    source["working"] = source.pop("lease_until") > int(time())
    source["queued"] = counts.get("queued", 0)
    source["pending"] = counts.get("pending", 0)
    source["rechecking"] = counts.get("recheck", 0) + counts.get("verified", 0)
    source["missing_metadata"] = counts.get("metadata", 0)
    source["download_unavailable"] = counts.get("unavailable", 0)
    source["rejected"] = counts.get("rejected", 0)
    source["unavailable"] = source["missing_metadata"] + source["download_unavailable"] + source["rejected"]
    return source


async def _require_premium(conn, user_id: int) -> None:
    user = await _one(conn, "SELECT is_premium, premium_expires_at FROM users WHERE id = ?", (user_id,))
    if not user or not db.is_premium_active(user):
        raise SyncError(403, "Для автосинхронизации нужен активный Premium.")


async def _destination(conn, user_id: int, destination_id: int | None) -> str | None:
    if destination_id is None:
        return "Мне нравится"
    row = await _one(conn, "SELECT title FROM user_playlists WHERE id = ? AND user_id = ? AND is_system = 0",
                     (destination_id, user_id))
    return row["title"] if row else None


async def save_source(user_id: int, sync_id: int | None, preview: dict,
                      destination_id: int | None, new_title: str | None) -> int:
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await _require_premium(conn, user_id)
        current = await _one(conn, """
            SELECT * FROM playlist_sync_sources WHERE user_id = ? AND (? IS NULL OR id = ?) LIMIT 1
        """, (user_id, sync_id, sync_id))
        if sync_id is None and current:
            raise SyncError(409, "Автосинхронизация уже подключена. Откройте её настройки.")
        if sync_id is not None and not current:
            raise SyncError(404, "Автосинхронизация не найдена.")
        if new_title is not None:
            new_title = new_title.strip()
            if not 1 <= len(new_title) <= 120:
                raise SyncError(400, "Введите название плейлиста от 1 до 120 символов.")
            playlist = await db.create_user_playlist(user_id, new_title, conn=conn)
            destination_id = playlist["id"]
        elif await _destination(conn, user_id, destination_id) is None:
            raise SyncError(404, "Плейлист назначения не найден.")

        url, title, count = (preview[key] for key in ("source_url", "source_title", "source_count"))
        values = (url, title, count, destination_id, int(time()))
        if current:
            if current["source_url"] == url and current["destination_id"] == destination_id:
                return sync_id
            await conn.execute("DELETE FROM playlist_sync_tracks WHERE sync_id = ?", (sync_id,))
            await conn.execute("""
                UPDATE playlist_sync_sources SET source_url = ?, source_title = ?, source_count = ?,
                    destination_id = ?, next_scan_at = ?, resume_at = 0, premium_paused = 0,
                    last_checked_at = NULL, last_added = 0, issue = NULL, notice = NULL,
                    notify_added = 0, notify_progress = 0, notify_at = 0, lease_token = NULL, lease_until = 0
                WHERE id = ?
            """, (*values, sync_id))
        else:
            cursor = await conn.execute("""
                INSERT INTO playlist_sync_sources (user_id, source_url, source_title, source_count, destination_id, next_scan_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, *values))
            sync_id = cursor.lastrowid
        await conn.commit()
        return sync_id


async def manage_source(user_id: int, sync_id: int, action: str) -> None:
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if action == "resume":
            await _require_premium(conn, user_id)
        if action == "disconnect":
            cursor = await conn.execute("DELETE FROM playlist_sync_sources WHERE user_id = ? AND id = ?", (user_id, sync_id))
        else:
            cursor = await conn.execute("""
                UPDATE playlist_sync_sources SET paused = ?, lease_token = NULL, lease_until = 0
                WHERE user_id = ? AND id = ? AND paused != ?
            """, (action == "pause", user_id, sync_id, action == "pause"))
        if not cursor.rowcount and not await _one(conn, "SELECT id FROM playlist_sync_sources WHERE user_id = ? AND id = ?",
                                                 (user_id, sync_id)):
            raise SyncError(404, "Автосинхронизация не найдена.")
        await conn.commit()


async def claim_job(now: int) -> dict | None:
    resume_at = next_daily(now)
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(f"""
            UPDATE playlist_sync_sources SET premium_paused = 1, lease_token = NULL, lease_until = 0
            WHERE premium_paused = 0 AND user_id IN (SELECT id FROM users u WHERE NOT ({db.ACTIVE_PREMIUM_SQL}))
        """)
        await conn.execute(f"""
            UPDATE playlist_sync_sources SET premium_paused = 0, next_scan_at = ?, resume_at = ?
            WHERE premium_paused = 1 AND user_id IN (SELECT id FROM users u WHERE {db.ACTIVE_PREMIUM_SQL})
        """, (resume_at, resume_at))
        source = await _one(conn, """
            SELECT s.*, u.telegram_id FROM playlist_sync_sources s JOIN users u ON u.id = s.user_id
            WHERE paused = 0 AND premium_paused = 0 AND resume_at <= ? AND lease_until <= ? AND (
                next_scan_at <= ?
                OR (issue IS NOT 'destination' AND EXISTS (
                    SELECT 1 FROM playlist_sync_tracks t WHERE t.sync_id = s.id
                    AND (t.state = 'recheck' OR (t.source_position IS NOT NULL
                        AND t.state IN ('queued', 'pending', 'unavailable', 'rejected', 'verified')))
                    AND t.retry_at <= ?
                ))
                OR (notify_at <= ? AND (notice IS NOT NULL OR (
                    issue IS NOT 'destination' AND (notify_added > 0 OR notify_progress > 0)
                    AND NOT EXISTS (SELECT 1 FROM playlist_sync_tracks t WHERE t.sync_id = s.id
                        AND t.state IN ('queued', 'verified'))
                )))
            ) ORDER BY lease_until, next_scan_at, id LIMIT 1
        """, (now, now, now, now, now))
        if source:
            source["lease_token"] = uuid4().hex
            await conn.execute("UPDATE playlist_sync_sources SET lease_token = ?, lease_until = ? WHERE id = ?",
                               (source["lease_token"], now + LEASE_SECONDS, source["id"]))
        await conn.commit()
        return source


async def _active(conn, job: dict) -> bool:
    row = await _one(conn, """
        SELECT u.is_premium, u.premium_expires_at
        FROM playlist_sync_sources s JOIN users u ON u.id = s.user_id
        WHERE s.id = ? AND s.lease_token = ? AND s.lease_until > ? AND s.paused = 0
    """, (job["id"], job["lease_token"], int(time())))
    return row is not None and db.is_premium_active(row)


async def job_active(job: dict, *, renew: bool = False) -> bool:
    async with db.get_db() as conn:
        if renew:
            await conn.execute("BEGIN IMMEDIATE")
        if not await _active(conn, job) or await _destination(conn, job["user_id"], job["destination_id"]) is None:
            return False
        if renew:
            await conn.execute("UPDATE playlist_sync_sources SET lease_until = ? WHERE id = ? AND lease_token = ?",
                               (int(time()) + LEASE_SECONDS, job["id"], job["lease_token"]))
            await conn.commit()
        return True


async def _release(job: dict) -> None:
    async with db.get_db() as conn:
        await conn.execute("UPDATE playlist_sync_sources SET lease_token = NULL, lease_until = 0 WHERE id = ? AND lease_token = ?",
                           (job["id"], job["lease_token"]))
        await conn.commit()


async def _mark_issue(job: dict, issue: str, *, checked: bool = False, reviews: list[str] | None = None) -> None:
    now = int(time())
    next_scan = next_daily(now) if issue in ("source", "destination") else now + (1800 if checked else 300)
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if not await _active(conn, job):
            return
        await conn.execute("""
            UPDATE playlist_sync_sources SET issue = ?, next_scan_at = ?,
                last_checked_at = CASE WHEN ? THEN ? ELSE last_checked_at END,
                notice = CASE WHEN ? IN ('source', 'destination') AND issue IS NOT ? THEN ? ELSE notice END
            WHERE id = ?
        """, (issue, next_scan, checked, now, issue, issue, issue, job["id"]))
        if reviews:
            await conn.executemany("""
                UPDATE playlist_sync_tracks SET retry_at = ?
                WHERE sync_id = ? AND source_track_id = ? AND state = 'recheck'
            """, ((next_scan, job["id"], track_id) for track_id in reviews))
        if issue == "processing":
            await conn.execute("""
                UPDATE playlist_sync_tracks SET retry_at = ? WHERE sync_id = ? AND retry_at < ?
                AND state IN ('queued', 'pending', 'unavailable', 'rejected', 'recheck', 'verified')
            """, (next_scan, job["id"], next_scan))
        await conn.commit()


async def _record_scan(job: dict, tracks: list[dict], title: str, reviews: list[str]) -> None:
    now = int(time())
    snapshot = {}
    for track in tracks:
        snapshot.setdefault(track["source_id"], track)
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if not await _active(conn, job) or await _destination(conn, job["user_id"], job["destination_id"]) is None:
            return
        row = await _one(conn, "SELECT COALESCE(MAX(MAX(sequence, batch)), 0) AS last FROM playlist_sync_tracks WHERE sync_id = ?",
                         (job["id"],))
        batch = row["last"] + 1
        await conn.execute("UPDATE playlist_sync_tracks SET source_position = NULL WHERE sync_id = ? AND source_position IS NOT NULL",
                           (job["id"],))
        await conn.executemany("""
            INSERT INTO playlist_sync_tracks (sync_id, source_track_id, sequence, batch, source_position,
                                              artist, title, duration, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (sync_id, source_track_id) DO UPDATE SET
                source_position = excluded.source_position,
                batch = CASE WHEN state = 'removed' THEN excluded.batch ELSE batch END,
                artist = CASE WHEN state IN ('metadata', 'removed') THEN excluded.artist ELSE artist END,
                title = CASE WHEN state IN ('metadata', 'removed') THEN excluded.title ELSE title END,
                duration = CASE WHEN state IN ('metadata', 'removed') THEN excluded.duration ELSE duration END,
                retry_at = CASE WHEN state IN ('metadata', 'removed') THEN 0 ELSE retry_at END,
                track_id = CASE WHEN state = 'removed' THEN NULL ELSE track_id END,
                state = CASE WHEN state IN ('metadata', 'removed') THEN excluded.state ELSE state END
        """, (
            (job["id"], source_id, batch + position, batch, position, track["artist"], track["title"], track["duration"],
             "queued" if track["artist"] and track["title"] else "metadata")
            for position, (source_id, track) in enumerate(snapshot.items())
        ))
        await conn.execute("""
            UPDATE playlist_sync_tracks SET state = 'removed'
            WHERE sync_id = ? AND source_position IS NULL AND state NOT IN ('added', 'existing', 'removed', 'rejected')
        """, (job["id"],))
        # Only approvals observed before the network request belong to this snapshot.
        await conn.executemany("""
            UPDATE playlist_sync_tracks SET state = 'verified', retry_at = 0
            WHERE sync_id = ? AND source_track_id = ? AND state = 'recheck' AND source_position IS NOT NULL
        """, ((job["id"], source_id) for source_id in reviews))
        await _restore_legacy_order(conn, job)
        await conn.execute("""
            UPDATE playlist_sync_sources SET source_title = ?, source_count = ?, next_scan_at = ?,
                last_checked_at = ?, last_added = CASE WHEN next_scan_at <= ? THEN 0 ELSE last_added END,
                issue = NULL, notice = NULL,
                notify_progress = notify_progress OR EXISTS (
                    SELECT 1 FROM playlist_sync_tracks WHERE sync_id = ? AND sequence > ? AND state = 'metadata'
                )
            WHERE id = ?
        """, (title, len(tracks), next_daily(now), now, now, job["id"], row["last"], job["id"]))
        await conn.commit()


async def _scan(job: dict, reviews: list[str]) -> None:
    # Network requests run outside the SQLite transaction. Renew while the parser fetches metadata.
    fetch = asyncio.create_task(fetch_source(job["source_url"]))
    try:
        while not fetch.done():
            await asyncio.wait({fetch}, timeout=30)
            if not await job_active(job, renew=True):
                return
        tracks, title = fetch.result()
    except YandexPlaylistUnavailable:
        await _mark_issue(job, "source", checked=True, reviews=reviews)
    except Exception:
        logger.exception("Playlist sync %s could not read its source", job["id"])
        await _mark_issue(job, "temporary", checked=True, reviews=reviews)
    else:
        await _record_scan(job, tracks, title, reviews)
    finally:
        if not fetch.done():
            fetch.cancel()
        await asyncio.gather(fetch, return_exceptions=True)


async def _catalog_track(conn, item: dict) -> dict | None:
    async with conn.execute("""
        SELECT id, file_path, status FROM tracks WHERE artist_norm = ? AND title_norm = ?
        ORDER BY status = 'approved' DESC, id
    """, (db.norm_key(db.normalize_artist(item["artist"])), db.norm_key(item["title"]))) as cursor:
        tracks = [dict(row) for row in await cursor.fetchall()]
    return next((row for row in tracks if _has_audio(row)), tracks[0] if tracks else None)


def _has_audio(track: dict | None) -> bool:
    return bool(track and track["file_path"] and os.path.isfile(track["file_path"]))


async def _destination_order(conn, job: dict) -> list[tuple[int, int, int]]:
    """Return (membership ID, track ID, position) in library order."""
    if job["destination_id"] is None:
        return [(tid, tid, position) for tid, position in await db._ordered_likes(conn, job["user_id"])]
    async with conn.execute("""
        SELECT id, track_id, position FROM playlist_tracks WHERE playlist_id = ? ORDER BY position, added_at DESC, id
    """, (job["destination_id"],)) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


async def _restore_legacy_order(conn, job: dict) -> None:
    # The old worker appended imports. Repair just its trailing block once, using the fresh source order.
    async with conn.execute("""
        SELECT state, track_id, source_position FROM playlist_sync_tracks WHERE sync_id = ? AND batch = 0
    """, (job["id"],)) as cursor:
        legacy = await cursor.fetchall()
    if not legacy:
        return
    await conn.execute("UPDATE playlist_sync_tracks SET batch = 1 WHERE sync_id = ? AND batch = 0", (job["id"],))
    ranks = {track_id: position for state, track_id, position in legacy if state == "added" and position is not None}
    if not ranks:
        return
    entries = await _destination_order(conn, job)
    start = len(entries)
    while start and entries[start - 1][1] in ranks:
        start -= 1
    if start == len(entries):
        return
    ordered = sorted(entries[start:], key=lambda row: ranks[row[1]]) + entries[:start]
    if ordered == entries:
        return
    if job["destination_id"] is None:
        await db._write_like_order(conn, job["user_id"], [track_id for _, track_id, _ in ordered])
        await db._sync_liked_playlist(conn, job["user_id"])
    else:
        await conn.executemany("UPDATE playlist_tracks SET position = ? WHERE id = ?",
                               ((position, row_id) for position, (row_id, _, _) in enumerate(ordered)))


async def _insertion_index(conn, job: dict, item: dict, order: list[int]) -> int:
    if not item["source_position"] or not order:
        return 0
    late = item["state"] == "verified"
    indexes = {track_id: index for index, track_id in enumerate(order)}
    # Ordinary imports go above older batches. After approval, use current source neighbours.
    async with conn.execute("""
        SELECT track_id, source_position FROM playlist_sync_tracks
        WHERE sync_id = ? AND source_position IS NOT NULL
          AND ((state = 'added' AND batch = ?) OR (? AND state IN ('added', 'existing')))
        ORDER BY source_position
    """, (job["id"], item["batch"], late)) as cursor:
        previous = following = None
        for track_id, rank in await cursor.fetchall():
            if track_id not in indexes:
                continue
            if rank < item["source_position"]:
                previous = indexes[track_id] + 1
            elif rank > item["source_position"]:
                following = indexes[track_id]
                break
    if following is not None and (previous is None or previous > following):
        return following
    return previous if previous is not None else 0


async def _link_track(conn, job: dict, item: dict, track_id: int) -> bool:
    playlist_id = job["destination_id"]
    entries = await _destination_order(conn, job)
    order = [tid for _, tid, _ in entries]
    if track_id in order:
        return False
    index = await _insertion_index(conn, job, item, order)
    if playlist_id is None:
        return await db._insert_import_like(conn, job["user_id"], track_id, order[:index])
    if index < len(order):
        position = entries[index][2]
        if position is None or (index and entries[index - 1][2] == position):
            await conn.executemany("UPDATE playlist_tracks SET position = ? WHERE id = ?",
                                   ((rank + (rank >= index), row_id) for rank, (row_id, _, _) in enumerate(entries)))
            position = index
        else:
            await conn.execute("UPDATE playlist_tracks SET position = position + 1 WHERE playlist_id = ? AND position >= ?",
                               (playlist_id, position))
    else:
        position = max((value or 0 for _, _, value in entries), default=-1) + 1
    return await db.add_track_to_playlist(playlist_id, track_id, position, conn=conn)


async def _resolve_item(job: dict, item: dict, *, retry_delay: int | None = None) -> bool:
    """Return True when the item still needs a download."""
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if not await _active(conn, job) or await _destination(conn, job["user_id"], job["destination_id"]) is None:
            return False
        item = await _one(conn, """
            SELECT * FROM playlist_sync_tracks WHERE sync_id = ? AND source_track_id = ?
              AND source_position IS NOT NULL AND state IN ('queued', 'pending', 'unavailable', 'rejected', 'verified')
        """, (job["id"], item["source_track_id"]))
        if item is None:
            return False
        track = await _catalog_track(conn, item)
        has_audio = _has_audio(track)
        now = int(time())
        if has_audio and track["status"] == "approved" and (
            item["state"] in ("pending", "unavailable", "rejected") or (item["state"] == "queued" and retry_delay is not None)
        ):
            state, retry_at = "recheck", 0
        elif has_audio and track["status"] == "approved":
            added = await _link_track(conn, job, item, track["id"])
            state, retry_at = ("added" if added else "existing"), 0
            if added:
                await conn.execute("""
                    UPDATE playlist_sync_sources SET notify_added = notify_added + 1, last_added = last_added + 1 WHERE id = ?
                """, (job["id"],))
        elif has_audio and track["status"] == "pending":
            state, retry_at = "pending", now + 60
        elif item["state"] == "rejected" or (track and track["status"] == "rejected") or (
            item["track_id"] is not None and not track
        ):
            state, retry_at = "rejected", now + DAY
        elif retry_delay is not None:
            state, retry_at = "unavailable", now + retry_delay
        else:
            return True

        if item["state"] == "queued" and state in ("pending", "unavailable", "rejected", "recheck"):
            await conn.execute("UPDATE playlist_sync_sources SET notify_progress = 1 WHERE id = ?", (job["id"],))

        await conn.execute("""
            UPDATE playlist_sync_tracks SET state = ?, retry_at = ?, track_id = ?
            WHERE sync_id = ? AND source_track_id = ?
        """, (state, retry_at, track["id"] if track else item["track_id"], job["id"], item["source_track_id"]))
        await conn.commit()
        return False


async def _work_items(job: dict, download) -> None:
    async with db.get_db() as conn:
        async with conn.execute("""
            SELECT * FROM playlist_sync_tracks WHERE sync_id = ? AND retry_at <= ?
            AND source_position IS NOT NULL AND state IN ('queued', 'pending', 'unavailable', 'rejected', 'verified')
            ORDER BY batch DESC, source_position LIMIT ?
        """, (job["id"], int(time()), IMPORT_BATCH)) as cursor:
            items = [dict(row) for row in await cursor.fetchall()]
    for item in items:
        if not await job_active(job, renew=True):
            return
        if await _resolve_item(job, item):
            retry_delay = DAY
            try:
                await download(job, item)
            except Exception:
                logger.exception("Playlist sync %s could not download track %s", job["id"], item["source_track_id"])
                retry_delay = 1800
            await _resolve_item(job, item, retry_delay=retry_delay)


async def _notify(job: dict, send) -> None:
    async with db.get_db() as conn:
        if not await _active(conn, job):
            return
        source = await _one(conn, "SELECT * FROM playlist_sync_sources WHERE id = ?", (job["id"],))
        if source["notify_at"] > int(time()):
            return
        counts = await _track_counts(conn, job["id"])
        title = await _destination(conn, job["user_id"], job["destination_id"])

    message = ISSUES.get(source["notice"], "")
    report = title and not (counts.get("queued") or counts.get("verified")) and (source["notify_added"] or source["notify_progress"])
    if report:
        parts = []
        if source["notify_added"]:
            count = source["notify_added"]
            unit = "трек" if count % 10 == 1 and count % 100 != 11 else "треков"
            if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
                unit = "трека"
            parts.append(f"Добавлено {count} {unit} в «{title[:120]}».")
        if counts.get("pending"):
            prefix = "Ещё" if parts else f"Для «{title[:120]}»"
            parts.append(f"{prefix} {counts['pending']} на модерации.")
        for state, label in (
            ("recheck", "Ждут сверки с Яндекс Музыкой"),
            ("metadata", "Яндекс Музыка не передала данные"),
            ("unavailable", "Не удалось загрузить аудио"),
            ("rejected", "Отклонены или удалены из каталога"),
        ):
            if count := counts.get(state):
                parts.append(f"{label} — {count}.")
        message = "\n".join(filter(None, (message, " ".join(parts))))
    if not message and not report:
        return
    try:
        if message:
            await send(job["telegram_id"], message)
    except Exception:
        logger.warning("Could not send playlist sync notice for %s", job["id"], exc_info=True)
        async with db.get_db() as conn:
            await conn.execute("UPDATE playlist_sync_sources SET notify_at = ? WHERE id = ? AND lease_token = ?",
                               (int(time()) + 1800, job["id"], job["lease_token"]))
            await conn.commit()
        return
    async with db.get_db() as conn:
        await conn.execute("""
            UPDATE playlist_sync_sources SET notice = NULL, notify_at = 0,
                notify_added = CASE WHEN ? THEN 0 ELSE notify_added END,
                notify_progress = CASE WHEN ? THEN 0 ELSE notify_progress END
            WHERE id = ? AND lease_token = ?
        """, (bool(report), bool(report), job["id"], job["lease_token"]))
        await conn.commit()


async def run_job(job: dict, download, send) -> None:
    try:
        if not await job_active(job):
            await _mark_issue(job, "destination")
        else:
            now = int(time())
            async with db.get_db() as conn:
                async with conn.execute("""
                    SELECT source_track_id FROM playlist_sync_tracks
                    WHERE sync_id = ? AND state = 'recheck' AND retry_at <= ?
                """, (job["id"], now)) as cursor:
                    reviews = [row[0] for row in await cursor.fetchall()]
            if reviews or job["next_scan_at"] <= now:
                await _scan(job, reviews)
            await _work_items(job, download)
        await _notify(job, send)
    except Exception:
        logger.exception("Playlist sync %s could not finish", job["id"])
        await _mark_issue(job, "processing")
    finally:
        await _release(job)


async def _move_evening_schedule() -> None:
    async with db.get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        # The former daily slot was 14:00 UTC. Keep delayed attempts and immediate jobs intact.
        await conn.execute("""
            UPDATE playlist_sync_tracks SET retry_at = retry_at - 43200
            WHERE state = 'recheck' AND sync_id IN (
                SELECT id FROM playlist_sync_sources s WHERE s.issue = 'source'
                    AND s.next_scan_at % 86400 = 50400 AND s.next_scan_at = playlist_sync_tracks.retry_at
            )
        """)
        await conn.execute("""
            UPDATE playlist_sync_sources SET next_scan_at = next_scan_at - 43200
            WHERE next_scan_at % 86400 = 50400 AND (issue IS NULL OR issue IN ('source', 'destination'))
        """)
        await conn.execute("UPDATE playlist_sync_sources SET resume_at = resume_at - 43200 WHERE resume_at % 86400 = 50400")
        await conn.commit()


async def playlist_sync_worker(download, send) -> None:
    await _move_evening_schedule()
    async def work():
        while True:
            try:
                job = await claim_job(int(time()))
                if job:
                    await run_job(job, download, send)
                    continue
            except Exception:
                logger.exception("Playlist sync worker failed")
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.gather(work(), work())
