"""Асинхронный слой работы с SQLite для FreeDom Music Bot."""
from __future__ import annotations

import hashlib
import os
import secrets
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Any
from contextlib import asynccontextmanager
import aiosqlite

from backend.config import ADMIN_ID

DB_DIR: str = os.path.dirname(os.path.abspath(__file__))
DB_PATH: str = os.path.join(DB_DIR, "music_bot.db")

LEVEL_THRESHOLDS = ((3000, "Меценат"), (1501, "Легенда"), (601, "Куратор"), (201, "Коллекционер"), (0, "Новичок"))

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
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys = ON")
    try: yield conn
    finally: await conn.close()

async def init_db() -> None:
    async with get_db() as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL, username TEXT,
                points INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, level TEXT DEFAULT 'Новичок',
                referrals_count INTEGER DEFAULT 0, downloads_today INTEGER DEFAULT 0,
                last_download_date DATE, is_premium BOOLEAN DEFAULT 0, premium_expires_at TIMESTAMP,
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
            CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, title, artist) VALUES('delete', old.id, old.title, old.artist);
                INSERT INTO tracks_fts(rowid, title, artist) VALUES (new.id, new.title, new.artist);
            END;
            """
        )
        
        await conn.execute("DELETE FROM playlist_tracks WHERE track_id NOT IN (SELECT id FROM tracks)")
        await conn.execute("DELETE FROM likes WHERE track_id NOT IN (SELECT id FROM tracks)")
        await conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")
        
        async with conn.execute("PRAGMA table_info(users)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "daily_download_limit" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN daily_download_limit INTEGER DEFAULT 3")
            if "xp" not in cols:
                await conn.execute("ALTER TABLE users ADD COLUMN xp INTEGER DEFAULT 0")
                await conn.execute("UPDATE users SET xp = points")
            # НОВЫЕ КОЛОНКИ:
            if "active_theme" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN active_theme TEXT DEFAULT 'dark'")
            if "unlocked_themes" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN unlocked_themes TEXT DEFAULT '[\"dark\"]'")
            if "loop_playlist" not in cols: await conn.execute("ALTER TABLE users ADD COLUMN loop_playlist BOOLEAN DEFAULT 0")
                
        async with conn.execute("PRAGMA table_info(tracks)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "original_duration" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN original_duration INTEGER DEFAULT 0")
            if "import_source" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN import_source TEXT DEFAULT 'unknown'")
            if "pending_playlist_id" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN pending_playlist_id INTEGER DEFAULT NULL")
            if "pending_position" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN pending_position INTEGER DEFAULT 0")
            if "source_url" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN source_url TEXT DEFAULT NULL")
            if "file_id" not in cols: await conn.execute("ALTER TABLE tracks ADD COLUMN file_id TEXT DEFAULT NULL")

        async with conn.execute("PRAGMA table_info(playlist_tracks)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "position" not in cols: await conn.execute("ALTER TABLE playlist_tracks ADD COLUMN position INTEGER DEFAULT 0")
            
        async with conn.execute("PRAGMA table_info(user_playlists)") as cursor:
            cols = [r[1] for r in await cursor.fetchall()]
            if "price" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN price INTEGER DEFAULT 0")
            if "sales_count" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN sales_count INTEGER DEFAULT 0")
            if "original_playlist_id" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN original_playlist_id INTEGER DEFAULT NULL")
            if "trailer_path" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN trailer_path TEXT DEFAULT NULL")
            if "custom_cover" not in cols: await conn.execute("ALTER TABLE user_playlists ADD COLUMN custom_cover TEXT DEFAULT NULL")
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

async def upload_track(user_id: int, title: str, artist: str, file_path: str, source: str, bitrate: int = 0, duration: int = 0, original_duration: int = 0, import_source: str = "unknown", pending_playlist_id: int = None, pending_position: int = 0, source_url: str = None) -> dict[str, Any]:
    file_hash = _compute_file_hash(file_path, title, artist, user_id)
    async with get_db() as conn:
        async with conn.execute("SELECT id FROM tracks WHERE LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?)", (artist, title)) as cursor:
            if await cursor.fetchone(): raise ValueError(f"Трек '{artist} — {title}' уже есть в базе!")
        try:
            cursor = await conn.execute(
                """INSERT INTO tracks (title, artist, file_path, file_hash, source, source_url, uploaded_by, bitrate, duration, original_duration, import_source, status, pending_playlist_id, pending_position) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (title, artist, file_path, file_hash, source, source_url, user_id, bitrate, duration, original_duration, import_source, pending_playlist_id, pending_position)
            )
            track_id = cursor.lastrowid
            await conn.commit()
            async with conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)) as c:
                return dict(await c.fetchone())
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
        await conn.execute("UPDATE users SET daily_download_limit = ?, downloads_today = 0 WHERE id = ?", (new_limit, user_id))
        await conn.commit()

async def check_download_limit(user_id: int, is_premium: bool) -> dict[str, Any]:
    if is_premium: return {"allowed": True, "remaining": 9999, "limit": 9999, "used": 0}
    async with get_db() as conn:
        async with conn.execute("SELECT telegram_id, downloads_today, last_download_date, daily_download_limit, level FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return {"allowed": False, "msg": "Пользователь не найден"}
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            downloads, last_date = row["downloads_today"] or 0, row["last_download_date"]
            
            level_limits = {"Новичок": 3, "Коллекционер": 5, "Куратор": 10, "Легенда": 20, "Меценат": 50}
            limit_from_level = level_limits.get(row["level"], 3)
            
            if row["telegram_id"] == ADMIN_ID:
                limit = row["daily_download_limit"] if row["daily_download_limit"] is not None else 3
            else:
                limit = max(limit_from_level, row["daily_download_limit"] or 0)
            
            if last_date != today:
                downloads = 0
                await conn.execute("UPDATE users SET downloads_today = 0, last_download_date = ? WHERE id = ?", (today, user_id))
                await conn.commit()
            if downloads >= limit: return {"allowed": False, "msg": f"Лимит бесплатных скачиваний на сегодня исчерпан ({limit}/{limit}). Повысь уровень или купи Premium!", "limit": limit, "used": downloads}
            return {"allowed": True, "remaining": limit - downloads, "limit": limit, "used": downloads}

async def increment_download_count(user_id: int, count: int) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE users SET downloads_today = downloads_today + ? WHERE id = ?", (count, user_id))
        await conn.commit()

async def check_track_exists(artist: str, title: str) -> int | None:
    def clean(s):
        if not s: return ""
        s = str(s).lower().replace('\xa0', ' ')
        s = re.sub(r'[^\w\s]', '', s)
        return " ".join(s.split())
    a_clean, t_clean = clean(artist), clean(title)
    async with get_db() as conn:
        async with conn.execute("SELECT id, artist, title, file_path FROM tracks") as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            db_a, db_t = clean(row["artist"]), clean(row["title"])
            a_match = (a_clean in db_a) or (db_a in a_clean)
            t_match = (t_clean in db_t) or (db_t in t_clean)
            if a_match and t_match:
                file_path = row["file_path"]
                if file_path and os.path.isfile(file_path): 
                    return row["id"] 
                else:
                    await conn.execute("DELETE FROM tracks WHERE id = ?", (row["id"],))
                    await conn.commit()
                    return None 
    return None

async def get_pending_tracks(limit: int = 10) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM tracks WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def update_track_status(track_id: int, new_status: str, admin_id: int) -> None:
    async with get_db() as conn:
        async with conn.execute("SELECT bitrate, pending_playlist_id, pending_position FROM tracks WHERE id = ?", (track_id,)) as cursor:
            row = await cursor.fetchone()
            bitrate = row["bitrate"] if row else 0
            
        quality = 'none'
        if new_status == 'approved':
            quality = 'Verified+' if bitrate and bitrate >= 320 else 'Verified'
            
            if row and row["pending_playlist_id"]:
                async with conn.execute("SELECT id FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (row["pending_playlist_id"], track_id)) as cursor:
                    if not await cursor.fetchone():
                        await conn.execute(
                            "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                            (row["pending_playlist_id"], track_id, row["pending_position"])
                        )

        await conn.execute("UPDATE tracks SET status = ?, verified_by = ?, quality_status = ? WHERE id = ?", (new_status, admin_id, quality, track_id))
        await conn.commit()

async def search_tracks(query: str, limit: int = 10) -> list[dict[str, Any]]:
    safe_query = re.sub(r'[^\w\s]', ' ', query).strip()
    if not safe_query: return []
    search_str = " AND ".join([f"{word}*" for word in safe_query.split()])
    async with get_db() as conn:
        try:
            sql = "SELECT t.* FROM tracks t JOIN tracks_fts fts ON t.id = fts.rowid WHERE tracks_fts MATCH ? AND t.status = 'approved' ORDER BY t.download_count DESC LIMIT ?"
            async with conn.execute(sql, (search_str, limit)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        except Exception as e:
            print(f"[DB] Ошибка поиска FTS5: {e}")
            return []

async def get_track_by_id(track_id: int) -> dict[str, Any] | None:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)) as cursor:
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

async def give_premium(user_id: int, days: int) -> None:
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    expires_str = expires.strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as conn:
        await conn.execute("UPDATE users SET is_premium = 1, premium_expires_at = ? WHERE id = ?", (expires_str, user_id))
        await conn.commit()

async def remove_premium(user_id: int) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE users SET is_premium = 0, premium_expires_at = NULL WHERE id = ?", (user_id,))
        await conn.commit()

async def update_track_metadata(track_id: int, bitrate: int, duration: int) -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE tracks SET bitrate = ?, duration = ? WHERE id = ?", (bitrate, duration, track_id))
        await conn.commit()

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
        await conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        await conn.commit()
        return True

async def get_hunt_tracks(limit: int = 30) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute("SELECT id, title, artist, complaint_count, status FROM tracks WHERE status = 'pending' AND complaint_count > 0 ORDER BY complaint_count DESC LIMIT ?", (limit,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def create_user_playlist(user_id: int, title: str) -> dict[str, Any]:
    async with get_db() as conn:
        cursor = await conn.execute("INSERT INTO user_playlists (user_id, title) VALUES (?, ?)", (user_id, title))
        await conn.commit()
        return {"id": cursor.lastrowid, "title": title, "status": "created"}

async def get_user_playlists(user_id: int, exclude_track_id: int = None) -> list[dict[str, Any]]:
    async with get_db() as conn:
        if exclude_track_id:
            query = """
                SELECT p.id, p.title, p.price, p.is_public, p.sales_count, p.original_playlist_id, p.trailer_path, p.custom_cover, COUNT(pt.track_id) as track_count, orig_u.username as original_seller_username, orig_u.telegram_id as original_seller_tg
                FROM user_playlists p 
                LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id 
                LEFT JOIN user_playlists orig_p ON p.original_playlist_id = orig_p.id 
                LEFT JOIN users orig_u ON orig_p.user_id = orig_u.id
                WHERE p.user_id = ? AND p.id NOT IN (SELECT playlist_id FROM playlist_tracks WHERE track_id = ?)
                GROUP BY p.id
            """
            async with conn.execute(query, (user_id, exclude_track_id)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        else:
            query = """
                SELECT p.id, p.title, p.price, p.is_public, p.sales_count, p.original_playlist_id, p.trailer_path, p.custom_cover, COUNT(pt.track_id) as track_count, orig_u.username as original_seller_username, orig_u.telegram_id as original_seller_tg
                FROM user_playlists p 
                LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id 
                LEFT JOIN user_playlists orig_p ON p.original_playlist_id = orig_p.id 
                LEFT JOIN users orig_u ON orig_p.user_id = orig_u.id
                WHERE p.user_id = ?
                GROUP BY p.id
            """
            async with conn.execute(query, (user_id,)) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

async def get_playlist_by_id(playlist_id: int) -> dict[str, Any] | None:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM user_playlists WHERE id = ?", (playlist_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def add_track_to_playlist(playlist_id: int, track_id: int, position: int = 0) -> bool:
    async with get_db() as conn:
        async with conn.execute("SELECT id FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id)) as cursor:
            row = await cursor.fetchone()
            
        if row:
            await conn.execute("UPDATE playlist_tracks SET position = ? WHERE playlist_id = ? AND track_id = ?", (position, playlist_id, track_id))
        else:
            await conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)", (playlist_id, track_id, position))
            
        await conn.commit()
        return True

async def get_playlist_tracks(playlist_id: int) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute(
            """SELECT t.id, t.artist, t.title, t.quality_status 
               FROM playlist_tracks pt 
               JOIN tracks t ON pt.track_id = t.id 
               WHERE pt.playlist_id = ? 
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
            """SELECT p.id, p.title, p.price, p.sales_count, COUNT(pt.track_id) as track_count, u.username, u.telegram_id as seller_tg
               FROM user_playlists p JOIN users u ON p.user_id = u.id LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id
               WHERE p.is_public = 1 AND p.price > 0 GROUP BY p.id ORDER BY p.price ASC""") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def buy_playlist(buyer_id: int, playlist_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        async with conn.execute("SELECT id FROM user_playlists WHERE user_id = ? AND original_playlist_id = ?", (buyer_id, playlist_id)) as cursor:
            if await cursor.fetchone(): return {"ok": False, "msg": "Вы уже покупали этот плейлист!"}
        async with conn.execute("SELECT * FROM user_playlists WHERE id = ?", (playlist_id,)) as cursor:
            pl = await cursor.fetchone()
            if not pl or not pl["is_public"] or pl["price"] <= 0: return {"ok": False, "msg": "Плейлист не продается"}
            if pl["user_id"] == buyer_id: return {"ok": False, "msg": "Нельзя купить свой плейлист"}
            price, seller_id, title = pl["price"], pl["user_id"], pl["title"]
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

async def toggle_like(user_id: int, track_id: int) -> bool:
    async with get_db() as conn:
        async with conn.execute("SELECT 1 FROM likes WHERE user_id = ? AND track_id = ?", (user_id, track_id)) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await conn.execute("DELETE FROM likes WHERE user_id = ? AND track_id = ?", (user_id, track_id))
            await conn.commit()
            return False
        else:
            await conn.execute("INSERT INTO likes (user_id, track_id) VALUES (?, ?)", (user_id, track_id))
            await conn.commit()
            return True

async def get_user_liked_tracks(user_id: int) -> list[dict[str, Any]]:
    async with get_db() as conn:
        async with conn.execute("""SELECT t.id, t.artist, t.title, t.quality_status FROM likes l JOIN tracks t ON l.track_id = t.id WHERE l.user_id = ? ORDER BY l.created_at DESC""", (user_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_user_liked_track_ids(user_id: int) -> list[int]:
    async with get_db() as conn:
        async with conn.execute("SELECT track_id FROM likes WHERE user_id = ?", (user_id,)) as cursor:
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

async def remove_track_from_playlist(playlist_id: int, track_id: int) -> bool:
    async with get_db() as conn:
        await conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
        await conn.commit()
        return True

async def get_user_playlist_by_title(user_id: int, title: str) -> dict[str, Any] | None:
    async with get_db() as conn:
        async with conn.execute("SELECT id FROM user_playlists WHERE user_id = ? AND title = ? ORDER BY id DESC LIMIT 1", (user_id, title)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def set_playlist_trailer(playlist_id: int, trailer_path: str) -> bool:
    """Привязывает путь к трейлеру для плейлиста."""
    async with get_db() as conn:
        await conn.execute("UPDATE user_playlists SET trailer_path = ? WHERE id = ?", (trailer_path, playlist_id))
        await conn.commit()
        return True

async def set_playlist_custom_cover(playlist_id: int, cover_path: str) -> bool:
    """Привязывает путь к кастомной обложке плейлиста."""
    async with get_db() as conn:
        await conn.execute("UPDATE user_playlists SET custom_cover = ? WHERE id = ?", (cover_path, playlist_id))
        await conn.commit()
        return True

async def get_similar_artists(artist_name: str, limit: int = 5) -> list[str]:
    """Ищет артистов, которые часто встречаются в одних плейлистах с заданным."""
    async with get_db() as conn:
        query = """
            SELECT t2.artist, COUNT(*) as freq
            FROM tracks t1
            JOIN playlist_tracks pt1 ON t1.id = pt1.track_id
            JOIN playlist_tracks pt2 ON pt1.playlist_id = pt2.playlist_id
            JOIN tracks t2 ON pt2.track_id = t2.id
            WHERE LOWER(t1.artist) LIKE LOWER(?) 
              AND LOWER(t2.artist) NOT LIKE LOWER(?)
            GROUP BY t2.artist
            ORDER BY freq DESC
            LIMIT ?
        """
        async with conn.execute(query, (f"%{artist_name}%", f"%{artist_name}%", limit)) as cursor:
            rows = await cursor.fetchall()
            return [row["artist"] for row in rows]

async def get_trending_tracks(limit: int = 5) -> list[dict[str, Any]]:
    """Возвращает самые скачиваемые треки для пустого поиска."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM tracks WHERE status = 'approved' ORDER BY download_count DESC LIMIT ?", 
            (limit,)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_trending_artists(limit: int = 6) -> list[dict[str, Any]]:
    """Возвращает топ артистов по количеству скачиваний их треков."""
    async with get_db() as conn:
        async with conn.execute(
            """SELECT artist, SUM(download_count) as total_downloads, MAX(id) as top_track_id 
               FROM tracks WHERE status = 'approved' 
               GROUP BY artist ORDER BY total_downloads DESC LIMIT ?""", 
            (limit,)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

async def get_tracks_by_artist(artist_name: str, limit: int = 20) -> list[dict[str, Any]]:
    """Возвращает треки конкретного артиста."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM tracks WHERE LOWER(artist) = LOWER(?) AND status = 'approved' ORDER BY download_count DESC LIMIT ?", 
            (artist_name, limit)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

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