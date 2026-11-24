"""Persistent candidates for the administrator's Yandex catalog import."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from mutagen.mp3 import MP3, error as MP3Error

from backend import artist_catalog as catalog, database as db
from backend.artist_catalog_store import (
    APPROVED_RECORDING_SQL, BIND_RECORDING_SQL, artist_credit_keys, metadata_key, split_artist_credits,
)
from backend.downloader import download_track_smart, embed_cover_bytes, fix_audio_tags, is_soundcloud_url
from backend.hitmo_parser import download_from_hitmo, duration_ok
from backend.parser import get_yandex_client

logger = logging.getLogger(__name__)
STAGING_DIR = Path(__file__).resolve().parent.parent / "uploads" / "artist-import"
LEASE_SECONDS = 15 * 60
MAX_AUDIO_BYTES = 49 * 1024 * 1024


@dataclass
class ImportPlan:
    artist_id: str
    source_name: str
    local_name: str
    releases: list[catalog.Release]
    tracks: dict[str, tuple[catalog.CatalogTrack, str | None]]
    covers: dict
    revision: int | None


def read_plan(artist_id: str, requested_name: str = "") -> ImportPlan:
    source = catalog.CatalogSource(get_yandex_client(), delay=0.35)
    name, releases = source.discography(artist_id)
    tracks: dict[str, tuple[catalog.CatalogTrack, str | None]] = {}
    for release in releases:
        for track in release.tracks:
            tracks.setdefault(track.id, (track, release.title))
    for track in source.artist_tracks(artist_id):
        tracks.setdefault(track.id, (track, None))
    if not tracks:
        raise ValueError("Яндекс Музыка вернула пустой каталог.")

    with closing(catalog.connect(Path(db.DB_PATH), False)) as conn:
        bindings = catalog.load_bindings(conn)
        known = next((row for row in bindings.values() if row["yandex_artist_id"] == artist_id), None)
        local_name = requested_name or (known["artist_name"] if known else name)
        key = metadata_key(local_name)
        if known and key != known["artist_key"]:
            raise ValueError(f"Артист уже связан со страницей «{known['artist_name']}».")
        if key in bindings and bindings[key]["yandex_artist_id"] != artist_id:
            raise ValueError("У этой страницы уже есть другой ID Яндекс Музыки. Укажите другую страницу через |.")
        if known and not releases and conn.execute(
            "SELECT 1 FROM artist_catalog_albums WHERE artist_key = ? LIMIT 1", (key,),
        ).fetchone():
            raise ValueError("Ранее непустая дискография вернулась пустой.")
        images = catalog.cached_covers(conn, key, known)
    return ImportPlan(artist_id, name, local_name, releases, tracks,
                      catalog.collect_covers(source, releases, images), known["updated_at"] if known else None)


def _reuse_source_recording(conn: sqlite3.Connection, source_id: str, track_id: int) -> list[str]:
    conn.execute("INSERT OR IGNORE INTO yandex_recordings(source_id) VALUES (?)", (source_id,))
    if not conn.execute(BIND_RECORDING_SQL, (track_id, source_id, track_id)).rowcount:
        raise ValueError("ID Яндекс Музыки уже связан с другой записью.")
    active = """source_id = ? AND compare_existing = 0
                AND (state IN ('queued', 'searching', 'missing')
                     OR (state IN ('found', 'rejected') AND album_request_id IS NULL))"""
    files = [row["file_path"] for row in conn.execute(
        f"SELECT file_path FROM artist_import_items WHERE {active} AND file_path IS NOT NULL", (source_id,),
    )]
    conn.execute(
        f"""UPDATE artist_import_items SET state = 'existing', track_id = ?, attempt = attempt + 1,
                   file_path = NULL, lease_until = 0, issue = NULL
            WHERE {active}""", (track_id, source_id),
    )
    return files


def enqueue_plan(plan: ImportPlan) -> int:
    key = metadata_key(plan.local_name)
    unused_files = []
    with closing(catalog.connect(Path(db.DB_PATH), True)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """SELECT artist_key, yandex_artist_id, updated_at FROM artist_catalog_bindings
               WHERE artist_key = ? OR yandex_artist_id = ?""", (key, plan.artist_id),
        ).fetchall()
        expected = [] if plan.revision is None else [(key, plan.artist_id, plan.revision)]
        if [tuple(row) for row in current] != expected:
            raise ValueError("Каталог успел обновиться. Повторите команду.")
        local = catalog.LocalTracks(conn, plan.local_name, catalog.load_bindings(conn))
        loose = [track for track, album in plan.tracks.values() if album is None]
        extras = catalog.Release("", "", None, None, None, loose)
        matches = catalog.inspect_releases(local, plan.artist_id, [*plan.releases, extras])
        matched_ids = {track.id: local_id for rows in matches.values()
                       for track, local_id, _ in rows if local_id is not None}
        catalog.save_recording_metadata(conn, (track for track, _ in plan.tracks.values()), plan.releases)
        catalog.save_catalog(conn, key, plan.local_name, plan.artist_id, plan.source_name,
                             plan.releases, matches, plan.covers, time.time_ns())
        conn.execute("INSERT OR IGNORE INTO artist_import_batches(artist_key) VALUES (?)", (key,))
        batch_id = conn.execute("SELECT id FROM artist_import_batches WHERE artist_key = ?", (key,)).fetchone()["id"]
        rows = []
        for track, album in plan.tracks.values():
            credit = ", ".join(plan.local_name if person_id == plan.artist_id else name
                               for person_id, name in track.credits)
            credited = any(person_id == plan.artist_id for person_id, _ in track.credits)
            local_id = matched_ids.get(track.id)
            rows.append((batch_id, track.id, credit, track.display_title, track.duration_ms, album, credited,
                         "existing" if local_id else "queued", local_id))
        conn.executemany(
            """INSERT INTO artist_import_items
                   (batch_id, source_id, artist, title, duration_ms, release_title, credited, state, track_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(batch_id, source_id) WHERE album_request_id IS NULL DO UPDATE SET
                   state = excluded.state, track_id = excluded.track_id,
                   artist = excluded.artist, title = excluded.title, duration_ms = excluded.duration_ms,
                   release_title = excluded.release_title, credited = excluded.credited
               WHERE artist_import_items.compare_existing = 0
                   AND artist_import_items.state IN ('queued','existing','missing','separate')
                   AND (excluded.track_id IS NOT NULL OR artist_import_items.state IN ('existing','separate'))""", rows,
        )
        for source_id, track_id in matched_ids.items():
            unused_files.extend(_reuse_source_recording(conn, source_id, track_id))
    for path in unused_files:
        remove_staged(path)
    return batch_id


async def batches(limit: int = 15) -> list[dict]:
    async with db.get_db() as conn, conn.execute(
        """SELECT b.id, a.artist_name FROM artist_import_batches b
           JOIN artist_catalog_bindings a USING(artist_key) ORDER BY b.id DESC LIMIT ?""", (limit,),
    ) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def status(batch_id: int) -> tuple[dict, dict] | None:
    async with db.get_db() as conn:
        async with conn.execute(
            """SELECT b.*, a.artist_name FROM artist_import_batches b JOIN artist_catalog_bindings a USING(artist_key)
               WHERE b.id = ?""", (batch_id,),
        ) as cursor:
            batch = await cursor.fetchone()
        if batch is None:
            return None
        async with conn.execute(
            "SELECT state, COUNT(*) FROM artist_import_items WHERE batch_id = ? GROUP BY state", (batch_id,),
        ) as cursor:
            counts = dict(await cursor.fetchall())
        async with conn.execute(
            """SELECT COUNT(*) FROM artist_import_items i WHERE i.batch_id = ? AND i.state = 'queued'
                AND i.album_request_id IS NULL AND i.compare_existing = 0
               AND EXISTS (
                   SELECT 1 FROM artist_import_items other
                   WHERE other.source_id = i.source_id AND other.batch_id != i.batch_id AND other.state = 'found'
                     AND other.album_request_id IS NULL
               )""", (batch_id,),
        ) as cursor:
            waiting = (await cursor.fetchone())[0]
        return dict(batch, waiting_for_review=waiting), counts


async def control_batch(batch_id: int, action: str) -> None:
    async with db.get_db() as conn:
        if action == "retry":
            await conn.execute(
                """UPDATE artist_import_items SET state = 'queued', attempt = attempt + 1, issue = NULL
                   WHERE batch_id = ? AND state = 'missing' AND album_request_id IS NULL""", (batch_id,),
            )
        else:
            await conn.execute("UPDATE artist_import_batches SET paused = ? WHERE id = ?", (action == "pause", batch_id))
        await conn.commit()


async def next_review(batch_id: int, state: str, after: int = 0) -> dict | None:
    async with db.get_db() as conn, conn.execute(
        """SELECT * FROM artist_import_items WHERE batch_id = ? AND state = ? AND id > ?
           ORDER BY id LIMIT 1""", (batch_id, state, after),
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_item(item_id: int) -> dict | None:
    async with db.get_db() as conn, conn.execute(
        "SELECT * FROM artist_import_items WHERE id = ?", (item_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def claim() -> dict | None:
    now = int(time.time())
    async with db.get_db() as conn, conn.execute(
        """UPDATE artist_import_items SET state = 'searching', attempt = attempt + 1, lease_until = ?
           WHERE id = (
               SELECT i.id FROM artist_import_items i JOIN artist_import_batches b ON b.id = i.batch_id
               WHERE (b.paused = 0 OR i.album_request_id IS NOT NULL)
                 AND (i.state = 'queued' OR (i.state = 'searching' AND i.lease_until <= ?))
                 AND (i.album_request_id IS NOT NULL OR i.compare_existing = 1 OR NOT EXISTS (
                     SELECT 1 FROM artist_import_items other
                     WHERE other.source_id = i.source_id AND other.batch_id != i.batch_id
                       AND other.album_request_id IS NULL
                       AND (other.state = 'found' OR (other.state = 'searching' AND other.lease_until > ?))
                 ))
               ORDER BY i.id LIMIT 1
           ) RETURNING *""", (now + LEASE_SECONDS, now, now),
    ) as cursor:
        row = await cursor.fetchone()
        await cursor.fetchall()
        await conn.commit()
        return dict(row) if row else None


def staged_path(item_id: int) -> Path:
    parent = STAGING_DIR / f"{item_id}-{uuid4().hex}"
    parent.mkdir(parents=True)
    return parent / "audio.mp3"


def remove_staged(path: str | Path | None) -> None:
    if not path:
        return
    file = Path(path)
    if file.parent.parent != STAGING_DIR or file.name != "audio.mp3":
        return
    try:
        file.unlink(missing_ok=True)
        file.parent.rmdir()
    except OSError as exc:
        logger.warning("Не удалось очистить временный файл %s: %s", file, exc)


@dataclass
class AudioCandidate:
    path: str
    source: str
    url: str | None
    bitrate: int
    duration: int


def prepare_audio(path: Path, item: dict, source: str, url=None) -> AudioCandidate:
    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("Файл превышает лимит отправки полного трека через Telegram.")
    try:
        info = MP3(path).info
    except Exception as exc:
        raise ValueError("Файл не удалось прочитать как MP3.") from exc
    duration = round(info.length)
    if duration <= 0:
        raise ValueError("В MP3 не удалось измерить длительность.")
    fix_audio_tags(str(path), item["artist"], item["title"])
    return AudioCandidate(str(path), source, url, info.bitrate // 1000, duration)


async def save_search_result(item: dict, audio: AudioCandidate | None, issue=None) -> bool:
    async with db.get_db() as conn, conn.execute(
        """UPDATE artist_import_items SET state = ?, file_path = ?, audio_source = ?, source_url = ?,
                  bitrate = ?, duration = ?, issue = ?, lease_until = 0
           WHERE id = ? AND attempt = ? AND state = 'searching'""",
        ("found" if audio else "unavailable" if item.get("album_request_id") else "missing",
         audio.path if audio else None, audio.source if audio else None,
         audio.url if audio else None, audio.bitrate if audio else None, audio.duration if audio else None,
         issue, item["id"], item["attempt"]),
    ) as cursor:
        await conn.commit()
        return bool(cursor.rowcount)


async def search_audio(item: dict) -> None:
    destination = staged_path(item["id"])
    keep = False
    try:
        seconds = round((item["duration_ms"] or 0) / 1000)
        if item["hitmo_only"]:
            path, error, url = await download_from_hitmo(
                item["artist"], item["title"], destination.parent, seconds, prefer_uncensored=True,
            )
        else:
            path, error, url = await download_track_smart(
                {"artist": item["artist"], "title": item["title"], "duration": seconds},
                uploads_dir=destination.parent, prefer_uncensored=True,
            )
        if not path:
            await save_search_result(item, None, error or "not_found")
            return
        Path(path).replace(destination)
        candidate = await asyncio.to_thread(
            prepare_audio, destination, item, "SoundCloud" if is_soundcloud_url(url or "") else "Hitmo", url,
        )
        if seconds and not duration_ok(candidate.duration, seconds):
            await save_search_result(item, None, "wrong_version")
            return
        async with db.get_db() as conn, conn.execute(
            "SELECT cover_url FROM yandex_recordings WHERE source_id = ?", (item["source_id"],),
        ) as cursor:
            recording = await cursor.fetchone()
        if recording and catalog.cover_url(recording["cover_url"]):
            image = await asyncio.to_thread(catalog.fetch_cover, recording["cover_url"])
            if image:
                await asyncio.to_thread(embed_cover_bytes, candidate.path, image[0])
        keep = await save_search_result(item, candidate)
    except asyncio.CancelledError:
        # The SQLite worker may have committed before cancellation was delivered.
        keep = True
        raise
    except Exception as exc:
        logger.warning("Ошибка поиска аудио для заявки %s: %s", item["id"], exc)
        await save_search_result(item, None, str(exc) if isinstance(exc, ValueError) else "error")
    finally:
        if not keep:
            remove_staged(destination)


async def worker(send_notice=None) -> None:
    from backend.album_import import send_results

    while True:
        try:
            if send_notice:
                await send_results(send_notice)
            item = await claim()
            if item:
                await search_audio(item)
                continue
        except Exception:
            logger.exception("Ошибка очереди импорта артистов")
        await asyncio.sleep(3)


async def replace_candidate(item: dict, audio: AudioCandidate) -> None:
    async with db.get_db() as conn, conn.execute(
        """UPDATE artist_import_items SET state = 'found', attempt = attempt + 1,
                  file_path = ?, audio_source = ?, source_url = ?, bitrate = ?, duration = ?, issue = NULL
           WHERE id = ? AND attempt = ? AND state IN ('found', 'missing')""",
        (audio.path, audio.source, audio.url, audio.bitrate, audio.duration, item["id"], item["attempt"]),
    ) as cursor:
        if not cursor.rowcount:
            raise ValueError("Заявка уже изменилась. Откройте её заново.")
        await conn.commit()
    remove_staged(item["file_path"])


async def search_hitmo_instead(item_id: int, revision: int) -> int:
    item = await get_item(item_id)
    async with db.get_db() as conn, conn.execute(
        """UPDATE artist_import_items SET state = 'queued', hitmo_only = 1, attempt = attempt + 1,
                  file_path = NULL, audio_source = NULL, source_url = NULL,
                  bitrate = NULL, duration = NULL, issue = NULL
           WHERE id = ? AND attempt = ? AND state = 'found' AND audio_source = 'SoundCloud'
           RETURNING batch_id""", (item_id, revision),
    ) as cursor:
        batch = await cursor.fetchone()
        await cursor.fetchall()
        if batch is None:
            raise ValueError("Заявка уже изменилась. Откройте её заново.")
        await conn.commit()
    remove_staged(item["file_path"] if item else None)
    return batch["batch_id"]


def _approved_audio(track: sqlite3.Row) -> bool:
    return track["status"] == "approved" and bool(track["file_path"] and Path(track["file_path"]).is_file())


def _matching_library_tracks(conn: sqlite3.Connection, item: dict, track_id: int | None = None) -> list[dict]:
    names = catalog.load_bindings(conn)
    artists = split_artist_credits(item["artist"], names)
    credit_keys = {metadata_key(name) for name in artists}
    artists.append(item["artist"])
    artist_norms = {db.norm_key(db.normalize_artist(name)) for name in artists}
    placeholders = ", ".join("?" for _ in artist_norms)
    title = catalog.candidate_title(item["title"])
    query = f"""SELECT id, artist, title, status, file_path, duration, source_url, bitrate, source,
                       quality_status, media_revision, file_hash
                FROM tracks WHERE status = 'approved' AND (
                    (artist_norm IN ({placeholders}) AND (title_norm = ? OR title_norm LIKE ?))
                    OR id IN (SELECT track_id FROM yandex_recordings WHERE source_id = ?))"""
    args = [*artist_norms, db.norm_key(item["title"]), db.norm_key(title) + "%", item["source_id"]]
    if track_id is not None:
        query += " AND id = ?"
        args.append(track_id)
    candidates = []
    for row in conn.execute(query + " ORDER BY id", args):
        if _approved_audio(row) and (
            (artist_credit_keys(row["artist"], names) & credit_keys and catalog.candidate_title(row["title"]) == title)
            or conn.execute(
                "SELECT 1 FROM yandex_recordings WHERE source_id = ? AND track_id = ?", (item["source_id"], row["id"]),
            ).fetchone()
        ):
            candidates.append(dict(row))
    return candidates


def _read_duplicates(item: dict, track_id: int | None) -> list[dict]:
    with closing(catalog.connect(Path(db.DB_PATH), False)) as conn:
        return _matching_library_tracks(conn, item, track_id)


async def duplicate_candidates(item: dict, track_id: int | None = None) -> list[dict]:
    return await asyncio.to_thread(_read_duplicates, item, track_id)


def _linked_track(conn: sqlite3.Connection, item: dict, digest: str | None, selected_id: int | None) -> int | None:
    canonical = conn.execute(APPROVED_RECORDING_SQL, (item["source_id"],)).fetchone()
    if canonical:
        if selected_id is not None and selected_id != canonical["id"]:
            raise ValueError(f"Этот трек Яндекс Музыки уже связан с записью #{canonical['id']}.")
        return canonical["id"]
    linked = conn.execute(catalog.APPROVED_IMPORT_LINKS_SQL + " AND i.source_id = ?", (item["source_id"],)).fetchall()
    if len(linked) > 1:
        raise ValueError("У этого ID Яндекс Музыки несколько записей в базе. Проверьте привязки.")
    if linked:
        track_id = linked[0]["track_id"]
        if selected_id is not None and selected_id != track_id:
            raise ValueError(f"Этот трек Яндекс Музыки уже связан с записью #{track_id}.")
        return track_id

    candidates = _matching_library_tracks(conn, item, selected_id)
    if selected_id is not None:
        if not candidates:
            raise ValueError("Запись больше не доступна или не подходит к этому треку. Откройте карточку заново.")
        candidate = candidates[0]
        if conn.execute("SELECT 1 FROM yandex_recordings WHERE track_id = ?", (selected_id,)).fetchone():
            if (metadata_key(candidate["title"]) != metadata_key(item["title"])
                    or not candidate["duration"] or not item["duration_ms"]
                    or abs(candidate["duration"] * 1000 - item["duration_ms"]) > catalog.DURATION_TOLERANCE_MS):
                raise ValueError("У записи другой ID Яндекс Музыки и отличается название версии или длительность.")
        return selected_id
    if item["album_request_id"] is not None:
        similar = conn.execute("""
            SELECT track_id FROM artist_catalog_tracks e JOIN tracks t ON t.id = e.track_id
            WHERE e.yandex_track_id = ? AND t.status = 'approved' LIMIT 1
        """, (item["source_id"],)).fetchone()
        if similar:
            return similar["track_id"]
    same_title = [
        row for row in candidates
        if metadata_key(row["title"]) == metadata_key(item["title"])
        and not conn.execute("SELECT 1 FROM yandex_recordings WHERE track_id = ?", (row["id"],)).fetchone()
    ]
    existing = conn.execute("SELECT id FROM tracks WHERE file_hash = ?", (digest,)).fetchone()
    if existing:
        if any(row["id"] == existing["id"] for row in same_title):
            return existing["id"]
        raise ValueError(f"Такой MP3 уже есть в записи #{existing['id']}. Проверьте версию или пришлите замену.")
    same_audio_url = [
        row for row in same_title
        if item["source_url"] and row["source_url"] == item["source_url"] and row["duration"] and item["duration"]
        and abs(row["duration"] - item["duration"]) * 1000 <= catalog.DURATION_TOLERANCE_MS
    ]
    return same_audio_url[0]["id"] if len(same_audio_url) == 1 else None


async def queue_comparison(item_id: int, revision: int) -> int:
    async with db.get_db() as conn, conn.execute("""
        UPDATE artist_import_items SET state = 'queued', compare_existing = 1, attempt = attempt + 1,
            hitmo_only = 0, issue = NULL
        WHERE id = ? AND attempt = ? AND state IN ('existing', 'approved') AND album_request_id IS NULL
        RETURNING batch_id
    """, (item_id, revision)) as cursor:
        row = await cursor.fetchone()
        await cursor.fetchall()
        if not row:
            raise ValueError("Карточка уже изменилась. Откройте её заново.")
        await conn.commit()
        return row["batch_id"]


def _replace_library_audio(conn, item, track_id, digest, media_revision, admin_id):
    current = conn.execute("SELECT * FROM tracks WHERE id = ? AND status = 'approved'", (track_id,)).fetchone()
    if not current or current["media_revision"] != media_revision:
        raise ValueError("Аудио в базе изменилось. Сравните версии заново.")
    if conn.execute("SELECT 1 FROM tracks WHERE file_hash = ? AND id != ?", (digest, track_id)).fetchone():
        raise ValueError("Этот файл уже используется в другой записи.")
    if current["file_hash"] == digest:
        raise ValueError("Это тот же файл, который уже используется в базе.")
    conn.execute("""
        UPDATE tracks SET file_path = ?, file_hash = ?, bitrate = ?, duration = ?, source_url = ?, source = ?,
            file_id = NULL, media_revision = media_revision + 1, verified_by = ?, quality_status = 'Verified'
        WHERE id = ?
    """, (item["file_path"], digest, item["bitrate"], item["duration"], item["source_url"],
          item["audio_source"].lower(), admin_id, track_id))
    conn.execute("""
        UPDATE telegram_topics SET rev = rev + 1, run_after = CURRENT_TIMESTAMP
        WHERE playlist_id IN (SELECT playlist_id FROM playlist_tracks WHERE track_id = ?)
    """, (track_id,))
    return current["file_path"]


def _review(item_id: int, revision: int, approve: bool, admin_id: int, selected_id: int | None,
            media_revision: int | None, version_label: str | None) -> dict:
    from backend.album_import import award_publication

    unused_files = []
    old_path = None
    replacing = media_revision is not None
    separate = version_label is not None
    states = {"found", "missing", "rejected"} if approve and selected_id is not None else {"found", "missing"}
    with closing(catalog.connect(Path(db.DB_PATH), True)) as conn, conn:
        row = conn.execute("SELECT * FROM artist_import_items WHERE id = ? AND attempt = ?", (item_id, revision)).fetchone()
        if row is None or row["state"] not in states:
            raise ValueError("Заявка уже обработана или изменилась. Откройте её заново.")
        item = dict(row)
        digest = None
        if approve and (selected_id is None or replacing):
            if item["state"] != "found" or not item["file_path"]:
                raise ValueError("Для одобрения нужен полный MP3.")
            try:
                if replacing:
                    info = MP3(item["file_path"]).info
                    duration = round(info.length)
                    if duration <= 0:
                        raise ValueError("Не удалось измерить длительность нового MP3.")
                    item.update(duration=duration, bitrate=info.bitrate // 1000)
                with Path(item["file_path"]).open("rb") as audio:
                    digest = hashlib.file_digest(audio, "sha256").hexdigest()
            except (OSError, MP3Error) as exc:
                raise ValueError("Не удалось прочитать MP3. Пришлите замену.") from exc

        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM artist_import_items WHERE id = ? AND attempt = ? AND state = ?",
            (item_id, revision, item["state"]),
        ).fetchone() is None:
            raise ValueError("Заявка уже изменилась. Откройте её заново.")

        request_id = item["album_request_id"]
        if request_id and (separate or replacing):
            raise ValueError("Для заявки альбома требуется запись с исходным ID Яндекс Музыки.")
        if separate and conn.execute("SELECT 1 FROM tracks WHERE file_hash = ?", (digest,)).fetchone():
            raise ValueError("Такой MP3 уже есть в библиотеке. Используйте привязку.")
        if separate:
            label = " ".join(version_label.split())
            if not 1 <= len(label) <= 60 or catalog.has_title_suffix(item["title"], label):
                raise ValueError("Укажите новое название версии длиной до 60 символов.")
            item["title"] += f" ({label})"
        existing_recording = conn.execute(APPROVED_RECORDING_SQL, (item["source_id"],)).fetchone()
        track_id = _linked_track(conn, item, digest, selected_id) if approve and not separate else None
        link_source = approve and not separate
        state = "existing" if track_id is not None else "rejected"
        reward = 0
        owner = admin_id
        if request_id:
            owner = conn.execute("SELECT user_id FROM album_import_requests WHERE id = ?", (request_id,)).fetchone()[0]
        if approve and track_id is None:
            track_id = conn.execute(
                """INSERT INTO tracks
                     (artist, title, file_path, file_hash, bitrate, duration, original_duration, import_source,
                      source, source_url, status, quality_status, uploaded_by, verified_by, artist_norm, title_norm)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', 'Verified', ?, ?, ?, ?)""",
                (item["artist"], item["title"], item["file_path"], digest, item["bitrate"], item["duration"],
                 round((item["duration_ms"] or 0) / 1000), "yandex_album" if request_id else "yandex_artist",
                 item["audio_source"].lower(), item["source_url"], owner, admin_id,
                 db.norm_key(db.normalize_artist(item["artist"])), db.norm_key(item["title"])),
            ).lastrowid
            state = "approved" if link_source else "separate"
            if request_id:
                reward = award_publication(conn, item["source_id"], request_id, owner)
        elif replacing:
            old_path = _replace_library_audio(conn, item, track_id, digest, media_revision, admin_id)
        elif item["file_path"]:
            unused_files.append(item["file_path"])
        if link_source and (selected_id is not None or not existing_recording):
            conn.execute("UPDATE tracks SET artist = ?, artist_norm = ? WHERE id = ?",
                         (item["artist"], db.norm_key(db.normalize_artist(item["artist"])), track_id))
        conn.execute(
            """UPDATE artist_import_items SET state = ?, attempt = attempt + 1, track_id = ?, file_path = NULL,
                   compare_existing = 0, reward = ?
               WHERE id = ?""", (state, track_id, reward, item_id),
        )
        if link_source:
            unused_files.extend(_reuse_source_recording(conn, item["source_id"], track_id))
        if old_path and conn.execute("SELECT 1 FROM tracks WHERE file_path = ?", (old_path,)).fetchone():
            old_path = None
    for path in unused_files:
        remove_staged(path)
    if old_path and old_path != item["file_path"]:
        try:
            Path(old_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Не удалось удалить старый файл трека #%s", track_id, exc_info=True)
    return dict(item, reused=state == "existing", replaced=replacing, reward=reward)


async def review(item_id: int, revision: int, approve: bool, admin_id: int,
                 *, existing_track_id: int | None = None, media_revision: int | None = None,
                 version_label: str | None = None) -> dict:
    return await asyncio.to_thread(_review, item_id, revision, approve, admin_id, existing_track_id,
                                   media_revision, version_label)
