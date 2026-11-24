"""Shared SQLite schema and artist identity for the WebApp and catalog CLI."""
import re
import unicodedata
from pathlib import Path


CATALOG_DB_PATH = Path(__file__).with_name("music_bot.db")

ARTIST_CATALOG_SCHEMA = """
    CREATE TABLE IF NOT EXISTS artist_catalog_bindings (
        artist_key TEXT PRIMARY KEY,
        artist_name TEXT NOT NULL,
        yandex_artist_id TEXT NOT NULL UNIQUE,
        yandex_artist_name TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        attempted_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS artist_catalog_albums (
        artist_key TEXT NOT NULL REFERENCES artist_catalog_bindings(artist_key) ON DELETE CASCADE,
        album_id TEXT NOT NULL,
        title TEXT NOT NULL,
        year INTEGER,
        release_type TEXT,
        cover_url TEXT,
        cover BLOB,
        cover_mime TEXT,
        PRIMARY KEY (artist_key, album_id)
    );
    CREATE TABLE IF NOT EXISTS artist_catalog_tracks (
        artist_key TEXT NOT NULL,
        album_id TEXT NOT NULL,
        disc INTEGER NOT NULL,
        position INTEGER NOT NULL,
        yandex_track_id TEXT NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL,
        duration_ms INTEGER,
        track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
        PRIMARY KEY (artist_key, album_id, disc, position),
        FOREIGN KEY (artist_key, album_id)
            REFERENCES artist_catalog_albums(artist_key, album_id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_artist_catalog_track_id ON artist_catalog_tracks(track_id);
    CREATE INDEX IF NOT EXISTS idx_artist_catalog_source_track ON artist_catalog_tracks(yandex_track_id);
    CREATE INDEX IF NOT EXISTS idx_artist_catalog_refresh ON artist_catalog_bindings(attempted_at, artist_key);
"""

ARTIST_IMPORT_ITEMS_TABLE = """
    CREATE TABLE IF NOT EXISTS artist_import_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES artist_import_batches(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        artist TEXT NOT NULL,
        title TEXT NOT NULL,
        duration_ms INTEGER,
        release_title TEXT,
        credited INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'queued',
        attempt INTEGER NOT NULL DEFAULT 0,
        lease_until INTEGER NOT NULL DEFAULT 0,
        hitmo_only INTEGER NOT NULL DEFAULT 0,
        file_path TEXT,
        audio_source TEXT,
        source_url TEXT,
        bitrate INTEGER,
        duration INTEGER,
        issue TEXT,
        track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
        album_request_id INTEGER REFERENCES album_import_requests(id),
        compare_existing INTEGER NOT NULL DEFAULT 0,
        reward INTEGER NOT NULL DEFAULT 0
    )
"""

ARTIST_IMPORT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS artist_import_batches (
        id INTEGER PRIMARY KEY,
        artist_key TEXT NOT NULL UNIQUE REFERENCES artist_catalog_bindings(artist_key) ON DELETE CASCADE,
        paused INTEGER NOT NULL DEFAULT 0
    );
""" + ARTIST_IMPORT_ITEMS_TABLE + ";"

ARTIST_IMPORT_INDEXES = (
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_artist_import_batch_recording
        ON artist_import_items(batch_id, source_id) WHERE album_request_id IS NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_album_import_request_recording
        ON artist_import_items(album_request_id, source_id) WHERE album_request_id IS NOT NULL""",
    "CREATE INDEX IF NOT EXISTS idx_artist_import_work ON artist_import_items(state, lease_until, id)",
    "CREATE INDEX IF NOT EXISTS idx_artist_import_review ON artist_import_items(batch_id, state, id)",
    """CREATE INDEX IF NOT EXISTS idx_artist_import_local_track ON artist_import_items(track_id)
        WHERE track_id IS NOT NULL AND state IN ('approved', 'existing')""",
    "CREATE INDEX IF NOT EXISTS idx_artist_import_source ON artist_import_items(source_id, state)",
)

RECORDING_SCHEMA = """
    CREATE TABLE IF NOT EXISTS yandex_recordings (
        source_id TEXT PRIMARY KEY,
        track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
        title TEXT,
        duration_ms INTEGER,
        credits TEXT NOT NULL DEFAULT '[]',
        cover_url TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_yandex_recordings_track ON yandex_recordings(track_id);
    CREATE TABLE IF NOT EXISTS yandex_recording_releases (
        source_id TEXT NOT NULL REFERENCES yandex_recordings(source_id) ON DELETE CASCADE,
        album_id TEXT NOT NULL,
        title TEXT,
        cover_url TEXT,
        PRIMARY KEY (source_id, album_id)
    );
    CREATE TABLE IF NOT EXISTS yandex_import_submissions (
        track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL REFERENCES yandex_recordings(source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_yandex_import_submissions_source ON yandex_import_submissions(source_id);
    CREATE TABLE IF NOT EXISTS yandex_import_destinations (
        track_id INTEGER NOT NULL REFERENCES yandex_import_submissions(track_id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        destination TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (track_id, user_id, destination)
    );
    CREATE TABLE IF NOT EXISTS album_import_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        artist_key TEXT NOT NULL,
        album_id TEXT NOT NULL,
        album_title TEXT NOT NULL,
        code TEXT NOT NULL UNIQUE,
        expires_at INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'created',
        eligible TEXT NOT NULL,
        draft TEXT,
        notified INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_album_import_requests_user ON album_import_requests(user_id, state);
    CREATE TABLE IF NOT EXISTS album_import_rewards (
        source_id TEXT PRIMARY KEY,
        request_id INTEGER NOT NULL REFERENCES album_import_requests(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        amount INTEGER NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS trg_yandex_recording_link
    AFTER UPDATE OF track_id ON yandex_recordings WHEN NEW.track_id IS NOT NULL BEGIN
        UPDATE artist_catalog_tracks SET track_id = NEW.track_id WHERE yandex_track_id = NEW.source_id;
    END;
"""

BIND_RECORDING_SQL = """
    UPDATE yandex_recordings SET track_id = ?
    WHERE source_id = ? AND (track_id IS NULL OR track_id = ?
        OR NOT EXISTS (SELECT 1 FROM tracks WHERE id = yandex_recordings.track_id AND status = 'approved'))
"""

SAVE_RECORDING_SQL = """
    INSERT INTO yandex_recordings(source_id, title, duration_ms, credits, cover_url)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(source_id) DO UPDATE SET title = excluded.title, duration_ms = excluded.duration_ms,
        credits = excluded.credits, cover_url = COALESCE(excluded.cover_url, yandex_recordings.cover_url)
    WHERE yandex_recordings.track_id IS NULL OR yandex_recordings.title IS NULL
"""

SAVE_RELEASE_MEMBER_SQL = """
    INSERT INTO yandex_recording_releases(source_id, album_id, title, cover_url) VALUES (?, ?, ?, ?)
    ON CONFLICT(source_id, album_id) DO UPDATE SET title = COALESCE(excluded.title, title),
        cover_url = COALESCE(excluded.cover_url, cover_url)
"""

APPROVED_RECORDING_SQL = """
    SELECT t.* FROM yandex_recordings y JOIN tracks t ON t.id = y.track_id
    WHERE y.source_id = ? AND t.status = 'approved'
"""

CREDIT_SEPARATOR = re.compile(
    r"\s*,\s*|\s+(?:feat\.?|ft\.?|featuring|при\s+уч(?:\.|астии)?|&)\s+", re.IGNORECASE
)


def metadata_key(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFC", value or "").lower().split())


def split_artist_credits(credit: str | None, confirmed_keys=()) -> list[str]:
    text = (credit or "").strip()
    if not text:
        return []
    if metadata_key(text) in confirmed_keys:
        return [text]
    separators = list(CREDIT_SEPARATOR.finditer(text))
    if not separators:
        return [text]

    # A confirmed name can itself contain a separator, even inside a collaboration.
    starts = [0, *(separator.end() for separator in separators)]
    ends = [*(separator.start() for separator in separators), len(text)]
    names = []
    start = 0
    while start < len(starts):
        end = next(
            (i for i in range(len(ends) - 1, start, -1)
             if metadata_key(text[starts[start]:ends[i]]) in confirmed_keys),
            start,
        )
        name = text[starts[start]:ends[end]].strip()
        if name:
            names.append(name)
        start = end + 1
    return names


def matches_artist_page(credit: str | None, page_key: str, confirmed_keys=()) -> bool:
    return metadata_key(credit) == page_key or any(
        metadata_key(name) == page_key for name in split_artist_credits(credit, confirmed_keys)
    )


def artist_credit_keys(credit: str | None, confirmed_keys=()) -> set[str]:
    return {metadata_key(name) for name in split_artist_credits(credit, confirmed_keys)}
