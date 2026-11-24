"""Persistent complaints and timestamped counters for the administrator report."""
from urllib.parse import urlsplit


REASONS = {
    "censored": "Запикано",
    "wrong_version": "Другая версия",
    "quality": "Плохое качество",
}

SCHEMA = """
    CREATE TABLE IF NOT EXISTS track_complaints (
        id INTEGER PRIMARY KEY,
        track_id INTEGER NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        username TEXT,
        telegram_id INTEGER NOT NULL,
        reason TEXT NOT NULL,
        reference_url TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT,
        resolution TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_open_complaint_user
        ON track_complaints(track_id, user_id) WHERE resolved_at IS NULL;
    CREATE INDEX IF NOT EXISTS idx_complaint_dates ON track_complaints(created_at, track_id);
    CREATE INDEX IF NOT EXISTS idx_complaint_track ON track_complaints(track_id, resolved_at);
    CREATE TABLE IF NOT EXISTS complaint_audio (
        track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
        token TEXT NOT NULL,
        media_revision INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        bitrate INTEGER NOT NULL,
        duration INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS premium_payments (
        charge_id TEXT PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        stars INTEGER NOT NULL,
        paid_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_premium_payments_date ON premium_payments(paid_at);
    CREATE TABLE IF NOT EXISTS track_publications (
        track_id INTEGER PRIMARY KEY,
        published_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_track_publications_date ON track_publications(published_at);
    CREATE TABLE IF NOT EXISTS download_minutes (
        minute TEXT PRIMARY KEY,
        total INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS admin_daily_reports (
        period_end TEXT PRIMARY KEY,
        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS admin_stats_start (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    INSERT OR IGNORE INTO admin_stats_start(id) VALUES (1);
    CREATE TRIGGER IF NOT EXISTS track_publication_insert AFTER INSERT ON tracks
    WHEN NEW.status = 'approved' BEGIN
        INSERT OR IGNORE INTO track_publications VALUES (NEW.id, CURRENT_TIMESTAMP);
    END;
    CREATE TRIGGER IF NOT EXISTS track_publication_update AFTER UPDATE OF status ON tracks
    WHEN NEW.status = 'approved' AND OLD.status IS NOT 'approved' BEGIN
        INSERT OR IGNORE INTO track_publications VALUES (NEW.id, CURRENT_TIMESTAMP);
    END;
    INSERT OR IGNORE INTO track_publications(track_id)
        SELECT id FROM tracks WHERE status = 'approved';
    CREATE TRIGGER IF NOT EXISTS track_download_minute AFTER UPDATE OF download_count ON tracks
    WHEN NEW.download_count > COALESCE(OLD.download_count, 0) BEGIN
        INSERT INTO download_minutes(minute, total)
        VALUES (strftime('%Y-%m-%d %H:%M:00','now'), NEW.download_count - COALESCE(OLD.download_count, 0))
        ON CONFLICT(minute) DO UPDATE SET total = total + excluded.total;
    END;
    CREATE TRIGGER IF NOT EXISTS resolve_removed_track_complaints BEFORE DELETE ON tracks BEGIN
        UPDATE track_complaints SET resolved_at = CURRENT_TIMESTAMP, resolution = 'removed'
        WHERE track_id = OLD.id AND resolved_at IS NULL;
    END;
"""


def validate_reference(url: str) -> str:
    url = url.strip()
    try:
        parsed = urlsplit(url)
        if (len(url) <= 500 and parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password and not any(c.isspace() for c in url)):
            return url
    except ValueError:
        pass
    raise ValueError("Вставьте ссылку на нужную версию трека, начиная с https://.")
