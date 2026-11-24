"""Refresh the local Yandex Music artist catalog without importing audio."""
from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from backend.artist_catalog_store import (
    ARTIST_CATALOG_SCHEMA, CATALOG_DB_PATH, artist_credit_keys, matches_artist_page, metadata_key,
)


YANDEX_HOST = re.compile(r"(?:www\.)?music\.yandex\.(?:ru|com|by|kz|ua|uz)")
PAGE_SIZE = 50
REQUEST_TIMEOUT = 15
MAX_COVER_BYTES = 512 * 1024
DURATION_TOLERANCE_MS = 2000


def yandex_path_id(url: str, pattern: str) -> str | None:
    try:
        parsed = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    if parsed.scheme in {"http", "https"} and YANDEX_HOST.fullmatch(parsed.netloc.lower()):
        match = re.fullmatch(pattern, parsed.path)
        if match:
            return str(int(match[1])) if int(match[1]) else None
    return None


def parse_artist_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value) and int(value):
        return str(int(value))
    artist_id = yandex_path_id(value, r"/artist/([0-9]+)(?:/(?:albums|tracks|info))?/?")
    if artist_id is None:
        raise argparse.ArgumentTypeError("Укажите ID или ссылку на артиста Яндекс Музыки.")
    return artist_id


def source_id(value) -> str:
    value = str(value)
    if not re.fullmatch(r"[0-9]+", value) or not int(value):
        raise ValueError(f"Некорректный ID в ответе источника: {value}")
    return str(int(value))


def source_name(value: str | None) -> str:
    if not value or not value.strip():
        raise ValueError("Источник вернул запись без названия или имени.")
    return value.strip()


def cover_url(uri: str | None) -> str | None:
    if not uri:
        return None
    try:
        parsed = urlsplit(uri.replace("%%", "300x300"))
        if not parsed.netloc:
            parsed = urlsplit("https://" + uri.replace("%%", "300x300"))
    except ValueError:
        return None
    if parsed.netloc.lower() in {"avatars.yandex.net", "avatars.mds.yandex.net"}:
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    return None


def fetch_cover(url: str) -> tuple[bytes, str] | None:
    try:
        with urlopen(url, timeout=REQUEST_TIMEOUT) as response:
            data = response.read(MAX_COVER_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(data) > MAX_COVER_BYTES:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data, "image/webp"
    return None


@dataclass(frozen=True)
class CatalogTrack:
    disc: int
    position: int
    id: str
    title: str
    version: str
    warning: str
    credits: tuple[tuple[str, str], ...]
    duration_ms: int | None
    isrc: str | None

    @property
    def display_title(self) -> str:
        title = self.title
        for label in (self.version, self.warning):
            if label and not has_title_suffix(title, label):
                title += f" ({label})"
        return title

    @property
    def artist(self) -> str:
        return ", ".join(name for _, name in self.credits)


@dataclass(frozen=True)
class Release:
    id: str
    title: str
    year: int | None
    kind: str | None
    cover_url: str | None
    tracks: list[CatalogTrack]


def read_source_track(item, disc: int, position: int) -> CatalogTrack:
    if item is None or getattr(item, "error", None) or not item.artists:
        raise ValueError("Яндекс Музыка вернула трек без полных метаданных.")
    credits = tuple((source_id(a.id), source_name(a.name)) for a in item.artists)
    warning = "Explicit" if getattr(item, "explicit", False) else ""
    warning = (getattr(item, "content_warning", None) or warning).strip()
    duration = item.duration_ms
    isrc = (getattr(item, "isrc", None) or "").strip().upper()
    return CatalogTrack(
        disc, position, source_id(item.id), source_name(item.title),
        (getattr(item, "version", None) or "").strip(), warning, credits,
        duration if isinstance(duration, int) and duration > 0 else None,
        isrc if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", isrc) else None,
    )


class CatalogSource:
    def __init__(self, client, delay: float):
        self.client = client
        self.delay = delay
        self.next_request = time.monotonic() + delay

    def request(self, method, *args, **kwargs):
        time.sleep(max(0, self.next_request - time.monotonic()))
        try:
            return method(*args, **kwargs)
        finally:
            self.next_request = time.monotonic() + self.delay

    def discography(self, artist_id: str) -> tuple[str, list[Release]]:
        artists = self.request(self.client.artists, artist_id, timeout=REQUEST_TIMEOUT)
        if len(artists or []) != 1 or source_id(artists[0].id) != artist_id:
            raise ValueError("Источник не вернул подтверждённого артиста.")
        name = source_name(artists[0].name)
        get_discography = getattr(self.client, "artists_discography_albums", None)
        get_direct_albums = getattr(self.client, "artists_direct_albums", None)
        albums = self.pages(get_discography or get_direct_albums, artist_id, "albums")
        # Some artists have an empty discography response but a populated direct-albums list.
        if not albums and get_discography is not None and get_direct_albums is not None:
            albums = self.pages(get_direct_albums, artist_id, "albums")
        get_release = getattr(self.client, "albums_with_tracks", None)
        if get_release is None:
            get_release = self.client.album_with_tracks
        releases = [
            self.release(album_id, item, self.request(get_release, album_id, timeout=REQUEST_TIMEOUT))
            for album_id, item in albums.items()
        ]
        return name, releases

    def artist_tracks(self, artist_id: str) -> list[CatalogTrack]:
        tracks = self.pages(self.client.artists_tracks, artist_id, "tracks")
        return [read_source_track(item, 1, position) for position, item in enumerate(tracks.values(), 1)]

    def pages(self, method, artist_id: str, collection: str) -> dict:
        items = {}
        page = 0
        totals = None
        while True:
            response = self.request(
                method, artist_id, page=page, page_size=PAGE_SIZE, timeout=REQUEST_TIMEOUT,
            )
            pager = getattr(response, "pager", None)
            rows = getattr(response, collection, None)
            if pager is None or rows is None or pager.page != page:
                raise ValueError("Источник вернул неполную страницу каталога.")
            total, per_page = pager.total, pager.per_page
            if (not isinstance(total, int) or not isinstance(per_page, int)
                    or total < 0 or per_page < 0 or (per_page == 0 and (total != 0 or rows))):
                raise ValueError("Источник вернул некорректную пагинацию дискографии.")
            if totals is None:
                totals = total, per_page
            if totals != (total, per_page) or len(rows) != min(per_page, total - len(items)):
                raise ValueError("Каталог изменился или вернулся не полностью.")
            for row in rows:
                row_id = source_id(row.id)
                if row_id in items:
                    raise ValueError(f"Запись {row_id} повторяется в каталоге.")
                items[row_id] = row
            if len(items) == total:
                return items
            page += 1

    @staticmethod
    def release(album_id: str, listed, album) -> Release:
        if album is None or getattr(album, "error", None) or source_id(album.id) != album_id:
            raise ValueError(f"Источник не вернул запрошенный релиз {album_id}.")
        volumes = album.volumes
        if volumes is None or any(not volume for volume in volumes):
            raise ValueError(f"Источник не вернул состав релиза {album_id}.")
        total = sum(map(len, volumes))
        counts = [item.track_count for item in (listed, album) if item.track_count is not None]
        if not counts or any(count != total for count in counts):
            raise ValueError(f"Источник вернул неполный состав релиза {album_id}.")
        tracks = []
        for disc, volume in enumerate(volumes, 1):
            for position, item in enumerate(volume, 1):
                tracks.append(read_source_track(item, disc, position))
        kind = getattr(album, "type", None) or getattr(listed, "type", None)
        if not kind:
            kind = getattr(album, "meta_type", None) or getattr(listed, "meta_type", None)
        title = source_name(album.title)
        version = (getattr(album, "version", None) or "").strip()
        if version and not has_title_suffix(title, version):
            title += f" ({version})"
        return Release(
            album_id, title, album.year or listed.year,
            metadata_key(kind) if kind else None,
            cover_url(album.cover_uri or listed.cover_uri), tracks,
        )


def title_variants(title: str, label: str) -> set[str]:
    key = metadata_key(title)
    if not label or has_title_suffix(title, label):
        return {key}
    label = metadata_key(label)
    return {f"{key} ({label})", f"{key} [{label}]", *(f"{key} {dash} {label}" for dash in "-–—")}


def has_title_suffix(title: str, label: str) -> bool:
    key, label = metadata_key(title), metadata_key(label)
    return key.endswith((f" ({label})", f" [{label}]", *(f" {d} {label}" for d in "-–—")))


def candidate_title(title: str | None) -> str:
    # Broad title lookup for the report only. A link always uses the full title.
    title = re.split(
        r"\s*[\[(]|\s+[-–—]\s+|\s+(?:live|remix|instrumental|clean|explicit|acoustic|"
        r"remastered?|ремикс|инструментал)(?:\s|$)", title or "", maxsplit=1, flags=re.IGNORECASE,
    )[0]
    return metadata_key(title)


APPROVED_IMPORT_LINKS_SQL = """
    SELECT DISTINCT i.source_id, i.track_id
    FROM artist_import_items i
    JOIN tracks t ON t.id = i.track_id AND t.status = 'approved'
    WHERE i.state IN ('approved', 'existing')
"""


class LocalTracks:
    def __init__(self, conn: sqlite3.Connection, local_artist: str, confirmed_keys):
        self.artist_key = metadata_key(local_artist)
        self.confirmed_keys = set(confirmed_keys) | {self.artist_key}
        self.bound_names = {row["yandex_artist_id"]: row["artist_key"] for row in confirmed_keys.values()}
        self.by_source = defaultdict(list)
        self.reviewed = defaultdict(list)
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artist_import_items'"
        ).fetchone():
            for row in conn.execute(APPROVED_IMPORT_LINKS_SQL):
                self.reviewed[row["source_id"]].append(row["track_id"])
        self.by_title = defaultdict(list)
        self.has_page = False
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
        duration = "duration" if "duration" in columns else "NULL AS duration"
        url = "source_url" if "source_url" in columns else "NULL AS source_url"
        for row in conn.execute(f"SELECT id, artist, title, status, {duration}, {url} FROM tracks"):
            external_id = yandex_path_id(row["source_url"] or "", r"/(?:album/[0-9]+/)?track/([0-9]+)/?")
            on_page = matches_artist_page(row["artist"], self.artist_key, self.confirmed_keys)
            if external_id:
                self.by_source[external_id].append(row)
            if on_page:
                self.by_title[candidate_title(row["title"])].append((row, external_id))
                self.has_page |= row["status"] == "approved"

    def match(self, track: CatalogTrack, artist_id: str) -> tuple[int | None, str]:
        reviewed = self.reviewed.get(track.id, [])
        direct = self.by_source.get(track.id, [])
        if reviewed:
            ids = set(reviewed) | {row["id"] for row in direct if row["status"] == "approved"}
            if len(ids) == 1:
                return reviewed[0], "ID Яндекс Музыки из предыдущего импорта"
            return None, "проверить несколько привязок ID Яндекс Музыки: " + ", ".join(map(str, sorted(ids)))
        if direct:
            if len(direct) == 1 and direct[0]["status"] == "approved":
                return direct[0]["id"], "ID Яндекс Музыки"
            return None, "проверить ID источника: " + describe_candidates(direct)

        candidates = self.by_title.get(candidate_title(track.title), [])
        if not candidates:
            return None, "нет в базе"
        if artist_id not in {key for key, _ in track.credits}:
            return None, "проверить исполнителя: " + describe_candidates(row for row, _ in candidates)

        # Substitute the operator-confirmed local name only for the bound ID.
        expected_credit = {
            self.artist_key if key == artist_id else self.bound_names.get(key, metadata_key(name))
            for key, name in track.credits
        }
        protected_names = self.confirmed_keys | expected_credit
        titles = {variant for title in title_variants(track.title, track.version)
                  for variant in title_variants(title, track.warning)}
        matches = []
        unknown_duration = False
        for row, other_id in candidates:
            if (other_id or row["status"] != "approved"
                    or artist_credit_keys(row["artist"], protected_names) != expected_credit):
                continue
            if metadata_key(row["title"]) not in titles:
                continue
            duration = row["duration"]
            if not track.duration_ms or not isinstance(duration, (int, float)) or duration <= 0:
                unknown_duration = True
            elif abs(1000 * duration - track.duration_ms) <= DURATION_TOLERANCE_MS:
                matches.append(row)
        if len(matches) == 1 and not unknown_duration:
            return matches[0]["id"], "исполнитель, полное название и длительность"
        reason = "нет длительности" if unknown_duration else "проверить запись, версию или длительность"
        return None, f"{reason}: " + describe_candidates(row for row, _ in candidates)


def describe_candidates(rows) -> str:
    return "; ".join(
        f'#{r["id"]} {r["artist"]} — {r["title"]} ({r["duration"] or "?"} с, {r["status"]})'
        for r in rows
    )


def load_bindings(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artist_catalog_bindings'"
    ).fetchone()
    if not table:
        return {}
    return {row["artist_key"]: row for row in conn.execute(
        "SELECT * FROM artist_catalog_bindings ORDER BY attempted_at, artist_key"
    )}


def cached_covers(conn: sqlite3.Connection, key: str, binding) -> dict:
    if binding is None:
        return {}
    return {row["album_id"]: row for row in conn.execute(
        """SELECT album_id, cover_url, cover, cover_mime FROM artist_catalog_albums
           WHERE artist_key = ? AND cover IS NOT NULL""", (key,)
    )}


def collect_covers(source: CatalogSource, releases: list[Release], cached: dict) -> dict:
    result = {}
    for release in releases:
        existing = cached.get(release.id)
        if existing and (not release.cover_url or existing["cover_url"] == release.cover_url):
            result[release.id] = (existing["cover_url"], existing["cover"], existing["cover_mime"])
        elif release.cover_url:
            cover = source.request(fetch_cover, release.cover_url)
            if cover:
                result[release.id] = (release.cover_url, *cover)
            elif existing:
                result[release.id] = (existing["cover_url"], existing["cover"], existing["cover_mime"])
    return result


def inspect_releases(local: LocalTracks, artist_id: str, releases: list[Release]) -> dict:
    matches = {album.id: [(track, *local.match(track, artist_id)) for track in album.tracks]
               for album in releases}
    recordings = defaultdict(dict)
    for rows in matches.values():
        for track, local_id, _ in rows:
            if local_id is not None and track.id not in local.by_source and track.id not in local.reviewed:
                recordings[local_id].setdefault(track.id, set()).add(track.isrc)
    ambiguous = {}
    for local_id, source_tracks in recordings.items():
        all_isrcs = set().union(*source_tracks.values())
        if len(source_tracks) > 1 and (len(all_isrcs) != 1 or None in all_isrcs):
            ambiguous[local_id] = ", ".join(sorted(source_tracks))
    for album_id, rows in matches.items():
        matches[album_id] = [
            (track, None, f"местный трек #{local_id} подходит нескольким ID источника: {ambiguous[local_id]}")
            if (local_id in ambiguous and track.id not in local.by_source and track.id not in local.reviewed)
            else (track, local_id, reason)
            for track, local_id, reason in rows
        ]
    return matches


def save_catalog(conn, key, name, artist_id, source_artist, releases, matches, covers, attempt):
    conn.execute(
        """INSERT INTO artist_catalog_bindings VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(artist_key) DO UPDATE SET
             yandex_artist_name = excluded.yandex_artist_name, updated_at = excluded.updated_at,
             attempted_at = MAX(attempted_at, excluded.attempted_at)""",
        (key, name, artist_id, source_artist, time.time_ns(), attempt),
    )
    conn.execute("DELETE FROM artist_catalog_albums WHERE artist_key = ?", (key,))
    conn.executemany(
        """INSERT INTO artist_catalog_albums
           (artist_key, album_id, title, year, release_type, cover_url, cover, cover_mime)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ((key, album.id, album.title, album.year, album.kind,
          *covers.get(album.id, (album.cover_url, None, None))) for album in releases),
    )
    conn.executemany(
        """INSERT INTO artist_catalog_tracks
           (artist_key, album_id, disc, position, yandex_track_id, title, artist, duration_ms, track_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ((key, album.id, track.disc, track.position, track.id, track.display_title,
          track.artist, track.duration_ms, local_id)
         for album in releases for track, local_id, _ in matches[album.id]),
    )


def print_report(local_name, artist_id, source_name, releases, matches, covers, apply):
    print(f"{local_name} → {source_name} (Яндекс Музыка #{artist_id}) · релизов: {len(releases)}")
    for album in releases:
        rows = matches[album.id]
        available = sum(local_id is not None for _, local_id, _ in rows)
        cover_info = " · обложка недоступна" if album.cover_url and album.id not in covers else ""
        print(f"  #{album.id} {album.title} ({album.year or 'год неизвестен'}) · {available}/{len(rows)}{cover_info}")
        for track, local_id, detail in rows:
            result = f"#{local_id} ({detail})" if local_id is not None else detail
            print(f"    {track.disc}.{track.position} #{track.id} {track.artist} — {track.display_title} → {result}")
    print("Каталог сохранён." if apply else "Dry-run. База данных без изменений.")


def refresh(conn, source, name, artist_id, binding, apply):
    key = metadata_key(name)
    attempt = time.time_ns()
    try:
        if binding is None:
            confirmed_keys = set(load_bindings(conn)) | {key}
            with closing(conn.execute("SELECT DISTINCT artist FROM tracks WHERE status = 'approved'")) as credits:
                if not any(matches_artist_page(row["artist"], key, confirmed_keys) for row in credits):
                    raise ValueError("Страница должна иметь хотя бы один одобренный трек с указанным именем артиста.")
        source_artist, releases = source.discography(artist_id)
        if binding and not releases and conn.execute(
            "SELECT 1 FROM artist_catalog_albums WHERE artist_key = ? LIMIT 1", (key,),
        ).fetchone():
            raise ValueError("Ранее непустая дискография вернулась пустой.")
        covers = collect_covers(source, releases, cached_covers(conn, key, binding))
        if apply:
            conn.executescript(ARTIST_CATALOG_SCHEMA)
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT * FROM artist_catalog_bindings WHERE artist_key = ? OR yandex_artist_id = ?",
                    (key, artist_id),
                ).fetchall()
                expected = [(key, binding["updated_at"])] if binding else []
                if [(row["artist_key"], row["updated_at"]) for row in current] != expected:
                    raise ValueError("Привязка или каталог изменились во время запроса. Повторите запуск.")
                local = LocalTracks(conn, name, load_bindings(conn))
                if binding is None and not local.has_page:
                    raise ValueError("Одобренные треки страницы изменились во время запроса.")
                matches = inspect_releases(local, artist_id, releases)
                save_catalog(conn, key, name, artist_id, source_artist, releases, matches, covers, attempt)
        else:
            matches = inspect_releases(LocalTracks(conn, name, load_bindings(conn)), artist_id, releases)
        print_report(name, artist_id, source_artist, releases, matches, covers, apply)
    except Exception as exc:
        if apply and binding:
            with conn:
                conn.execute(
                    """UPDATE artist_catalog_bindings SET attempted_at = MAX(attempted_at, ?)
                       WHERE artist_key = ? AND yandex_artist_id = ?""", (attempt, key, artist_id),
                )
        raise RuntimeError(f"{name}: {exc}") from exc


def connect(db_path: Path, apply: bool) -> sqlite3.Connection:
    mode = "rw" if apply else "ro"
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main(argv=None, *, client_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-artist", help="Имя страницы FreeDom Music")
    parser.add_argument("--yandex-artist", type=parse_artist_id, help="ID или ссылка на артиста Яндекс Музыки")
    parser.add_argument("--refresh-confirmed", action="store_true", help="Обновить только подтверждённые страницы")
    parser.add_argument("--limit", type=int, default=10, help="Максимум артистов за один пакет (10 по умолчанию)")
    parser.add_argument("--delay", type=float, default=0.5, help="Пауза между запросами в секундах (0.5 по умолчанию)")
    parser.add_argument("--db", type=Path, default=CATALOG_DB_PATH, help="Путь к существующей SQLite")
    parser.add_argument("--apply", action="store_true", help="Записать каталог. По умолчанию dry-run")
    args = parser.parse_args(argv)
    if args.limit < 1 or not math.isfinite(args.delay) or args.delay < 0:
        parser.error("Укажите положительный --limit и неотрицательный --delay.")
    if args.refresh_confirmed:
        if args.local_artist is not None or args.yandex_artist:
            parser.error("--refresh-confirmed запускается без --local-artist и --yandex-artist.")
    elif not args.local_artist or not args.local_artist.strip():
        parser.error("Укажите --local-artist или --refresh-confirmed.")
    try:
        with closing(connect(args.db, args.apply)) as conn:
            bindings = load_bindings(conn)
            if args.refresh_confirmed:
                jobs = [(row["artist_name"], row["yandex_artist_id"], row)
                        for row in list(bindings.values())[:args.limit]]
            else:
                name = args.local_artist.strip()
                key = metadata_key(name)
                binding = bindings.get(key)
                artist_id = args.yandex_artist or (binding and binding["yandex_artist_id"])
                if not artist_id:
                    raise ValueError("Для первой привязки нужен --yandex-artist.")
                if binding and binding["yandex_artist_id"] != artist_id:
                    raise ValueError(f'Страница уже связана с ID {binding["yandex_artist_id"]}.')
                if any(other["artist_key"] != key and other["yandex_artist_id"] == artist_id
                       for other in bindings.values()):
                    raise ValueError(f"ID {artist_id} уже связан с другой страницей FreeDom Music.")
                jobs = [(binding["artist_name"] if binding else name, artist_id, binding)]
            if not jobs:
                print("Подтверждённых артистов пока нет.")
                return 0
            if client_factory is None:
                from backend.parser import get_yandex_client
                client_factory = get_yandex_client
            source = CatalogSource(client_factory(), args.delay)
            failed = False
            for name, artist_id, binding in jobs:
                try:
                    refresh(conn, source, name, artist_id, binding, args.apply)
                except RuntimeError as exc:
                    print(f"Ошибка обновления · {exc}", file=sys.stderr)
                    failed = True
            return int(failed)
    except Exception as exc:
        print(f"Ошибка обновления · {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
