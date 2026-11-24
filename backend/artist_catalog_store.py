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

ARTIST_IMPORT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS artist_import_batches (
        id INTEGER PRIMARY KEY,
        artist_key TEXT NOT NULL UNIQUE REFERENCES artist_catalog_bindings(artist_key) ON DELETE CASCADE,
        paused INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS artist_import_items (
        id INTEGER PRIMARY KEY,
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
        UNIQUE (batch_id, source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_artist_import_work ON artist_import_items(state, lease_until, id);
    CREATE INDEX IF NOT EXISTS idx_artist_import_review ON artist_import_items(batch_id, state, id);
    CREATE INDEX IF NOT EXISTS idx_artist_import_local_track ON artist_import_items(track_id)
        WHERE track_id IS NOT NULL AND state IN ('approved', 'existing');
    CREATE INDEX IF NOT EXISTS idx_artist_import_source ON artist_import_items(source_id, state);
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
