"""Асинхронный слой работы с SQLite для FreeDom Music Bot."""
from __future__ import annotations

import hashlib
import os
import secrets
import re
import json
import random
from datetime import datetime, timezone, timedelta
from typing import Any
from contextlib import asynccontextmanager
from uuid import uuid4
import aiosqlite

from backend.artist_catalog_store import ARTIST_CATALOG_SCHEMA, ARTIST_IMPORT_SCHEMA, CATALOG_DB_PATH
from backend.artist_catalog_store import matches_artist_page, metadata_key, split_artist_credits
from backend.config import ADMIN_ID

DB_PATH: str = str(CATALOG_DB_PATH)
LIKED_PLAYLIST_TITLE = "❤️ Мне нравится"
TOPIC_LEASE_SECONDS = 60
PROFILE_DECORATIONS = {
    "vinyl": {"slot": "frame", "name": "Винил", "price": 60},
    "neon": {"slot": "frame", "name": "Неон", "price": 120},
    "equalizer": {"slot": "frame", "name": "Эквалайзер", "price": 180},
    "cassette": {"slot": "item", "name": "Кассета", "price": 240},
}
MINI_APP_SCHEMA = """
    CREATE TABLE IF NOT EXISTS artist_covers (
        artist_key TEXT PRIMARY KEY, jpeg BLOB NOT NULL
    );
    CREATE TABLE IF NOT EXISTS profile_decorations (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        decoration_id TEXT NOT NULL,
        slot TEXT NOT NULL CHECK (slot IN ('frame', 'item')),
        equipped INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, decoration_id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_profile_decoration_equipped
        ON profile_decorations(user_id, slot) WHERE equipped = 1;
    CREATE TABLE IF NOT EXISTS playlist_market_previews (
        playlist_id INTEGER PRIMARY KEY REFERENCES user_playlists(id) ON DELETE CASCADE,
        mode TEXT NOT NULL CHECK (mode IN ('tracks', 'trailer')),
        track_ids TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS playlist_sync_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        source_url TEXT NOT NULL,
        source_title TEXT NOT NULL,
        source_count INTEGER NOT NULL,
        destination_id INTEGER,
        paused INTEGER NOT NULL DEFAULT 0,
        premium_paused INTEGER NOT NULL DEFAULT 0,
        resume_at INTEGER NOT NULL DEFAULT 0,
        next_scan_at INTEGER NOT NULL,
        last_checked_at INTEGER,
        last_added INTEGER NOT NULL DEFAULT 0,
        issue TEXT,
        notice TEXT,
        notify_added INTEGER NOT NULL DEFAULT 0,
        notify_progress INTEGER NOT NULL DEFAULT 0,
        notify_at INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        lease_until INTEGER NOT NULL DEFAULT 0,
        UNIQUE (user_id, source_url)
    );
    CREATE INDEX IF NOT EXISTS idx_playlist_sync_due ON playlist_sync_sources (next_scan_at, lease_until);
    CREATE TABLE IF NOT EXISTS playlist_sync_tracks (
        sync_id INTEGER NOT NULL REFERENCES playlist_sync_sources(id) ON DELETE CASCADE,
        source_track_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        batch INTEGER NOT NULL DEFAULT 0,
        source_position INTEGER,
        artist TEXT NOT NULL,
        title TEXT NOT NULL,
        duration INTEGER NOT NULL,
        state TEXT NOT NULL,
        retry_at INTEGER NOT NULL DEFAULT 0,
        track_id INTEGER,
        PRIMARY KEY (sync_id, source_track_id),
        UNIQUE (sync_id, sequence)
    );
    CREATE INDEX IF NOT EXISTS idx_playlist_sync_tracks_due ON playlist_sync_tracks (sync_id, state, retry_at);
    CREATE INDEX IF NOT EXISTS idx_playlist_sync_catalog_track ON playlist_sync_tracks (track_id);
"""
ACTIVE_PREMIUM_SQL = """u.is_premium = 1 AND (
    NULLIF(u.premium_expires_at, '') IS NULL OR datetime(u.premium_expires_at) > CURRENT_TIMESTAMP
)"""
EXPIRED_PREMIUM_SQL = "datetime(premium_expires_at) <= CURRENT_TIMESTAMP"

# ============================================================
# НОРМАЛИЗАЦИЯ АРТИСТОВ
# ============================================================
def normalize_artist(artist_name: str) -> str:
    if not artist_name: return "Unknown"
    name = artist_name.strip()

    name = re.split(r'\s*(?:&|feat\.?|ft\.?|prod\.?|при\s+уч\.?|,)\s*', name, flags=re.IGNORECASE)[0]
    name = name.strip()

    aliases = {
        "мияги": "Miyagi",
        "miyagi": "Miyagi",
        "эндшпиль": "Miyagi",
        "andy panda": "Miyagi",
        "баста": "Баста",
        "basta": "Баста",
        "ноггано": "Ноггано",
        "noggano": "Ноггано",
        "гуф": "Гуф",
        "guf": "Гуф",
        "скриптонит": "Скриптонит",
        "scriptonite": "Скриптонит"
    }

    lower_name = name.lower()
    if lower_name in aliases:
        return aliases[lower_name]

    return name

def norm_key(s: str) -> str:
    """Ключ нормализации: убирает спецсимволы, приводит к нижнему регистру, заменяет похожие буквы и чистит фиты."""
    if not s: 
        return ""
    s = str(s).casefold()
    
    # Заменяем кириллические буквы, которые похожи на латинские
    homoglyphs = {
        'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 
        'х': 'x', 'к': 'k', 'м': 'm', 'т': 't', 'в': 'b', 'н': 'h', 'п': 'p'
    }
    for cyr, lat in homoglyphs.items():
        s = s.replace(cyr, lat)

    # Чистим от фитов и версий (как в hitmo_parser)
    s = re.sub(r'\s*[\(\[\{].*?[\)\]\}]', '', s)
    s = re.sub(r'\s+[-–—]\s+.*?$', '', s)
    s = re.sub(r'\b(feat|ft|prod|remix|version)\b.*', '', s)
        
    return re.sub(r'[\W_]+', '', s)
# ============================================================

LEVEL_THRESHOLDS = ((3000, "Меценат"), (1501, "Легенда"), (601, "Куратор"), (201, "Коллекционер"), (0, "Новичок"))

# Лимиты по уровням — вынесено, чтобы использовать и в check_download_limit, и в reserve_downloads
LEVEL_LIMITS = {"Новичок": 3, "Коллекционер": 5, "Куратор": 10, "Легенда": 20, "Меценат": 50}

def get_level_by_points(points: int) -> str:
    for threshold, name in LEVEL_THRESHOLDS:
        if points >= threshold: return name
    return "Новичок"

def get_level_progress(points: int) -> dict[str, Any]:
    current_level, current_threshold, next_level, next_threshold = "Новичок", 0, "Коллекционер", 201
    for i, (threshold, name) in enumerate(LEVEL_THRESHOLDS):
        if points >= threshold:
            current_level, current_threshold = name, threshold
            if i > 0: next_threshold, next_level = LEVEL_THRESHOLDS[i - 1]
            else: next_level, next_threshold = "Максимальный уровень", threshold
            break
    progress_percent, remaining = 100, 0
    if next_threshold > current_threshold:
        progress_percent = int(((points - current_threshold) / (next_threshold - current_threshold)) * 100)
        remaining = next_threshold - points
    return {"current_level": current_level, "next_level": next_level, "progress_percent": progress_percent, "remaining": remaining}

@asynccontextmanager
async def get_db():
    conn = await aiosqlite.connect(DB_PATH)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    except BaseException:
        if conn.in_transaction:
            await conn.rollback()
        raise
    finally:
        await conn.close()

async def init_mini_app_schema() -> None:
    async with get_db() as conn:
        await conn.executescript(MINI_APP_SCHEMA)
        await conn.executescript(ARTIST_CATALOG_SCHEMA)
        await conn.executescript(ARTIST_IMPORT_SCHEMA)
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute("PRAGMA table_info(playlist_sync_tracks)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "batch" not in columns:
            await conn.execute("ALTER TABLE playlist_sync_tracks ADD COLUMN batch INTEGER NOT NULL DEFAULT 0")
            await conn.execute("""
                UPDATE playlist_sync_sources SET next_scan_at = 0
                WHERE id IN (SELECT sync_id FROM playlist_sync_tracks)
            """)
        if "source_position" not in columns:
            await conn.execute("ALTER TABLE playlist_sync_tracks ADD COLUMN source_position INTEGER")
        await conn.commit()


async def init_db() -> None:
    async with get_db() as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL, username TEXT,
                points INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, level TEXT DEFAULT 'Новичок',
                referrals_count INTEGER DEFAULT 0, downloads_today INTEGER DEFAULT 0,
                last_download_date DATE, is_premium BOOLEAN DEFAULT 0, premium_expires_at TIMESTAMP,
                premium_notice_hours INTEGER, premium_quota_reset INTEGER NOT NULL DEFAULT 0,
                last_daily_reward TIMESTAMP, referrer_id INTEGER REFERENCES users(id),
                daily_download_limit INTEGER DEFAULT 3, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, artist TEXT NOT NULL,
                file_path TEXT, file_hash TEXT UNIQUE, bitrate INTEGER, duration INTEGER,
                original_duration INTEGER DEFAULT 0, import_source TEXT DEFAULT 'unknown',
                status TEXT DEFAULT 'pending', quality_status TEXT DEFAULT 'none',
                source TEXT DEFAULT 'unknown', source_url TEXT DEFAULT NULL,
                uploaded_by INTEGER REFERENCES users(id), verified_by INTEGER REFERENCES users(id),
                download_count INTEGER DEFAULT 0, complaint_count INTEGER DEFAULT 0,
                pending_playlist_id INTEGER DEFAULT NULL, pending_position INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id),
                source_url TEXT NOT NULL, source_type TEXT NOT NULL, tracks_count INTEGER DEFAULT 0,
                imported_tracks_count INTEGER DEFAULT 0, is_paid BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id),
                amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER REFERENCES users(id),
                title TEXT NOT NULL, is_public BOOLEAN DEFAULT 0, price INTEGER DEFAULT 0,
                sales_count INTEGER DEFAULT 0, original_playlist_id INTEGER DEFAULT NULL,
                trailer_path TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER REFERENCES user_playlists(id),
                track_id INTEGER REFERENCES tracks(id), position INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist_track ON playlist_tracks(playlist_id, track_id);
            CREATE TABLE IF NOT EXISTS likes (
                user_id INTEGER REFERENCES users(id), track_id INTEGER REFERENCES tracks(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, track_id)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
                title, artist, content='tracks', content_rowid='id', tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
                INSERT INTO tracks_fts(rowid, title, artist) VALUES (new.id, new.title, new.artist);
            END;
            CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, title, artist) VALUES('delete', old.id, old.title, old.artist);
            END;
            CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE OF title, artist ON tracks BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, title, artist) VALUES('delete', old.id, old.title, old.artist);
                INSERT INTO tracks_fts(rowid, title, artist) VALUES (new.id, new.title, new.artist);
            END;

            -- ============================================================
            -- ОФФЛАЙН-ПЛЕЙЛИСТЫ (ТЕМЫ В ЛС БОТА)
            -- ============================================================
            CREATE TABLE IF NOT EXISTS telegram_chats (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              kind TEXT NOT NULL DEFAULT 'dm',
              tg_chat_id INTEGER NOT NULL,
              invite_link TEXT,
              state TEXT NOT NULL DEFAULT 'active',
              grace_until TIMESTAMP,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              UNIQUE (user_id, kind)
            );

            CREATE TABLE IF NOT EXISTS telegram_topics (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL REFERENCES telegram_chats(id) ON DELETE CASCADE,
              playlist_id INTEGER REFERENCES user_playlists(id) ON DELETE SET NULL,
              thread_id INTEGER,
              stale_thread_id INTEGER,
              title TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'new',
              rev INTEGER NOT NULL DEFAULT 1,
              synced_rev INTEGER NOT NULL DEFAULT 0,
              run_after TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              attempts INTEGER DEFAULT 0,
              last_error TEXT,
              last_synced_at TIMESTAMP,
              updated_at TIMESTAMP,
              sync_token TEXT,
              move_plan TEXT,
              UNIQUE (chat_id, playlist_id)
            );

            CREATE TABLE IF NOT EXISTS telegram_topic_deletions (
              chat_id INTEGER NOT NULL,
              thread_id INTEGER NOT NULL,
              run_after TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (chat_id, thread_id)
            );

            CREATE TABLE IF NOT EXISTS telegram_topic_tracks (
              topic_id INTEGER NOT NULL REFERENCES telegram_topics(id) ON DELETE CASCADE,
              track_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL,
              position INTEGER,
              PRIMARY KEY (topic_id, track_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ttt_msg ON telegram_topic_tracks(topic_id, message_id);

            CREATE TRIGGER IF NOT EXISTS trg_off_pt_ins AFTER INSERT ON playlist_tracks BEGIN
              UPDATE telegram_topics SET rev = rev + 1, run_after = datetime('now','+30 seconds')
              WHERE playlist_id = NEW.playlist_id;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_off_pt_del AFTER DELETE ON playlist_tracks BEGIN
              UPDATE telegram_topics SET rev = rev + 1, run_after = datetime('now','+30 seconds')
              WHERE playlist_id = OLD.playlist_id;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_off_pt_pos AFTER UPDATE OF position ON playlist_tracks BEGIN
              UPDATE telegram_topics SET rev = rev + 1, run_after = datetime('now','+30 seconds')
              WHERE playlist_id = NEW.playlist_id;
            END;

            """
        )

        await conn.execute("BEGIN IMMEDIATE")

        await conn.execute("DELETE FROM playlist_tracks WHERE track_id NOT IN (SELECT id FROM tracks)")
        await conn.execute("DELETE FROM likes WHERE track_id NOT IN (SELECT id FROM tracks)")
        await conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

        async with conn.execute("PRAGMA table_info(users)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "daily_download_limit" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN daily_download_limit INTEGER DEFAULT 3")
            if "xp" not in cols:
                await conn.execute("ALTER TABLE users ADD COLUMN xp INTEGER DEFAULT 0")
                await conn.execute("UPDATE users SET xp = points")
            if "active_theme" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN active_theme TEXT DEFAULT 'dark'")
            if "unlocked_themes" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN unlocked_themes TEXT DEFAULT '[\"dark\"]'")
            if "loop_playlist" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN loop_playlist BOOLEAN DEFAULT 0")
            if "premium_notice_hours" not in cols:
                await conn.execute("ALTER TABLE users ADD COLUMN premium_notice_hours INTEGER")
            if "premium_quota_reset" not in cols:
                await conn.execute("ALTER TABLE users ADD COLUMN premium_quota_reset INTEGER NOT NULL DEFAULT 0")

        async with conn.execute("PRAGMA table_info(tracks)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "original_duration" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN original_duration INTEGER DEFAULT 0")
            if "import_source" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN import_source TEXT DEFAULT 'unknown'")
            if "pending_playlist_id" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN pending_playlist_id INTEGER DEFAULT NULL")
            if "pending_position" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN pending_position INTEGER DEFAULT 0")
            if "source_url" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN source_url TEXT DEFAULT NULL")
            if "file_id" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN file_id TEXT DEFAULT NULL")
            # --- НОВЫЕ КОЛОНКИ ДЛЯ БЫСТРОГО ПОИСКА ДУБЛИКАТОВ ---
            if "artist_norm" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN artist_norm TEXT DEFAULT NULL")
            if "title_norm" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN title_norm TEXT DEFAULT NULL")

        async with conn.execute("PRAGMA table_info(playlist_tracks)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "position" not in cols: await conn.execute("ALTER TABLE playlist_tracks ADD COLUMN position INTEGER DEFAULT 0")

        await _migrate_offline_topics(conn)

        async with conn.execute("PRAGMA table_info(user_playlists)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "price" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN price INTEGER DEFAULT 0")
            if "sales_count" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN sales_count INTEGER DEFAULT 0")
            if "original_playlist_id" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN original_playlist_id INTEGER DEFAULT NULL")
            if "trailer_path" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN trailer_path TEXT DEFAULT NULL")
            if "custom_cover" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN custom_cover TEXT DEFAULT NULL")

        async with conn.execute("PRAGMA table_info(likes)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "position" not in cols: await conn.execute("ALTER TABLE likes ADD COLUMN position INTEGER DEFAULT 0")

        await conn.commit()

        await _migrate_system_playlists(conn)

        # --- ЗАПОЛНЕНИЕ artist_norm / title_norm ДЛЯ СТАРЫХ ТРЕКОВ ---
        async with conn.execute(
            "SELECT id, artist, title FROM tracks WHERE artist_norm IS NULL OR title_norm IS NULL"
        ) as cursor:
            to_fill = await cursor.fetchall()
        if to_fill:
            for r in to_fill:
                a_norm = norm_key(normalize_artist(r["artist"]))
                t_norm = norm_key(r["title"])
                await conn.execute(
                    "UPDATE tracks SET artist_norm = ?, title_norm = ? WHERE id = ?",
                    (a_norm, t_norm, r["id"])
                )
            await conn.commit()

        # --- ИНДЕКС ДЛЯ БЫСТРОГО ПОИСКА ДУБЛИКАТОВ ---
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_norm ON tracks(artist_norm, title_norm)"
        )
        await conn.commit()
    await init_mini_app_schema()

async def _migrate_offline_topics(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA table_info(telegram_topics)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "updated_at" not in columns:
        await conn.execute("ALTER TABLE telegram_topics ADD COLUMN updated_at TIMESTAMP")
    if "sync_token" not in columns:
        await conn.execute("ALTER TABLE telegram_topics ADD COLUMN sync_token TEXT")
    if "move_plan" not in columns:
        await conn.execute("ALTER TABLE telegram_topics ADD COLUMN move_plan TEXT")
    if "stale_thread_id" not in columns:
        await conn.execute("ALTER TABLE telegram_topics ADD COLUMN stale_thread_id INTEGER")

    async with conn.execute("PRAGMA table_info(telegram_topic_tracks)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "position" not in columns:
        await conn.execute("ALTER TABLE telegram_topic_tracks ADD COLUMN position INTEGER")
        # Для уже синхронизированных тем позиции можно восстановить из плейлиста.
        await conn.execute("""
            UPDATE telegram_topic_tracks AS m
            SET position = (
                SELECT COALESCE(pt.position, 0)
                FROM telegram_topics t JOIN playlist_tracks pt ON pt.playlist_id = t.playlist_id
                WHERE t.id = m.topic_id AND pt.track_id = m.track_id
                ORDER BY pt.position, pt.added_at DESC, pt.id DESC LIMIT 1
            )
            WHERE topic_id IN (SELECT id FROM telegram_topics WHERE state = 'ready' AND synced_rev = rev)
        """)

    await conn.execute("DROP TRIGGER IF EXISTS trg_off_track_status")
    await conn.execute("""
        CREATE TRIGGER trg_off_track_status AFTER UPDATE OF status ON tracks
        WHEN (OLD.status IS 'approved') <> (NEW.status IS 'approved') BEGIN
            UPDATE telegram_topics SET rev = rev + 1, run_after = datetime('now', '+30 seconds')
            WHERE playlist_id IN (SELECT playlist_id FROM playlist_tracks WHERE track_id = NEW.id);
        END
    """)


async def _migrate_system_playlists(conn: aiosqlite.Connection) -> None:
    await conn.execute("BEGIN IMMEDIATE")
    async with conn.execute("PRAGMA table_info(user_playlists)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if "is_system" not in columns:
        await conn.execute("ALTER TABLE user_playlists ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
        # Для владельцев с лайками сохраняем плейлист, привязанный к теме, иначе берём первый по id.
        await conn.execute(
            """WITH candidates AS (
                   SELECT p.id, ROW_NUMBER() OVER (
                       PARTITION BY p.user_id
                       ORDER BY EXISTS (
                           SELECT 1 FROM telegram_topics t JOIN telegram_chats c ON c.id = t.chat_id
                           WHERE t.playlist_id = p.id AND c.user_id = p.user_id AND t.title = ?
                       ) DESC, p.id
                   ) AS preference
                   FROM user_playlists p
                   WHERE p.title = ? AND EXISTS (SELECT 1 FROM likes l WHERE l.user_id = p.user_id)
               )
               UPDATE user_playlists SET is_system = 1
               WHERE id IN (SELECT id FROM candidates WHERE preference = 1)""",
            (LIKED_PLAYLIST_TITLE, LIKED_PLAYLIST_TITLE),
        )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_playlists_system ON user_playlists(user_id) WHERE is_system = 1"
    )
    await conn.commit()

async def get_user(telegram_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            if row: return dict(row)
        await conn.execute("INSERT INTO users (telegram_id) VALUES (?)", (telegram_id,))
        await conn.commit()
        async with conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            return dict(await cursor.fetchone())

async def add_points(user_id: int, amount: int, reason: str = "bonus") -> dict[str, Any]:
    async with get_db() as conn:
        if amount > 0: await conn.execute("UPDATE users SET points = points + ?, xp = xp + ? WHERE id = ?", (amount, amount, user_id))
        else: await conn.execute("UPDATE users SET points = points + ? WHERE id = ?", (amount, user_id))
        await conn.execute("INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)", (user_id, amount, reason))
        await conn.commit()
    return await check_level_up(user_id)

async def check_level_up(user_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        async with conn.execute("SELECT id, points, level, xp FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None: raise ValueError("Пользователь не найден")
            new_level = get_level_by_points(int(row["xp"]))
            leveled_up = new_level != row["level"]
            if leveled_up:
                await conn.execute("UPDATE users SET level = ? WHERE id = ?", (new_level, user_id))
                await conn.commit()
            async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as c2:
                res = dict(await c2.fetchone())
                res["leveled_up"] = leveled_up
                return res

def _compute_file_hash(file_path: str | None, title: str, artist: str, user_id: int) -> str:
    if file_path and os.path.isfile(file_path):
        digest = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""): digest.update(chunk)
        return digest.hexdigest()
    payload = f"{user_id}|{title}|{artist}|{file_path or ''}|{secrets.token_hex(8)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

# ============================================================
# ОБНОВЛЁННЫЙ UPLOAD_TRACK (нормализованная проверка дубликатов)
# ============================================================
async def upload_track(user_id: int, title: str, artist: str, file_path: str, source: str, bitrate: int = 0, duration: int = 0, original_duration: int = 0, import_source: str = "unknown", pending_playlist_id: int | str | None = None, pending_position: int = 0, source_url: str = None, *, import_liked_after: list[int] | None = None, automatic: bool = False) -> dict[str, Any]:
    file_hash = _compute_file_hash(file_path, title, artist, user_id)
    
    a_norm = norm_key(normalize_artist(artist))
    t_norm = norm_key(title)

    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        # Быстрая проверка по нормализованным колонкам
        async with conn.execute(
            "SELECT * FROM tracks WHERE artist_norm = ? AND title_norm = ? LIMIT 1",
            (a_norm, t_norm)
        ) as cursor:
            existing = await cursor.fetchone()

        if existing:
            # Если трек был в базе, но файла нет — перезаписываем его, а не падаем
            existing_file = existing["file_path"]
            has_file = existing_file and os.path.isfile(existing_file)
            if automatic and (has_file or not file_path):
                return dict(existing)
            if has_file:
                raise ValueError(f"Трек '{artist} — {title}' уже есть в базе!")
            else:
                if automatic:
                    user_id = existing["uploaded_by"] or user_id
                    pending_playlist_id = existing["pending_playlist_id"]
                    pending_position = existing["pending_position"]
                await conn.execute(
                    """UPDATE tracks SET
                          title = ?, artist = ?, file_path = ?, file_hash = ?,
                          source = ?, source_url = ?, uploaded_by = ?,
                          bitrate = ?, duration = ?, original_duration = ?,
                          import_source = ?, status = 'pending',
                          pending_playlist_id = ?, pending_position = ?,
                          file_id = NULL
                       WHERE id = ?""",
                    (title, artist, file_path, file_hash,
                     source, source_url, user_id,
                     bitrate, duration, original_duration,
                     import_source, pending_playlist_id, pending_position,
                     existing["id"])
                )
                if import_liked_after is not None:
                    like_added = await _insert_import_like(conn, user_id, existing["id"], import_liked_after)
                await conn.commit()
                async with conn.execute("SELECT * FROM tracks WHERE id = ?", (existing["id"],)) as c:
                    saved = dict(await c.fetchone())
                if import_liked_after is not None:
                    saved["import_like_added"] = like_added
                return saved

        try:
            cursor = await conn.execute(
                """INSERT INTO tracks (title, artist, file_path, file_hash, source, source_url, uploaded_by, bitrate, duration, original_duration, import_source, status, pending_playlist_id, pending_position, artist_norm, title_norm)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (title, artist, file_path, file_hash, source, source_url, user_id, bitrate, duration, original_duration, import_source, pending_playlist_id, pending_position, a_norm, t_norm)
            )
            track_id = cursor.lastrowid
            if import_liked_after is not None:
                like_added = await _insert_import_like(conn, user_id, track_id, import_liked_after)
            await conn.commit()
            async with conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)) as c:
                saved = dict(await c.fetchone())
            if import_liked_after is not None:
                saved["import_like_added"] = like_added
            return saved
        except aiosqlite.IntegrityError:
            raise ValueError("Трек с таким файлом уже существует (совпал хэш).")

async def import_playlist(user_id: int, source_url: str, source_type: str, is_paid: bool) -> dict[str, Any]:
    async with get_db() as conn:
        cursor = await conn.execute("INSERT INTO playlists (user_id, source_url, source_type, is_paid) VALUES (?, ?, ?, ?)", (user_id, source_url, source_type, 1 if is_paid else 0))
        await conn.commit()
        async with conn.execute("SELECT * FROM playlists WHERE id = ?", (cursor.lastrowid,)) as c:
            return dict(await c.fetchone())

async def claim_daily(user_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        time_24h_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        reward = 30
        cursor = await conn.execute("UPDATE users SET points = points + ?, xp = xp + ?, last_daily_reward = ? WHERE id = ? AND (last_daily_reward IS NULL OR last_daily_reward <= ?)", (reward, reward, now_str, user_id, time_24h_ago))
        if cursor.rowcount == 0:
            async with conn.execute("SELECT last_daily_reward FROM users WHERE id = ?", (user_id,)) as c:
                row = await c.fetchone()
                if row and row["last_daily_reward"]:
                    last_dt = datetime.strptime(row["last_daily_reward"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    time_left = timedelta(hours=24) - (now - last_dt)
                    hours, remainder = divmod(time_left.seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    return {"ok": False, "msg": f"Приходи через {hours} ч. {minutes} мин."}
            return {"ok": False, "msg": "Бонус уже получен."}
        await conn.execute("INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)", (user_id, reward, "daily_bonus"))
        await conn.commit()
    updated_user = await check_level_up(user_id)
    return {"ok": True, "reward": reward, "user": updated_user}

async def process_referral(new_user_id: int, referrer_tg_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        async with conn.execute("SELECT id FROM users WHERE telegram_id = ?", (referrer_tg_id,)) as cursor:
            referrer = await cursor.fetchone()
        if not referrer: return {"ok": False, "msg": "Пригласивший не найден"}
        referrer_id = referrer["id"]
        await conn.execute("UPDATE users SET referrer_id = ? WHERE id = ?", (referrer_id, new_user_id))
        bonus = 100
        await conn.execute("UPDATE users SET points = points + ?, xp = xp + ?, referrals_count = referrals_count + 1 WHERE id = ?", (bonus, bonus, referrer_id))
        await conn.execute("INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)", (referrer_id, bonus, f"referral_from_{new_user_id}"))
        await conn.commit()
    return {"ok": True, "bonus": bonus}

async def reset_download_limit(user_id: int) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE users SET downloads_today = 0 WHERE id = ?", (user_id,))
        await conn.commit()

async def set_daily_limit(user_id: int, new_limit: int) -> None:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await _reset_premium_quota(conn, user_id)
        await conn.execute("UPDATE users SET daily_download_limit = ?, downloads_today = 0 WHERE id = ?", (new_limit, user_id))
        await conn.commit()

def _free_download_quota(user: dict, now: datetime) -> tuple[int, int]:
    today = now.date().isoformat()
    used = (user["downloads_today"] or 0) if user["last_download_date"] == today else 0
    level_limit = LEVEL_LIMITS.get(user["level"], 3)
    expiry = parse_premium_expiry(user["premium_expires_at"])
    if expiry and expiry <= now and not user["premium_quota_reset"]:
        return level_limit, used if today > expiry.date().isoformat() else 0
    override = user["daily_download_limit"]
    if user["telegram_id"] == ADMIN_ID and override is not None:
        return override, used
    if user["telegram_id"] == ADMIN_ID and not user["premium_quota_reset"]:
        return 3, used
    return max(level_limit, override or 0), used


async def check_download_limit(user_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
    if not row:
        return {"allowed": False, "msg": "Пользователь не найден"}
    user = dict(row)
    now = datetime.now(timezone.utc)
    if is_premium_active(user, now=now):
        return {"allowed": True, "is_premium": True, "remaining": None, "limit": None, "used": 0}
    limit, used = _free_download_quota(user, now)
    result = {"allowed": used < limit, "is_premium": False, "remaining": max(0, limit - used), "limit": limit, "used": used}
    if not result["allowed"]:
        result["msg"] = f"Лимит бесплатных скачиваний на сегодня исчерпан ({limit}/{limit}). Повысь уровень или купи Premium!"
    return result

# ============================================================
# АТОМАРНЫЕ ЛИМИТЫ: резервирование и возврат
# ============================================================
async def reserve_downloads(user_id: int, want: int = 1) -> tuple[int, str | None]:
    """Возвращает число выданных скачиваний и дату списания из бесплатного лимита."""
    if want <= 0: return 0, None
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(timezone.utc)
            await _reset_premium_quota(conn, user_id, now=now)
            async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                await conn.rollback()
                return 0, None

            user = dict(row)
            if is_premium_active(user, now=now):
                await conn.commit()
                return want, None

            today = now.date().isoformat()
            limit, used = _free_download_quota(user, now)
            grant = max(0, min(want, limit - used))
            if grant:
                await conn.execute(
                    "UPDATE users SET downloads_today = ?, last_download_date = ? WHERE id = ?",
                    (used + grant, today, user_id)
                )
            await conn.commit()
            return grant, today if grant else None
        except Exception:
            await conn.rollback()
            raise

async def refund_downloads(user_id: int, n: int, quota_date: str | None) -> None:
    """Возвращает лимит, если трек не скачался."""
    if n <= 0 or quota_date is None: return
    async with get_db() as conn:
        await conn.execute(
            """UPDATE users SET downloads_today = MAX(downloads_today - ?, 0)
               WHERE id = ? AND last_download_date = ?""",
            (n, user_id, quota_date)
        )
        await conn.commit()
# ============================================================

async def increment_download_count(user_id: int, count: int) -> None:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await _reset_premium_quota(conn, user_id)
        await conn.execute("""
            UPDATE users SET downloads_today = ? +
                CASE WHEN last_download_date = CURRENT_DATE THEN COALESCE(downloads_today, 0) ELSE 0 END,
                last_download_date = CURRENT_DATE
            WHERE id = ?
        """, (count, user_id))
        await conn.commit()

# ============================================================
# ОБНОВЛЁННЫЙ CHECK_TRACK_EXISTS (безопасный, без DELETE)
# ============================================================
async def check_track_exists(artist: str, title: str) -> int | None:
    a_norm = norm_key(normalize_artist(artist))
    t_norm = norm_key(title)

    if not a_norm or not t_norm:
        return None

    async with get_db() as conn:
        async with conn.execute(
            "SELECT id, file_path FROM tracks WHERE artist_norm = ? AND title_norm = ? LIMIT 1",
            (a_norm, t_norm)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        if row["file_path"] and os.path.isfile(row["file_path"]):
            return row["id"]

        # Просто возвращаем -1. Не удаляем, чтобы не сломать внешние ключи (плейлисты/лайки).
        # upload_track увидит существующую запись и перезапишет её новым файлом.
        return -1
# ============================================================

async def get_pending_tracks(limit: int = 10) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM tracks WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def update_track_status(track_id: int, new_status: str, admin_id: int) -> None:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT status, bitrate, uploaded_by, pending_playlist_id, pending_position FROM tracks WHERE id = ?",
            (track_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return

        quality = 'none'
        if new_status == 'approved':
            quality = 'Verified+' if (row["bitrate"] or 0) >= 320 else 'Verified'
            destination = row["pending_playlist_id"]
            position = row["pending_position"]
            if destination == 'liked':
                await _insert_import_like(conn, row["uploaded_by"], track_id, [])
            elif destination is not None:
                await conn.execute(
                    """INSERT INTO playlist_tracks (playlist_id, track_id, position)
                       SELECT ?, ?, ? WHERE NOT EXISTS (
                           SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?
                       )""",
                    (destination, track_id, position, destination, track_id),
                )
            await conn.execute(
                "UPDATE tracks SET pending_playlist_id = NULL, pending_position = 0 WHERE id = ?",
                (track_id,),
            )

        await conn.execute("UPDATE tracks SET status = ?, verified_by = ?, quality_status = ? WHERE id = ?", (new_status, admin_id, quality, track_id))
        if new_status == "approved" and row["status"] != "approved":
            await conn.execute("""
                UPDATE playlist_sync_tracks SET state = 'recheck', retry_at = 0
                WHERE track_id = ? AND state IN ('pending', 'unavailable', 'rejected', 'verified')
            """, (track_id,))
            # Запрос оффлайн-темы через API мог обновить копию, пока трек был на модерации.
            async with conn.execute("SELECT user_id FROM likes WHERE track_id = ?", (track_id,)) as cursor:
                users = await cursor.fetchall()
            for user in users:
                await _sync_liked_playlist(conn, user["user_id"])
        elif new_status == "rejected":
            await _stop_sync_track(conn, track_id)
        await conn.commit()

# ============================================================
# ОБНОВЛЁННЫЙ SEARCH_TRACKS (безопасный FTS5)
# ============================================================
async def search_tracks(query: str, limit: int = 10) -> list[dict[str, Any]]:
    safe_query = re.sub(r'[^\w\s]', ' ', query).strip()
    if not safe_query: return []
    
    words = [w for w in safe_query.split() if len(w) >= 2]
    if not words: return []
    search_str = " AND ".join([f'"{w}"*' for w in words])
    
    async with get_db() as conn:
        try:
            sql = "SELECT t.* FROM tracks t JOIN tracks_fts fts ON t.id = fts.rowid WHERE tracks_fts MATCH ? AND t.status = 'approved' ORDER BY t.download_count DESC LIMIT ?"
            async with conn.execute(sql, (search_str, limit)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        except Exception as e:
            print(f"[DB] Ошибка поиска FTS5: {e}")
            return []
# ============================================================

async def get_track_by_id(track_id: int, *, approved_only: bool = False) -> dict[str, Any] | None:
    query = "SELECT * FROM tracks WHERE id = ?"
    if approved_only:
        query += " AND status = 'approved'"
    async with get_db() as conn:
        async with conn.execute(query, (track_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def increment_track_download(track_id: int) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE tracks SET download_count = download_count + 1 WHERE id = ?", (track_id,))
        await conn.commit()

async def get_user_by_internal_id(user_id: int) -> dict[str, Any] | None:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

def parse_premium_expiry(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        return None
    return expiry.astimezone(timezone.utc) if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)


def is_premium_active(user: dict, *, now: datetime | None = None) -> bool:
    if not user.get("is_premium"):
        return False
    raw_expiry = user.get("premium_expires_at")
    if not raw_expiry:
        return True
    expiry = parse_premium_expiry(raw_expiry)
    return expiry is not None and expiry > (now or datetime.now(timezone.utc))


async def give_premium(user_id: int, days: int) -> datetime:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute("SELECT premium_expires_at FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expiry = parse_premium_expiry(row["premium_expires_at"]) if row else None
        if expiry and expiry <= now:
            await _reset_premium_quota(conn, user_id)
            await _queue_premium_topic_purges(conn, user_id)
        expiry = max(now, expiry or now) + timedelta(days=days)
        await conn.execute("""
            UPDATE users SET is_premium = 1, premium_expires_at = ?, premium_notice_hours = NULL,
                premium_quota_reset = 0
            WHERE id = ?
        """, (expiry.strftime("%Y-%m-%d %H:%M:%S"), user_id))
        await conn.execute("""
            UPDATE telegram_chats SET state = 'active' WHERE user_id = ? AND state = 'expired'
        """, (user_id,))
        await conn.commit()
        return expiry

async def remove_premium(user_id: int) -> None:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await _reset_premium_quota(conn, user_id, revoking=True)
        await conn.execute("""
            UPDATE users SET is_premium = 0, premium_expires_at = NULL, premium_notice_hours = NULL
            WHERE id = ?
        """, (user_id,))
        await _queue_premium_topic_purges(conn, user_id)
        await conn.commit()


async def _reset_premium_quota(
    conn: aiosqlite.Connection, user_id: int | None = None, *, now: datetime | None = None,
    revoking: bool = False,
) -> None:
    user_filter = "AND id = ?" if user_id is not None else ""
    as_of = (now or datetime.now(timezone.utc)).isoformat()
    params = (as_of, revoking, user_id) if user_id is not None else (as_of, revoking)
    await conn.execute(f"""
        UPDATE users
        SET daily_download_limit = NULL,
            downloads_today = CASE WHEN last_download_date > date(premium_expires_at)
                                   THEN downloads_today ELSE 0 END,
            premium_quota_reset = 1
        WHERE premium_quota_reset = 0
          AND (datetime(premium_expires_at) <= datetime(?) OR (? AND is_premium = 1))
          {user_filter}
    """, params)


async def _queue_premium_topic_purges(conn: aiosqlite.Connection, user_id: int | None = None) -> None:
    if user_id is None:
        owner_filter = f"user_id IN (SELECT id FROM users WHERE {EXPIRED_PREMIUM_SQL})"
        params = ()
    else:
        owner_filter = "user_id = ?"
        params = (user_id,)
    affected_chats = f"SELECT id FROM telegram_chats WHERE {owner_filter}"
    await conn.execute(f"""
        UPDATE telegram_topics
        SET state = 'purge', playlist_id = NULL, sync_token = NULL,
            run_after = CASE WHEN state = 'syncing' AND thread_id IS NULL AND sync_token IS NOT NULL
                             THEN datetime('now', '+{TOPIC_LEASE_SECONDS} seconds')
                             ELSE CURRENT_TIMESTAMP END
        WHERE state <> 'purge' AND chat_id IN ({affected_chats})
    """, params)
    await conn.execute(f"""
        UPDATE telegram_chats SET state = 'expired'
        WHERE state <> 'expired' AND {owner_filter}
    """, params)


async def collect_premium_notices() -> list[dict]:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await _reset_premium_quota(conn)
        await _queue_premium_topic_purges(conn)
        await conn.execute(f"UPDATE users SET is_premium = 0 WHERE is_premium = 1 AND {EXPIRED_PREMIUM_SQL}")
        async with conn.execute("""
            SELECT id, telegram_id, is_premium, premium_expires_at, premium_notice_hours
            FROM users u
            WHERE (is_premium = 1 AND datetime(premium_expires_at) <= datetime('now', '+72 hours'))
               OR (is_premium = 0 AND datetime(premium_expires_at) <= CURRENT_TIMESTAMP
                   AND premium_notice_hours IS NOT 0
                   AND NOT EXISTS (
                       SELECT 1 FROM telegram_chats c JOIN telegram_topics t ON t.chat_id = c.id
                       WHERE c.user_id = u.id
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM telegram_chats c JOIN telegram_topic_deletions d ON d.chat_id = c.tg_chat_id
                       WHERE c.user_id = u.id
                   ))
            ORDER BY is_premium, datetime(premium_expires_at)
        """) as cursor:
            notices = [dict(user) for user in await cursor.fetchall()]
        await conn.commit()
        return notices


async def mark_premium_notice(user: dict, hours: int) -> None:
    async with get_db() as conn:
        await conn.execute("""
            UPDATE users SET premium_notice_hours = ?
            WHERE id = ? AND is_premium = ? AND premium_expires_at = ?
              AND (premium_notice_hours IS NULL OR premium_notice_hours > ?)
        """, (hours, user["id"], user["is_premium"], user["premium_expires_at"], hours))
        await conn.commit()


async def update_track_metadata(track_id: int, bitrate: int, duration: int) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE tracks SET bitrate = ?, duration = ? WHERE id = ?", (bitrate, duration, track_id))
        await conn.commit()

async def _stop_sync_track(conn, track_id: int) -> None:
    await conn.execute("""
        UPDATE playlist_sync_tracks SET state = 'rejected', retry_at = CAST(strftime('%s', 'now') AS INTEGER) + 86400
        WHERE track_id = ? AND state IN ('pending', 'unavailable', 'recheck', 'verified')
    """, (track_id,))


async def delete_track(track_id: int) -> bool:
    async with get_db() as conn:
        async with conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)) as cursor:
            row = await cursor.fetchone()
        if not row: return False
        file_path = row["file_path"]
        if file_path and os.path.isfile(file_path):
            try: os.remove(file_path)
            except Exception as e: print(f"Не удалось удалить файл {file_path}: {e}")
            
        await conn.execute("DELETE FROM playlist_tracks WHERE track_id = ?", (track_id,))
        await conn.execute("DELETE FROM likes WHERE track_id = ?", (track_id,))
        await _stop_sync_track(conn, track_id)
        await conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        await conn.commit()
        return True

async def get_hunt_tracks(limit: int = 30) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute("SELECT id, title, artist, complaint_count, status FROM tracks WHERE status = 'pending' AND complaint_count > 0 ORDER BY complaint_count DESC LIMIT ?", (limit,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def create_user_playlist(user_id: int, title: str, *, conn: aiosqlite.Connection | None = None) -> dict[str, Any]:
    if conn is None:
        async with get_db() as conn:
            playlist = await create_user_playlist(user_id, title, conn=conn)
            await conn.commit()
            return playlist
    cursor = await conn.execute("INSERT INTO user_playlists (user_id, title) VALUES (?, ?)", (user_id, title))
    return {"id": cursor.lastrowid, "title": title, "status": "created"}

async def get_user_playlists(user_id: int, exclude_track_id: int = None) -> list[dict[str, Any]]:
    async with get_db() as conn:
        if exclude_track_id:
            query = """
                SELECT p.id, p.title, p.price, p.is_public, p.sales_count, p.original_playlist_id, p.trailer_path, p.custom_cover, COUNT(t.id) as track_count, orig_u.username as original_seller_username, orig_u.telegram_id as original_seller_tg
                FROM user_playlists p 
                LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id
                LEFT JOIN tracks t ON t.id = pt.track_id AND t.status = 'approved'
                LEFT JOIN user_playlists orig_p ON p.original_playlist_id = orig_p.id 
                LEFT JOIN users orig_u ON orig_p.user_id = orig_u.id
                WHERE p.user_id = ? AND p.is_system = 0
                  AND p.id NOT IN (SELECT playlist_id FROM playlist_tracks WHERE track_id = ?)
                GROUP BY p.id
            """
            async with conn.execute(query, (user_id, exclude_track_id)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        else:
            query = """
                SELECT p.id, p.title, p.price, p.is_public, p.sales_count, p.original_playlist_id, p.trailer_path, p.custom_cover, COUNT(t.id) as track_count, orig_u.username as original_seller_username, orig_u.telegram_id as original_seller_tg
                FROM user_playlists p 
                LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id
                LEFT JOIN tracks t ON t.id = pt.track_id AND t.status = 'approved'
                LEFT JOIN user_playlists orig_p ON p.original_playlist_id = orig_p.id 
                LEFT JOIN users orig_u ON orig_p.user_id = orig_u.id
                WHERE p.user_id = ? AND p.is_system = 0
                GROUP BY p.id
            """
            async with conn.execute(query, (user_id,)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

async def get_playlist_by_id(playlist_id: int) -> dict[str, Any] | None:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM user_playlists WHERE id = ?", (playlist_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def add_track_to_playlist(
    playlist_id: int, track_id: int, position: int = 0, *, conn: aiosqlite.Connection | None = None
) -> bool:
    if conn is None:
        async with get_db() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await add_track_to_playlist(playlist_id, track_id, position, conn=conn)
            await conn.commit()
            return True

    async with conn.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)
    ) as cursor:
        row = await cursor.fetchone()
    if row:
        await conn.execute(
            "UPDATE playlist_tracks SET position = ? WHERE playlist_id = ? AND track_id = ?",
            (position, playlist_id, track_id),
        )
    else:
        await conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            (playlist_id, track_id, position),
        )
    return True

async def get_playlist_tracks(playlist_id: int) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute(
            """SELECT t.id, t.artist, t.title, t.quality_status, t.file_path, t.file_id
               FROM playlist_tracks pt 
               JOIN tracks t ON pt.track_id = t.id 
               WHERE pt.playlist_id = ? AND t.status = 'approved'
               ORDER BY pt.position ASC, pt.added_at DESC""", (playlist_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def set_playlist_price(playlist_id: int, price: int) -> bool:
    async with get_db() as conn:
        async with conn.execute("SELECT original_playlist_id FROM user_playlists WHERE id = ?", (playlist_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row["original_playlist_id"] is not None: raise ValueError("Нельзя продавать купленные плейлисты!")
        await conn.execute("UPDATE user_playlists SET price = ?, is_public = 1 WHERE id = ?", (price, playlist_id))
        await conn.commit()
        return True

async def get_market_playlists() -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute(
            """SELECT p.id, p.title, p.price, p.sales_count, COUNT(t.id) as track_count,
                      u.username, u.telegram_id AS seller_tg,
                      f.decoration_id AS seller_frame, d.decoration_id AS seller_item
               FROM user_playlists p JOIN users u ON p.user_id = u.id LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id
               LEFT JOIN tracks t ON t.id = pt.track_id AND t.status = 'approved'
               LEFT JOIN profile_decorations f ON f.user_id = u.id AND f.slot = 'frame' AND f.equipped = 1
               LEFT JOIN profile_decorations d ON d.user_id = u.id AND d.slot = 'item' AND d.equipped = 1
               WHERE p.is_public = 1 AND p.price > 0 AND p.is_system = 0
               GROUP BY p.id ORDER BY p.price ASC""") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_playlist_market_preview(playlist_id: int, buyer_id: int | None = None) -> dict | None:
    async with get_db() as conn:
        async with conn.execute(
            """SELECT p.*, m.mode AS preview_mode, m.track_ids AS preview_ids, u.username,
                      (SELECT id FROM user_playlists WHERE user_id = ? AND original_playlist_id = p.id
                       ORDER BY id LIMIT 1) AS bought_id
               FROM user_playlists p
               JOIN users u ON p.user_id = u.id
               LEFT JOIN playlist_market_previews m ON p.id = m.playlist_id
               WHERE p.id = ? AND p.is_system = 0""", (buyer_id, playlist_id)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_playlist_preview_tracks(playlist_id: int, user_id: int, track_ids: list[int]) -> None:
    if len(track_ids) != 3 or len(set(track_ids)) != 3:
        raise ValueError("Выберите три разных трека.")
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            """SELECT 1 FROM user_playlists
               WHERE id = ? AND user_id = ? AND is_system = 0 AND original_playlist_id IS NULL""",
            (playlist_id, user_id),
        ) as cursor:
            if not await cursor.fetchone():
                raise PermissionError("Предпросмотр может менять только автор плейлиста.")
        async with conn.execute(
            """SELECT DISTINCT t.id, t.file_path
               FROM playlist_tracks pt JOIN tracks t ON pt.track_id = t.id
               WHERE pt.playlist_id = ? AND t.status = 'approved' AND t.id IN (?, ?, ?)""",
            (playlist_id, *track_ids),
        ) as cursor:
            files = await cursor.fetchall()
        if len(files) != 3 or any(not row["file_path"] or not os.path.isfile(row["file_path"]) for row in files):
            raise ValueError("Все три трека должны быть доступны в этом плейлисте.")
        await conn.execute(
            """INSERT INTO playlist_market_previews (playlist_id, mode, track_ids) VALUES (?, 'tracks', ?)
               ON CONFLICT(playlist_id) DO UPDATE SET mode = excluded.mode, track_ids = excluded.track_ids""",
            (playlist_id, json.dumps(track_ids)),
        )
        await conn.commit()


async def buy_playlist(buyer_id: int, playlist_id: int, expected_price: int | None = None) -> dict[str, Any]:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute("SELECT id FROM user_playlists WHERE user_id = ? AND original_playlist_id = ?", (buyer_id, playlist_id)) as cursor:
            if await cursor.fetchone(): return {"ok": False, "msg": "Вы уже покупали этот плейлист!"}
        async with conn.execute("SELECT * FROM user_playlists WHERE id = ?", (playlist_id,)) as cursor:
            pl = await cursor.fetchone()
            if not pl or not pl["is_public"] or pl["price"] <= 0: return {"ok": False, "msg": "Плейлист не продается"}
            if pl["user_id"] == buyer_id: return {"ok": False, "msg": "Нельзя купить свой плейлист"}
            price, seller_id, title = pl["price"], pl["user_id"], pl["title"]
        if expected_price is not None and price != expected_price:
            return {"ok": False, "msg": "Цена изменилась. Откройте карточку плейлиста ещё раз."}
        cursor = await conn.execute("UPDATE users SET points = points - ? WHERE id = ? AND points >= ?", (price, buyer_id, price))
        if cursor.rowcount == 0: return {"ok": False, "msg": "Недостаточно поинтов"}
        await conn.execute("INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)", (buyer_id, -price, f"buy_playlist_{playlist_id}"))
        seller_cut = int(price * 0.8)
        await conn.execute("UPDATE users SET points = points + ? WHERE id = ?", (seller_cut, seller_id))
        await conn.execute("INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)", (seller_id, seller_cut, f"sell_playlist_{playlist_id}"))
        await conn.execute("UPDATE user_playlists SET sales_count = sales_count + 1 WHERE id = ?", (playlist_id,))
        cursor = await conn.execute("INSERT INTO user_playlists (user_id, title, original_playlist_id) VALUES (?, ?, ?)", (buyer_id, f"{title} (Куплено)", playlist_id))
        new_playlist_id = cursor.lastrowid
        async with conn.execute("SELECT track_id, position FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)) as cursor:
            tracks = await cursor.fetchall()
            for t in tracks: await conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)", (new_playlist_id, t["track_id"], t["position"]))
        await conn.commit()
        async with conn.execute("SELECT telegram_id FROM users WHERE id = ?", (seller_id,)) as cursor:
            seller_tg = (await cursor.fetchone())["telegram_id"]
        return {"ok": True, "msg": "Плейлист успешно куплен!", "seller_tg": seller_tg, "seller_cut": seller_cut}

async def _clear_playlist_links(conn, playlist_id: int) -> None:
    await conn.execute("""
        UPDATE telegram_topics
        SET state = 'purge', playlist_id = NULL, sync_token = NULL, run_after = CURRENT_TIMESTAMP
        WHERE playlist_id = ?
    """, (playlist_id,))
    await conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))


async def clear_user_playlist(playlist_id: int, user_id: int) -> bool:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT id FROM user_playlists WHERE id = ? AND user_id = ? AND is_system = 0",
            (playlist_id, user_id),
        ) as cursor:
            if not await cursor.fetchone():
                return False
        await _clear_playlist_links(conn, playlist_id)
        await conn.commit()
        return True


async def clear_user_likes(user_id: int) -> None:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("DELETE FROM likes WHERE user_id = ?", (user_id,))
        async with conn.execute(
            "SELECT id FROM user_playlists WHERE user_id = ? AND is_system = 1 AND title = ?",
            (user_id, LIKED_PLAYLIST_TITLE),
        ) as cursor:
            playlist = await cursor.fetchone()
        if playlist:
            await _clear_playlist_links(conn, playlist["id"])
        await conn.commit()


async def delete_user_playlist(playlist_id: int) -> bool:
    async with get_db() as conn:
        await conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        await conn.execute("DELETE FROM user_playlists WHERE id = ?", (playlist_id,))
        await conn.commit()
        return True

async def remove_playlist_from_market(playlist_id: int) -> bool:
    async with get_db() as conn:
        await conn.execute("UPDATE user_playlists SET price = 0, is_public = 0 WHERE id = ?", (playlist_id,))
        await conn.commit()
        return True

async def _ordered_likes(conn, user_id: int) -> list[tuple[int, int]]:
    async with conn.execute(
        "SELECT track_id, position FROM likes WHERE user_id = ? ORDER BY position, rowid",
        (user_id,),
    ) as cursor:
        return [(row["track_id"], row["position"]) for row in await cursor.fetchall()]


async def _write_like_order(conn, user_id: int, track_ids: list[int]) -> None:
    await conn.executemany(
        "UPDATE likes SET position = ? WHERE user_id = ? AND track_id = ?",
        ((position, user_id, track_id) for position, track_id in enumerate(track_ids)),
    )


async def _get_or_create_liked_playlist(conn: aiosqlite.Connection, user_id: int) -> int:
    lookup = "SELECT id FROM user_playlists WHERE user_id = ? AND is_system = 1"
    async with conn.execute(lookup, (user_id,)) as cursor:
        row = await cursor.fetchone()
    if row:
        return row["id"]
    try:
        async with conn.execute(
            "INSERT INTO user_playlists (user_id, title, is_public, price, is_system) VALUES (?, ?, 0, 0, 1)",
            (user_id, LIKED_PLAYLIST_TITLE),
        ) as cursor:
            return cursor.lastrowid
    except aiosqlite.IntegrityError as exc:
        if exc.sqlite_errorname != "SQLITE_CONSTRAINT_UNIQUE":
            raise
        async with conn.execute(lookup, (user_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise
        return row["id"]


async def _sync_liked_playlist(conn: aiosqlite.Connection, user_id: int) -> int:
    """Переносит итоговые позиции из likes в рамках транзакции вызывающей функции."""
    playlist_id = await _get_or_create_liked_playlist(conn, user_id)
    likes = dict(await _ordered_likes(conn, user_id))
    positions = {}
    duplicates = []
    async with conn.execute(
        "SELECT id, track_id, position FROM playlist_tracks WHERE playlist_id = ? ORDER BY position, id",
        (playlist_id,),
    ) as cursor:
        for row in await cursor.fetchall():
            track_id = row["track_id"]
            if track_id in positions:
                duplicates.append((row["id"],))
            else:
                # Сохраняем строку с минимальной позицией, при равенстве — с минимальным id.
                positions[track_id] = row["position"]

    if duplicates:
        await conn.executemany("DELETE FROM playlist_tracks WHERE id = ?", duplicates)
    for track_id in positions.keys() - likes.keys():
        await remove_track_from_playlist(playlist_id, track_id, conn=conn)
    for track_id, position in likes.items():
        if track_id not in positions or positions[track_id] != position:
            await add_track_to_playlist(playlist_id, track_id, position, conn=conn)
    return playlist_id


async def sync_liked_playlist(user_id: int) -> int:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        playlist_id = await _sync_liked_playlist(conn, user_id)
        await conn.commit()
        return playlist_id


async def get_import_liked_tracks(user_id: int) -> dict[tuple[str, str], int]:
    async with get_db() as conn:
        async with conn.execute(
            """SELECT t.artist_norm, t.title_norm, t.id
               FROM likes l JOIN tracks t ON t.id = l.track_id
               WHERE l.user_id = ? ORDER BY l.position, l.rowid""",
            (user_id,),
        ) as cursor:
            return {(row["artist_norm"], row["title_norm"]): row["id"]
                    for row in reversed(await cursor.fetchall())}


async def get_cached_import_keys(keys: list[tuple[str, str]]) -> set[tuple[str, str]]:
    if not keys:
        return set()
    async with get_db() as conn:
        async with conn.execute(
            """SELECT t.artist_norm, t.title_norm, t.file_path
               FROM json_each(?) AS input
               JOIN tracks t ON t.artist_norm = json_extract(input.value, '$[0]')
                            AND t.title_norm = json_extract(input.value, '$[1]')""",
            (json.dumps(keys),),
        ) as cursor:
            return {(row["artist_norm"], row["title_norm"]) for row in await cursor.fetchall()
                    if row["file_path"] and os.path.isfile(row["file_path"])}


async def move_likes_to_top(user_id: int, track_ids: list[int]) -> None:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        current = dict(await _ordered_likes(conn, user_id))
        moved = dict.fromkeys(track_id for track_id in track_ids if track_id in current)
        await _write_like_order(conn, user_id, [*moved, *(tid for tid in current if tid not in moved)])
        await _sync_liked_playlist(conn, user_id)
        await conn.commit()


async def _insert_import_like(conn, user_id: int, track_id: int, after: list[int]) -> bool:
    current = dict(await _ordered_likes(conn, user_id))
    if track_id in current:
        return False
    order = list(current)
    predecessor = next((tid for tid in reversed(after) if tid in current), None)
    position = order.index(predecessor) + 1 if predecessor is not None else 0
    if list(current.values()) != list(range(len(current))):
        await _write_like_order(conn, user_id, order)
    await conn.execute(
        "UPDATE likes SET position = position + 1 WHERE user_id = ? AND position >= ?",
        (user_id, position),
    )
    await conn.execute(
        "INSERT INTO likes (user_id, track_id, position) VALUES (?, ?, ?)",
        (user_id, track_id, position),
    )
    await _sync_liked_playlist(conn, user_id)
    return True


async def add_import_like(user_id: int, track_id: int, after: list[int]) -> bool:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        added = await _insert_import_like(conn, user_id, track_id, after)
        await conn.commit()
        return added


async def toggle_like(user_id: int, track_id: int) -> bool:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        current = dict(await _ordered_likes(conn, user_id))
        added = track_id not in current
        if not added:
            await conn.execute("DELETE FROM likes WHERE user_id = ? AND track_id = ?", (user_id, track_id))
            del current[track_id]
            await _write_like_order(conn, user_id, list(current))
        else:
            if list(current.values()) != list(range(len(current))):
                await _write_like_order(conn, user_id, list(current))
            await conn.execute("UPDATE likes SET position = position + 1 WHERE user_id = ?", (user_id,))
            await conn.execute("INSERT INTO likes (user_id, track_id, position) VALUES (?, ?, 0)", (user_id, track_id))
        await _sync_liked_playlist(conn, user_id)
        await conn.commit()
        return added

async def get_user_liked_tracks(user_id: int) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute("""
            SELECT t.id, t.artist, t.title, t.quality_status 
            FROM likes l 
            JOIN tracks t ON l.track_id = t.id 
            WHERE l.user_id = ? AND t.status = 'approved'
            ORDER BY l.position ASC, l.rowid ASC
        """, (user_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_user_liked_track_ids(user_id: int) -> list[int]:
    async with get_db() as conn:
        async with conn.execute("SELECT l.track_id FROM likes l JOIN tracks t ON t.id = l.track_id WHERE l.user_id = ? AND t.status = 'approved'", (user_id,)) as cursor:
            return [row["track_id"] for row in await cursor.fetchall()]

async def replace_track_file(track_id: int, new_file_path: str, bitrate: int, duration: int, new_source_url: str = None) -> str | None:
    async with get_db() as conn:
        async with conn.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,)) as cursor:
            row = await cursor.fetchone()
            old_path = row["file_path"] if row else None
            
        if new_source_url:
            await conn.execute(
                "UPDATE tracks SET file_path = ?, bitrate = ?, duration = ?, status = 'pending', source_url = ?, file_id = NULL WHERE id = ?",
                (new_file_path, bitrate, duration, new_source_url, track_id)
            )
        else:
            await conn.execute(
                "UPDATE tracks SET file_path = ?, bitrate = ?, duration = ?, status = 'pending', file_id = NULL WHERE id = ?",
                (new_file_path, bitrate, duration, track_id)
            )
        await conn.commit()
        return old_path

async def get_user_stats(user_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        async with conn.execute("SELECT COUNT(*) as c FROM likes WHERE user_id = ?", (user_id,)) as cur:
            likes = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COUNT(*) as c FROM user_playlists WHERE user_id = ?", (user_id,)) as cur:
            playlists = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COUNT(*) as c FROM tracks WHERE uploaded_by = ? AND status = 'approved'", (user_id,)) as cur:
            uploaded = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COUNT(*) as c FROM transactions WHERE user_id = ? AND reason = 'import_track'", (user_id,)) as cur:
            downloaded = (await cur.fetchone())["c"]

        return {"likes": likes, "playlists": playlists, "uploaded": uploaded, "downloaded": downloaded}

async def report_track(track_id: int) -> bool:
    async with get_db() as conn:
        await conn.execute("UPDATE tracks SET complaint_count = complaint_count + 1 WHERE id = ?", (track_id,))
        await conn.commit()
        return True

async def remove_track_from_playlist(
    playlist_id: int, track_id: int, *, conn: aiosqlite.Connection | None = None
) -> bool:
    if conn is None:
        async with get_db() as conn:
            await remove_track_from_playlist(playlist_id, track_id, conn=conn)
            await conn.commit()
            return True

    await conn.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)
    )
    return True

async def get_user_playlist_by_title(user_id: int, title: str) -> dict[str, Any] | None:
    async with get_db() as conn:
        async with conn.execute("SELECT id FROM user_playlists WHERE user_id = ? AND title = ? ORDER BY id DESC LIMIT 1", (user_id, title)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def set_playlist_trailer(playlist_id: int, user_id: int, trailer_path: str) -> str | None:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            """SELECT trailer_path FROM user_playlists
               WHERE id = ? AND user_id = ? AND is_system = 0 AND original_playlist_id IS NULL""",
            (playlist_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise PermissionError("Трейлер может менять только автор плейлиста.")
        await conn.execute("UPDATE user_playlists SET trailer_path = ? WHERE id = ?", (trailer_path, playlist_id))
        await conn.execute(
            """INSERT INTO playlist_market_previews (playlist_id, mode) VALUES (?, 'trailer')
               ON CONFLICT(playlist_id) DO UPDATE SET mode = excluded.mode""",
            (playlist_id,),
        )
        await conn.commit()
        return row["trailer_path"]

async def set_playlist_custom_cover(playlist_id: int, cover_path: str) -> bool:
    async with get_db() as conn:
        await conn.execute("UPDATE user_playlists SET custom_cover = ? WHERE id = ?", (cover_path, playlist_id))
        await conn.commit()
        return True

async def get_similar_artists(artist_name: str, limit: int = 5) -> list[str]:
    async with get_db() as conn:
        query = """
            SELECT t2.artist, COUNT(*) as freq
            FROM tracks t1
            JOIN playlist_tracks pt1 ON t1.id = pt1.track_id
            JOIN playlist_tracks pt2 ON pt1.playlist_id = pt2.playlist_id
            JOIN tracks t2 ON pt2.track_id = t2.id
            WHERE LOWER(t1.artist) LIKE LOWER(?) 
              AND LOWER(t2.artist) NOT LIKE LOWER(?)
              AND t1.status = 'approved' AND t2.status = 'approved'
            GROUP BY t2.artist
            ORDER BY freq DESC
            LIMIT ?
        """
        async with conn.execute(query, (f"%{artist_name}%", f"%{artist_name}%", limit)) as cursor:
            rows = await cursor.fetchall()
            return [row["artist"] for row in rows]

async def get_trending_tracks(limit: int = 5) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM tracks WHERE status = 'approved' ORDER BY download_count DESC LIMIT ?", 
            (limit,)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

def artist_cover_key(name: str) -> str:
    return normalize_artist(name).casefold()


async def get_artist_cover(name: str) -> bytes | None:
    async with get_db() as conn:
        async with conn.execute("SELECT jpeg FROM artist_covers WHERE artist_key = ?", (artist_cover_key(name),)) as cursor:
            row = await cursor.fetchone()
            return row["jpeg"] if row else None


async def set_artist_cover(name: str, jpeg: bytes) -> bool:
    cover_key = artist_cover_key(name)
    async with get_db() as conn:
        key = metadata_key(name)
        await _bind_artist_page_filter(conn, key)
        async with conn.execute(f"SELECT 1 FROM tracks WHERE {ARTIST_PAGE_FILTER} LIMIT 1", (key,)) as cursor:
            found = await cursor.fetchone() is not None
        if not found:
            async with conn.execute("SELECT DISTINCT artist FROM tracks WHERE status = 'approved'") as cursor:
                found = any(artist_cover_key(row["artist"]) == cover_key for row in await cursor.fetchall())
        if not found:
            return False
        await conn.execute(
            """INSERT INTO artist_covers (artist_key, jpeg) VALUES (?, ?)
               ON CONFLICT(artist_key) DO UPDATE SET jpeg = excluded.jpeg""", (cover_key, jpeg),
        )
        await conn.commit()
        return True


TRACK_INTERACTIONS_SQL = """
    , track_likes AS (
        SELECT l.track_id, COUNT(DISTINCT l.user_id) AS total
        FROM likes l JOIN eligible_tracks t ON t.id = l.track_id
        GROUP BY l.track_id
    ), track_saves AS (
        SELECT pt.track_id, COUNT(DISTINCT p.user_id) AS total
        FROM eligible_tracks t
        JOIN playlist_tracks pt ON pt.track_id = t.id
        JOIN user_playlists p ON p.id = pt.playlist_id
        WHERE p.is_system = 0
        GROUP BY pt.track_id
    ), ranked_tracks AS (
        SELECT t.*, COALESCE(t.download_count, 0) + COALESCE(l.total, 0)
                      + COALESCE(s.total, 0) AS interactions
        FROM eligible_tracks t
        LEFT JOIN track_likes l ON l.track_id = t.id
        LEFT JOIN track_saves s ON s.track_id = t.id
    )
"""


async def _read_confirmed_artist_names(conn: aiosqlite.Connection) -> dict[str, str]:
    async with conn.execute("SELECT artist_key, artist_name FROM artist_catalog_bindings") as cursor:
        return {row["artist_key"]: row["artist_name"] for row in await cursor.fetchall()}


IMPORTED_ARTIST_CREDITS_SQL = """
    SELECT i.track_id, i.artist,
           CASE WHEN i.credited = 1 THEN a.artist_key END AS bound_key,
           CASE WHEN i.credited = 1 THEN a.artist_name END AS bound_name
    FROM artist_import_items i
    JOIN artist_import_batches b ON b.id = i.batch_id
    JOIN artist_catalog_bindings a ON a.artist_key = b.artist_key
    WHERE i.track_id IS NOT NULL AND i.state IN ('approved', 'existing')
"""

ARTIST_PAGE_FILTER = f"""status = 'approved' AND (on_artist_page(artist) OR id IN (
    SELECT track_id FROM ({IMPORTED_ARTIST_CREDITS_SQL}) WHERE bound_key = ? OR on_artist_page(artist)
))"""


async def _bind_artist_page_filter(conn: aiosqlite.Connection, key: str) -> dict[str, str]:
    names = await _read_confirmed_artist_names(conn)
    await conn.create_function(
        "on_artist_page", 1, lambda credit: matches_artist_page(credit, key, names), deterministic=True,
    )
    return names


async def _read_imported_artist_credits(
    conn: aiosqlite.Connection, track_ids: list[int] | None = None,
) -> dict[int, list[str]]:
    query = IMPORTED_ARTIST_CREDITS_SQL
    if track_ids is not None:
        if not track_ids:
            return {}
        placeholders = ", ".join("?" for _ in track_ids)
        query += f" AND i.track_id IN ({placeholders})"
    result: dict[int, list[str]] = {}
    async with conn.execute(query + " ORDER BY i.id", track_ids or []) as cursor:
        async for row in cursor:
            credits = result.setdefault(row["track_id"], [])
            credits.append(row["artist"])
            if row["bound_name"]:
                credits.append(row["bound_name"])
    return result


def _artist_page_names(credits: list[str], confirmed_names: dict[str, str]) -> list[str]:
    pages = {}
    for credit in credits:
        for name in split_artist_credits(credit, confirmed_names):
            key = metadata_key(name)
            pages.setdefault(key, confirmed_names.get(key, name))
    return list(pages.values())


async def get_track_artist_pages(tracks: list[dict]) -> dict[int, list[str]]:
    if not tracks:
        return {}
    async with get_db() as conn:
        names = await _read_confirmed_artist_names(conn)
        imported = await _read_imported_artist_credits(conn, [track["id"] for track in tracks])
    return {track["id"]: _artist_page_names([track["artist"], *imported.get(track["id"], [])], names)
            for track in tracks}


async def get_trending_artists(limit: int | None = 6) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute(
            """WITH eligible_tracks AS (
                   SELECT id, artist, download_count, created_at FROM tracks WHERE status = 'approved'
               )""" + TRACK_INTERACTIONS_SQL + """
               SELECT id, artist, COALESCE(download_count, 0) AS downloads, interactions
               FROM ranked_tracks ORDER BY created_at DESC, id DESC"""
        ) as cursor:
            all_tracks = await cursor.fetchall()
        async with conn.execute("SELECT artist_key FROM artist_covers") as cursor:
            custom_covers = {row["artist_key"] for row in await cursor.fetchall()}
        confirmed_names = await _read_confirmed_artist_names(conn)
        imported = await _read_imported_artist_credits(conn)

    unnamed = {"", "unknown", "unknown artist", "неизвестно", "неизвестный исполнитель", "various artists", "разные исполнители"}
    artist_stats = {}
    for t in all_tracks:
        for name in _artist_page_names([t["artist"], *imported.get(t["id"], [])], confirmed_names):
            key = metadata_key(name)
            if key in unnamed:
                continue
            if key not in artist_stats:
                artist_stats[key] = {
                    "artist": name,
                    "total_downloads": 0,
                    "interactions": 0,
                    "track_count": 0,
                    "latest_track_id": t["id"],
                    "has_custom_cover": artist_cover_key(name) in custom_covers,
                }
            stats = artist_stats[key]
            stats["total_downloads"] += t["downloads"]
            stats["interactions"] += t["interactions"]
            stats["track_count"] += 1

    sorted_artists = sorted(
        artist_stats.values(),
        key=lambda artist: (-artist["interactions"], -artist["track_count"], artist["artist"].casefold()),
    )
    return sorted_artists[:limit]

async def get_artist_tracks(artist_name: str, limit: int = 20, offset: int = 0) -> tuple[list[dict], dict]:
    key = metadata_key(artist_name)
    async with get_db() as conn:
        await conn.execute("BEGIN")
        confirmed_names = await _bind_artist_page_filter(conn, key)
        async with conn.execute(
            f"""SELECT COUNT(*) OVER () AS total, id
                FROM tracks WHERE {ARTIST_PAGE_FILTER} ORDER BY created_at DESC, id DESC LIMIT 1""", (key,)
        ) as cursor:
            latest = await cursor.fetchone()
        async with conn.execute("SELECT 1 FROM artist_covers WHERE artist_key = ?",
                                (artist_cover_key(artist_name),)) as cursor:
            has_custom_cover = await cursor.fetchone() is not None
        async with conn.execute(
            f"""WITH eligible_tracks AS (
                    SELECT id, artist, title, quality_status, download_count, created_at
                    FROM tracks WHERE {ARTIST_PAGE_FILTER}
                )""" + TRACK_INTERACTIONS_SQL + """
                SELECT id, artist, title, quality_status FROM ranked_tracks
                ORDER BY interactions DESC, created_at DESC, id DESC LIMIT ? OFFSET ?""",
            (key, limit, offset),
        ) as cursor:
            tracks = [dict(row) for row in await cursor.fetchall()]
    return tracks, {
        "artist": confirmed_names.get(key, artist_name.strip()),
        "latest_track_id": latest["id"] if latest else None,
        "has_custom_cover": has_custom_cover,
        "track_count": latest["total"] if latest else 0,
    }


async def get_artist_albums(artist_name: str) -> list[dict]:
    async with get_db() as conn:
        async with conn.execute(
            """SELECT a.album_id, a.title, a.year, a.release_type, a.cover IS NOT NULL AS has_cover,
                      COUNT(e.position) AS track_count, COUNT(t.id) AS available_count
               FROM artist_catalog_albums a
               LEFT JOIN artist_catalog_tracks e ON e.artist_key = a.artist_key AND e.album_id = a.album_id
               LEFT JOIN tracks t ON t.id = e.track_id AND t.status = 'approved'
               WHERE a.artist_key = ?
               GROUP BY a.artist_key, a.album_id
               ORDER BY a.year DESC, a.title COLLATE NOCASE, a.album_id""",
            (metadata_key(artist_name),),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_artist_album(artist_name: str, album_id: str) -> dict | None:
    params = (metadata_key(artist_name), album_id)
    async with get_db() as conn:
        await conn.execute("BEGIN")
        async with conn.execute(
            """SELECT album_id, title, year, release_type, cover IS NOT NULL AS has_cover
               FROM artist_catalog_albums WHERE artist_key = ? AND album_id = ?""", params
        ) as cursor:
            album = await cursor.fetchone()
        if album is None:
            return None
        async with conn.execute(
            """SELECT e.disc, e.position, e.title, e.artist, e.duration_ms,
                      t.id, t.title AS local_title, t.artist AS local_artist, t.quality_status
               FROM artist_catalog_tracks e
               LEFT JOIN tracks t ON t.id = e.track_id AND t.status = 'approved'
               WHERE e.artist_key = ? AND e.album_id = ?
               ORDER BY e.disc, e.position""", params
        ) as cursor:
            return dict(album, tracks=[dict(row) for row in await cursor.fetchall()])


async def get_artist_album_cover(artist_name: str, album_id: str) -> tuple[bytes, str] | None:
    async with get_db() as conn:
        async with conn.execute(
            """SELECT cover, cover_mime FROM artist_catalog_albums
               WHERE artist_key = ? AND album_id = ? AND cover IS NOT NULL""",
            (metadata_key(artist_name), album_id),
        ) as cursor:
            row = await cursor.fetchone()
            return (row["cover"], row["cover_mime"]) if row else None

async def set_track_file_id(track_id: int, file_id: str) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE tracks SET file_id = ? WHERE id = ?", (file_id, track_id))
        await conn.commit()

async def get_tracks_without_file_id() -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM tracks WHERE status = 'approved' AND file_id IS NULL AND file_path IS NOT NULL AND file_path != ''"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def update_user_settings(user_id: int, active_theme: str = None, loop_playlist: bool = None) -> None:
    async with get_db() as conn:
        if active_theme is not None:
            await conn.execute("UPDATE users SET active_theme = ? WHERE id = ?", (active_theme, user_id))
        if loop_playlist is not None:
            await conn.execute("UPDATE users SET loop_playlist = ? WHERE id = ?", (1 if loop_playlist else 0, user_id))
        await conn.commit()

async def buy_theme(user_id: int, theme_name: str, price: int) -> dict:
    async with get_db() as conn:
        async with conn.execute("SELECT points, unlocked_themes FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return {"error": "Пользователь не найден"}
            
            points = row["points"]
            unlocked = json.loads(row["unlocked_themes"])
            
            if theme_name in unlocked:
                return {"error": "Тема уже куплена"}
                
            if points < price:
                return {"error": "Недостаточно поинтов"}
                
            unlocked.append(theme_name)
            new_unlocked_str = json.dumps(unlocked)
            
            await conn.execute("UPDATE users SET points = points - ?, unlocked_themes = ? WHERE id = ?", (price, new_unlocked_str, user_id))
            await conn.execute("INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)", (user_id, -price, f"buy_theme_{theme_name}"))
            await conn.commit()
            
            return {"ok": True, "msg": "Тема успешно куплена!", "unlocked": unlocked}


async def get_profile_decorations(user_id: int) -> tuple[set[str], dict[str, str]]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT decoration_id, slot, equipped FROM profile_decorations WHERE user_id = ?", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return {row["decoration_id"] for row in rows}, {
        row["slot"]: row["decoration_id"] for row in rows if row["equipped"]
    }


async def set_public_username(user_id: int, username: str | None) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
        await conn.commit()


async def change_profile_decoration(user_id: int, decoration_id: str, action: str) -> None:
    item = PROFILE_DECORATIONS[decoration_id]
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(
            "SELECT 1 FROM profile_decorations WHERE user_id = ? AND decoration_id = ?", (user_id, decoration_id)
        ) as cursor:
            owned = await cursor.fetchone() is not None
        if not owned:
            if action != "buy":
                raise ValueError("Это украшение ещё не куплено.")
            price = item["price"]
            cursor = await conn.execute(
                "UPDATE users SET points = points - ? WHERE id = ? AND points >= ?", (price, user_id, price)
            )
            if not cursor.rowcount:
                raise ValueError("Недостаточно поинтов.")
            await conn.execute(
                "INSERT INTO profile_decorations (user_id, decoration_id, slot) VALUES (?, ?, ?)",
                (user_id, decoration_id, item["slot"]),
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)",
                (user_id, -price, f"buy_decoration_{decoration_id}"),
            )
        if action == "unequip":
            await conn.execute(
                "UPDATE profile_decorations SET equipped = 0 WHERE user_id = ? AND decoration_id = ?",
                (user_id, decoration_id),
            )
        else:
            await conn.execute(
                "UPDATE profile_decorations SET equipped = 0 WHERE user_id = ? AND slot = ? AND equipped = 1",
                (user_id, item["slot"]),
            )
            await conn.execute(
                "UPDATE profile_decorations SET equipped = 1 WHERE user_id = ? AND decoration_id = ?",
                (user_id, decoration_id),
            )
        await conn.commit()

async def get_wave_recommendations(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute("""
            SELECT t.artist FROM likes l 
            JOIN tracks t ON l.track_id = t.id 
            WHERE l.user_id = ? AND t.status = 'approved'
        """, (user_id,)) as cursor:
            liked_artists = [row["artist"] for row in await cursor.fetchall()]
            
        async with conn.execute("""
            SELECT pt.track_id FROM playlist_tracks pt
            JOIN user_playlists p ON pt.playlist_id = p.id
            WHERE p.user_id = ?
        """, (user_id,)) as cursor:
            existing_tracks = [row["track_id"] for row in await cursor.fetchall()]

        recommended_tracks = []
        seen_ids = set(existing_tracks)
        
        if not liked_artists:
            async with conn.execute("SELECT * FROM tracks WHERE status = 'approved' ORDER BY download_count DESC LIMIT ?", (limit,)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

        unique_artists = list(set(liked_artists))[:5]
        for artist in unique_artists:
            async with conn.execute("""
                SELECT * FROM tracks 
                WHERE LOWER(artist) LIKE LOWER(?) AND status = 'approved'
                ORDER BY download_count DESC LIMIT 5
            """, (f"%{artist}%",)) as cursor:
                for row in await cursor.fetchall():
                    if row["id"] not in seen_ids:
                        recommended_tracks.append(dict(row))
                        seen_ids.add(row["id"])

        needed = limit - len(recommended_tracks)
        if needed > 0:
            async with conn.execute("""
                SELECT * FROM tracks 
                WHERE status = 'approved' 
                ORDER BY RANDOM() LIMIT ?
            """, (needed * 2,)) as cursor:
                for row in await cursor.fetchall():
                    if row["id"] not in seen_ids:
                        recommended_tracks.append(dict(row))
                        seen_ids.add(row["id"])
                    if len(recommended_tracks) >= limit:
                        break
        
        random.shuffle(recommended_tracks)
        return recommended_tracks[:limit]

async def get_admin_stats() -> dict:
    async with get_db() as conn:
        stats = {}
        
        # 1. Всего пользователей
        async with conn.execute("SELECT COUNT(*) FROM users") as c:
            stats["total_users"] = (await c.fetchone())[0]
            
        # 2. Треки в базе (approved)
        async with conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'approved'") as c:
            stats["total_tracks"] = (await c.fetchone())[0]
            
        # 3. Треки на модерации (pending)
        async with conn.execute("SELECT COUNT(*) FROM tracks WHERE status = 'pending'") as c:
            stats["pending_tracks"] = (await c.fetchone())[0]
            
        # 4. Пользователи за сутки (DAU)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with conn.execute("SELECT COUNT(*) FROM users WHERE last_download_date = ? OR created_at >= datetime('now', '-1 day')", (today,)) as c:
            stats["dau"] = (await c.fetchone())[0]
            
        # 5. Популярные источники (import_source)
        await conn.create_function("local_file_exists", 1, lambda path: bool(path) and os.path.isfile(path))
        async with conn.execute(
            """SELECT COALESCE(NULLIF(import_source, ''), 'unknown') AS source, COUNT(*) AS total,
                      SUM(CASE WHEN status = 'approved' AND local_file_exists(file_path)
                               AND COALESCE(tracks.source, '') <> 'failed_download' THEN 1 ELSE 0 END) AS ready,
                      SUM(CASE WHEN tracks.source = 'failed_download' THEN 1 ELSE 0 END) AS missing
               FROM tracks GROUP BY 1 ORDER BY total DESC"""
        ) as c:
            stats["sources"] = [
                {"source": row["source"], "count": row["total"], "ready": row["ready"], "missing": row["missing"]}
                for row in await c.fetchall()
            ]
            
        # 6. Премиум пользователи
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with conn.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1 AND premium_expires_at > ?", (now_str,)) as c:
            stats["premium_users"] = (await c.fetchone())[0]

        # 7. Популярные треки (топ 5 по скачиваниям)
        async with conn.execute("SELECT artist, title, download_count FROM tracks WHERE status = 'approved' ORDER BY download_count DESC LIMIT 5") as c:
            stats["top_tracks"] = [{"artist": row[0], "title": row[1], "downloads": row[2]} for row in await c.fetchall()]

        return stats

# ============================================================
# ФУНКЦИИ ДЛЯ ОФФЛАЙН-ПЛЕЙЛИСТОВ (ТЕМЫ В ЛС)
# ============================================================
async def get_or_create_telegram_chat(user_id: int, tg_chat_id: int) -> dict | None:
    async with get_db() as conn:
        await conn.execute(f"""
            INSERT INTO telegram_chats (user_id, kind, tg_chat_id)
            SELECT u.id, 'dm', ? FROM users u WHERE u.id = ? AND {ACTIVE_PREMIUM_SQL}
            ON CONFLICT(user_id, kind) DO NOTHING
        """, (tg_chat_id, user_id))
        await conn.commit()
        async with conn.execute("SELECT * FROM telegram_chats WHERE user_id = ? AND kind = 'dm'", (user_id,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def create_telegram_topic(chat_id: int, playlist_id: int, title: str) -> bool:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(f"""
            SELECT 1 FROM telegram_chats c JOIN users u ON u.id = c.user_id
            WHERE c.id = ? AND c.state = 'active' AND {ACTIVE_PREMIUM_SQL}
        """, (chat_id,)) as cursor:
            if await cursor.fetchone() is None:
                return False
        await conn.execute("""
            INSERT INTO telegram_topics (chat_id, playlist_id, title, state) VALUES (?, ?, ?, 'new')
            ON CONFLICT(chat_id, playlist_id) DO NOTHING
        """, (chat_id, playlist_id, title))
        await conn.commit()
        return True

class TopicClaimLost(RuntimeError):
    pass


async def claim_topic_purges(limit: int) -> list[dict]:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute("""
            SELECT t.id, t.thread_id, t.stale_thread_id, c.tg_chat_id
            FROM telegram_topics t JOIN telegram_chats c ON c.id = t.chat_id
            WHERE t.state = 'purge' AND COALESCE(datetime(t.run_after), CURRENT_TIMESTAMP) <= CURRENT_TIMESTAMP
            ORDER BY t.run_after, t.id LIMIT ?
        """, (limit,)) as cursor:
            claims = [dict(row, sync_token=uuid4().hex) for row in await cursor.fetchall()]
        await conn.executemany("""
            UPDATE telegram_topics SET sync_token = ?, run_after = datetime('now', ?)
            WHERE id = ?
        """, ((claim["sync_token"], f"+{TOPIC_LEASE_SECONDS} seconds", claim["id"]) for claim in claims))
        await conn.commit()
        return claims


async def finish_topic_purge(topic_id: int, sync_token: str) -> None:
    async with get_db() as conn:
        await conn.execute("""
            DELETE FROM telegram_topics WHERE id = ? AND state = 'purge' AND sync_token = ?
        """, (topic_id, sync_token))
        await conn.commit()


async def retry_topic_purge(topic_id: int, sync_token: str, delay: int, error: str) -> None:
    async with get_db() as conn:
        await conn.execute("""
            UPDATE telegram_topics SET sync_token = NULL, run_after = datetime('now', ?),
                attempts = COALESCE(attempts, 0) + 1, last_error = ?
            WHERE id = ? AND state = 'purge' AND sync_token = ?
        """, (f"+{delay} seconds", error, topic_id, sync_token))
        await conn.commit()


async def claim_topics(limit: int = 5) -> list[dict]:
    async with get_db() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        async with conn.execute(f"""
            SELECT t.id, t.state
            FROM telegram_topics t
            JOIN telegram_chats c ON t.chat_id = c.id
            JOIN users u ON u.id = c.user_id
            WHERE c.state = 'active'
              AND {ACTIVE_PREMIUM_SQL}
              AND t.state <> 'purge'
              AND (t.run_after IS NULL OR t.run_after <= CURRENT_TIMESTAMP)
              AND (t.state <> 'syncing' OR t.sync_token IS NULL OR t.updated_at IS NULL
                   OR t.updated_at <= datetime('now', ?))
              AND (t.rev > t.synced_rev OR t.state IN ('syncing', 'retry')
                   OR (t.playlist_id IS NULL AND EXISTS (
                       SELECT 1 FROM telegram_topic_tracks m WHERE m.topic_id = t.id
                   )))
            ORDER BY t.run_after, t.id
            LIMIT ?
        """, (f"-{TOPIC_LEASE_SECONDS} seconds", limit)) as cursor:
            claims = [dict(row, sync_token=uuid4().hex) for row in await cursor.fetchall()]
        await conn.executemany("""
            UPDATE telegram_topics SET state = 'syncing', sync_token = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, ((claim["sync_token"], claim["id"]) for claim in claims))
        await conn.commit()
        return claims


async def get_topic_sync_snapshot(topic_id: int, sync_token: str) -> tuple[dict, list[dict], list[dict]]:
    async with get_db() as conn:
        # Ревизия, треки и привязки должны быть прочитаны из одного снимка SQLite.
        await conn.execute("BEGIN")
        async with conn.execute(f"""
            SELECT t.*, c.tg_chat_id FROM telegram_topics t
            JOIN telegram_chats c ON c.id = t.chat_id
            JOIN users u ON u.id = c.user_id
            WHERE t.id = ? AND t.state = 'syncing' AND t.sync_token = ?
              AND c.state = 'active' AND {ACTIVE_PREMIUM_SQL}
        """, (topic_id, sync_token)) as cursor:
            topic = await cursor.fetchone()
        if topic is None:
            raise TopicClaimLost(topic_id)

        async with conn.execute("""
            SELECT t.id, t.artist, t.title, t.duration, t.file_id, t.file_path, COALESCE(pt.position, 0) AS position
            FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id
            WHERE pt.playlist_id = ? AND t.status = 'approved'
            ORDER BY pt.position ASC, pt.added_at DESC, pt.id DESC
        """, (topic["playlist_id"],)) as cursor:
            tracks = {}
            for row in await cursor.fetchall():
                tracks.setdefault(row["id"], dict(row))
        async with conn.execute("""
            SELECT track_id, message_id, position FROM telegram_topic_tracks WHERE topic_id = ?
        """, (topic_id,)) as cursor:
            messages = [dict(row) for row in await cursor.fetchall()]
        return dict(topic), list(tracks.values()), messages


async def _refresh_topic_claim(conn: aiosqlite.Connection, topic_id: int, sync_token: str) -> None:
    async with conn.execute(f"""
        UPDATE telegram_topics SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND state = 'syncing' AND sync_token = ?
          AND EXISTS (
              SELECT 1 FROM telegram_chats c JOIN users u ON u.id = c.user_id
              WHERE c.id = telegram_topics.chat_id AND c.state = 'active' AND {ACTIVE_PREMIUM_SQL}
          )
    """, (topic_id, sync_token)) as cursor:
        if cursor.rowcount != 1:
            raise TopicClaimLost(topic_id)


async def renew_topic_claim(topic_id: int, sync_token: str) -> None:
    async with get_db() as conn:
        await _refresh_topic_claim(conn, topic_id, sync_token)
        await conn.commit()


async def save_topic_moves(topic_id: int, sync_token: str, rev: int, moves: set[int]) -> None:
    async with get_db() as conn:
        await _refresh_topic_claim(conn, topic_id, sync_token)
        await conn.execute("UPDATE telegram_topics SET move_plan = ? WHERE id = ?", (
            json.dumps({"rev": rev, "tracks": sorted(moves)}), topic_id,
        ))
        await conn.commit()


async def finish_topic(
    topic_id: int, sync_token: str, synced_rev: int, positions: list[tuple[int, int]], *, warning: str | None = None
) -> None:
    async with get_db() as conn:
        await _refresh_topic_claim(conn, topic_id, sync_token)
        await conn.executemany("""
            UPDATE telegram_topic_tracks SET position = ? WHERE topic_id = ? AND track_id = ?
        """, ((position, topic_id, track_id) for track_id, position in positions))
        await conn.execute("""
            UPDATE telegram_topics
            SET state = 'ready', sync_token = NULL, move_plan = NULL, synced_rev = ?,
                last_synced_at = CURRENT_TIMESTAMP, attempts = 0, last_error = ?
            WHERE id = ?
        """, (synced_rev, warning, topic_id))
        await conn.commit()


async def retry_topic(topic_id: int, sync_token: str, error: str, delay: int = 30) -> None:
    async with get_db() as conn:
        await conn.execute("""
            UPDATE telegram_topics
            SET state = 'retry', sync_token = NULL, updated_at = CURRENT_TIMESTAMP,
                run_after = MAX(COALESCE(run_after, CURRENT_TIMESTAMP), datetime('now', ?)),
                attempts = COALESCE(attempts, 0) + 1, last_error = ?
            WHERE id = ? AND state = 'syncing' AND sync_token = ?
        """, (f"+{delay} seconds", error, topic_id, sync_token))
        await conn.commit()


async def set_topic_thread_id(topic_id: int, sync_token: str, thread_id: int) -> None:
    async with get_db() as conn:
        await _refresh_topic_claim(conn, topic_id, sync_token)
        await conn.execute("UPDATE telegram_topics SET thread_id = ? WHERE id = ?", (thread_id, topic_id))
        await conn.commit()


async def add_topic_track(
    topic_id: int, sync_token: str, track_id: int, message_id: int, position: int, file_id: str | None
) -> None:
    async with get_db() as conn:
        await _refresh_topic_claim(conn, topic_id, sync_token)
        await conn.execute("""
            INSERT INTO telegram_topic_tracks (topic_id, track_id, message_id, position) VALUES (?, ?, ?, ?)
        """, (topic_id, track_id, message_id, position))
        if file_id:
            await conn.execute("UPDATE tracks SET file_id = ? WHERE id = ? AND file_id IS NULL", (file_id, track_id))
        await conn.commit()


async def drop_topic_track(topic_id: int, sync_token: str, track_id: int, message_id: int) -> None:
    async with get_db() as conn:
        await _refresh_topic_claim(conn, topic_id, sync_token)
        await conn.execute("""
            DELETE FROM telegram_topic_tracks WHERE topic_id = ? AND track_id = ? AND message_id = ?
        """, (topic_id, track_id, message_id))
        await conn.commit()
