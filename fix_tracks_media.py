#!/usr/bin/env python3
"""
fix_tracks_media.py — одноразовая «починка» старых треков FreeDom Music.

Что делает для каждого трека из `tracks`, у которого есть file_path:
  1. Открывает .mp3 через mutagen и берёт реальную длительность.
     Если она отличается от `tracks.duration` (или там 0/NULL) — обновляет БД.
  2. Проверяет обложку (APIC):
       - нет / битая            -> ищет на SoundCloud, приводит к JPEG 320x320, вшивает;
       - есть, но не годится    -> (WebP/PNG, >320px, >200 КБ, несколько APIC)
                                   перекодирует в JPEG 320x320 и вшивает обратно;
       - есть и годится         -> файл не трогает.
  3. Если файл или длительность изменились (или файл > 10 МБ), сбрасывает
     `tracks.file_id`, чтобы бот перезалил трек в Telegram файлом. Пока file_id
     закеширован, Telegram отдаёт старую версию без обложки и с 00:00 —
     thumbnail при отправке по file_id игнорируется.
  4. (опционально, --rebuild-topics) помечает оффлайн-темы, где есть такие треки,
     на пересборку.

Безопасность: нет файла -> пропуск; ошибка на одном треке не валит остальные;
перед записью делается копия БД; есть --dry-run и --backup-dir.
Повторный запуск безопасен: что уже исправлено, пропускается.

Запуск из корня проекта (рядом с папкой backend/):
    python fix_tracks_media.py --dry-run --limit 30
    python fix_tracks_media.py
    python fix_tracks_media.py --rebuild-topics
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from mutagen.id3 import APIC
from mutagen.mp3 import MP3
from PIL import Image, ImageOps

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

try:
    import yt_dlp
    from backend.downloader import (
        UPLOADS_DIR, build_search_queries, clean_title, has_bad_marker, match_track, split_artists,
    )
except ImportError as exc:
    sys.exit(f"Не удалось импортировать backend.downloader ({exc}). "
             f"Запускай скрипт из корня проекта, рядом с папкой backend/.")

logger = logging.getLogger("fix_tracks_media")

DEFAULT_DB = BASE_DIR / "backend" / "music_bot.db"
THUMB_SIZE = 320                      # Telegram: ширина и высота thumbnail <= 320
THUMB_MAX_BYTES = 190 * 1024          # лимит Telegram 200 КБ, оставляем запас
BIG_FILE_BYTES = 10 * 1024 * 1024     # выше этого Telegram показывал 00:00
MAX_IMAGE_DOWNLOAD = 15 * 1024 * 1024


# ============================================================
# СТАТИСТИКА
# ============================================================
@dataclass
class Stats:
    total: int = 0
    processed: int = 0            # файл найден, mp3 прочитан без ошибок
    missing_file: int = 0
    not_mp3: int = 0
    errors: int = 0
    duration_fixed: int = 0
    covers_ok: int = 0            # обложка уже подходила Telegram
    covers_added: int = 0         # не было / битая -> взяли с SoundCloud
    covers_normalized: int = 0    # была, но не подходила -> пережали в JPEG 320x320
    covers_not_found: int = 0
    file_id_reset: int = 0
    reset_ids: list[int] = field(default_factory=list)


# ============================================================
# ПУТИ
# ============================================================
def resolve_path(raw: str) -> Path | None:
    """file_path из БД -> реальный файл. Понимает относительные пути и «чужие» (Windows) пути."""
    raw = raw.strip()
    p = Path(raw)
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    candidates = [p] if p.is_absolute() else [BASE_DIR / p, Path.cwd() / p]
    candidates.append(UPLOADS_DIR / name)
    return next((c for c in candidates if c.is_file()), None)


# ============================================================
# ОБЛОЖКИ: Pillow
# ============================================================
def to_tg_jpeg(raw: bytes) -> bytes:
    """Любая картинка (JPEG/PNG/WebP/...) -> квадратный JPEG 320x320 до 190 КБ."""
    with Image.open(BytesIO(raw)) as src:
        img = ImageOps.exif_transpose(src)
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")            # прозрачность -> белый фон, а не чёрный
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        else:
            img = img.convert("RGB")
        img = ImageOps.fit(img, (THUMB_SIZE, THUMB_SIZE), method=Image.LANCZOS)
    for quality in (90, 82, 72, 60, 45):
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= THUMB_MAX_BYTES:
            return buf.getvalue()
    raise ValueError("обложка не укладывается в лимит Telegram")


def inspect_cover(tags) -> tuple[str, bytes | None]:
    """-> ('none' | 'broken' | 'convert' | 'ok', байты лучшей обложки)."""
    all_frames = tags.getall("APIC") if tags is not None else []
    frames = [f for f in all_frames if f.data]
    if not frames:
        return "none", None
    frame = next((f for f in frames if f.type == 3), frames[0])   # type 3 = Cover (front)
    data = bytes(frame.data)
    try:
        with Image.open(BytesIO(data)) as im:
            fmt, (w, h) = im.format, im.size
    except Exception:
        return "broken", None                  # не картинка / формат, который Pillow не читает
    fine = (
        fmt == "JPEG"                          # реальный формат, а не заявленный mime
        and (frame.mime or "").lower() in ("image/jpeg", "image/jpg")
        and len(data) <= THUMB_MAX_BYTES
        and w <= THUMB_SIZE and h <= THUMB_SIZE
        and len(all_frames) == 1               # оффлайн-воркер берёт ПЕРВЫЙ APIC
    )
    return ("ok" if fine else "convert"), data


def write_cover(audio: MP3, jpeg: bytes) -> None:
    if audio.tags is None:
        audio.add_tags()
    audio.tags.delall("APIC")
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
    audio.save()


def backup_original(path: Path, track_id: int, backup_dir: Path | None) -> Path | None:
    if not backup_dir:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{track_id}_{path.name}"
    if not dst.exists():
        shutil.copy2(path, dst)
    return dst


# ============================================================
# ОБЛОЖКИ: поиск на SoundCloud
# ============================================================
def thumbnail_urls(info: dict) -> list[str]:
    """URL обложек: сначала самые крупные из тех, у кого указан размер."""
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    thumbs.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    urls = [t["url"] for t in thumbs]
    if info.get("thumbnail") and info["thumbnail"] not in urls:
        urls.append(info["thumbnail"])
    return urls


class CoverFinder:
    def __init__(self, pause: float):
        self.pause = pause
        self.ydl = yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "socket_timeout": 20,
        })
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "Mozilla/5.0 (compatible; FreeDomMusic-backfill)"

    def find(self, artist: str, title: str) -> bytes | None:
        """Готовый JPEG 320x320 или None."""
        queries = list(build_search_queries(artist, title, limit=3))
        if not queries:
            main = (split_artists(artist) or [artist])[0]
            queries = [f"{main} {clean_title(title)}".strip()]
        for query in queries:
            try:
                result = self.ydl.extract_info(f"scsearch5:{query}", download=False)
            except Exception as e:
                logger.warning("SoundCloud: поиск '%s' не удался: %s", query, e)
                continue
            finally:
                time.sleep(self.pause)
            for entry in (result or {}).get("entries") or []:
                if not entry:
                    continue
                etitle = entry.get("title") or ""
                if has_bad_marker(etitle, title):          # slowed / 8D / remix, если этого нет в запросе
                    continue
                if not match_track(f"{entry.get('uploader') or ''} - {etitle}", artist, title):
                    continue                               # чужой трек — чужая обложка нам не нужна
                jpeg = self._cover_from_entry(entry)
                if jpeg:
                    return jpeg
        return None

    def _cover_from_entry(self, entry: dict) -> bytes | None:
        urls = thumbnail_urls(entry)
        if not urls and entry.get("url"):                  # flat-результат без картинок -> один дешёвый resolve
            try:
                urls = thumbnail_urls(self.ydl.extract_info(entry["url"], download=False, process=False) or {})
            except Exception as e:
                logger.debug("SoundCloud: resolve %s не удался: %s", entry.get("url"), e)
        for url in urls[:2]:
            try:
                r = self.http.get(url, timeout=15)
                if r.status_code == 200 and 0 < len(r.content) <= MAX_IMAGE_DOWNLOAD:
                    return to_tg_jpeg(r.content)
            except Exception as e:
                logger.debug("Обложка %s не подошла: %s", url, e)
        return None


# ============================================================
# ОБРАБОТКА ОДНОГО ТРЕКА
# ============================================================
@dataclass
class Ctx:
    args: argparse.Namespace
    conn: sqlite3.Connection
    stats: Stats
    finder: CoverFinder | None
    has_file_id: bool


def process_track(row: sqlite3.Row, ctx: Ctx) -> None:
    st, args = ctx.stats, ctx.args
    tid = row["id"]
    label = f"#{tid} {row['artist']} - {row['title']}"

    path = resolve_path(row["file_path"])
    if path is None:
        st.missing_file += 1
        logger.info("%s: файла нет на диске, пропускаю (%s)", label, row["file_path"])
        return
    if path.suffix.lower() != ".mp3":
        st.not_mp3 += 1
        logger.info("%s: не .mp3, пропускаю (%s)", label, path.name)
        return

    audio = MP3(path)
    real = int(round(audio.info.length))
    if real <= 0:
        raise ValueError("mutagen вернул нулевую длительность (файл повреждён?)")

    changed_file = False
    changed_duration = False

    # --- 1. длительность -------------------------------------------------
    db_dur = int(row["duration"] or 0)
    if abs(real - db_dur) > args.tolerance:
        logger.info("%s: длительность %s -> %s с", label, db_dur, real)
        changed_duration = True
        st.duration_fixed += 1

    # --- 2. обложка ------------------------------------------------------
    if not args.skip_covers:
        state, raw = inspect_cover(audio.tags)
        if state == "ok":
            st.covers_ok += 1
        else:
            jpeg, kind = None, "added"
            if state == "convert":
                try:
                    jpeg, kind = to_tg_jpeg(raw), "normalized"
                except Exception as e:
                    logger.warning("%s: не смог перекодировать обложку (%s), ищу на SoundCloud", label, e)
            if jpeg is None:
                jpeg, kind = ctx.finder.find(row["artist"], row["title"]), "added"

            if jpeg is None:
                st.covers_not_found += 1
                logger.info("%s: обложка не найдена (состояние: %s)", label, state)
            else:
                if not args.dry_run:
                    backup = backup_original(path, tid, args.backup_dir)
                    try:
                        write_cover(audio, jpeg)
                        check = MP3(path)              # sanity: аудио цело, APIC на месте
                        if abs(check.info.length - audio.info.length) > 1 or not check.tags.getall("APIC"):
                            raise RuntimeError("после записи файл не прошёл проверку")
                    except Exception:
                        if backup:
                            shutil.copy2(backup, path)  # откат из копии
                        raise
                changed_file = True
                if kind == "normalized":
                    st.covers_normalized += 1
                else:
                    st.covers_added += 1
                logger.info("%s: обложка %s (%s КБ)", label,
                            "пережата в JPEG 320x320" if kind == "normalized" else "добавлена с SoundCloud",
                            len(jpeg) // 1024)

    # --- 3. БД: длительность + сброс file_id ------------------------------
    is_big = path.stat().st_size > BIG_FILE_BYTES
    need_reset = (
        ctx.has_file_id and not args.keep_file_id and bool(row["file_id"])
        and (changed_file or changed_duration or is_big)
    )
    if need_reset:
        st.file_id_reset += 1
        st.reset_ids.append(tid)
    if not args.dry_run and (changed_duration or need_reset):
        with ctx.conn:                                  # одна короткая транзакция на трек
            if changed_duration:
                ctx.conn.execute("UPDATE tracks SET duration = ? WHERE id = ?", (real, tid))
            if need_reset:
                ctx.conn.execute("UPDATE tracks SET file_id = NULL WHERE id = ?", (tid,))

    st.processed += 1


# ============================================================
# ПОСЛЕ ОСНОВНОГО ПРОХОДА
# ============================================================
def rebuild_topics(conn: sqlite3.Connection, track_ids: list[int]) -> int:
    """Помечает оффлайн-темы с этими треками как требующие синхронизации (rev + 1)."""
    topics: set[int] = set()
    for i in range(0, len(track_ids), 500):
        chunk = track_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        topics.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT tt.id FROM telegram_topics tt "
            f"JOIN playlist_tracks pt ON pt.playlist_id = tt.playlist_id "
            f"WHERE pt.track_id IN ({marks})", chunk))
    topic_list = sorted(topics)
    with conn:
        for i in range(0, len(topic_list), 500):
            chunk = topic_list[i:i + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"UPDATE telegram_topics SET rev = rev + 1, run_after = datetime('now') "
                         f"WHERE id IN ({marks})", chunk)
    return len(topic_list)


def backup_db(src: Path) -> Path:
    dst = src.with_name(f"{src.stem}.pre-media-fix-{datetime.now():%Y%m%d-%H%M%S}{src.suffix}")
    with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(dst)) as d:
        s.backup(d)
    return dst


def print_report(st: Stats, args: argparse.Namespace, interrupted: bool, topics: int | None) -> None:
    seen = st.processed + st.missing_file + st.not_mp3 + st.errors
    lines = [
        "",
        "=" * 56,
        "ИТОГ" + ("  (DRY-RUN — ничего не записано)" if args.dry_run else ""),
        "=" * 56,
        f"Треков с file_path в выборке:        {st.total}",
        f"Просмотрено:                         {seen}" + ("  (прервано вручную)" if interrupted else ""),
        f"  обработано успешно:                {st.processed}",
        f"  файла нет на диске (пропущено):    {st.missing_file}",
        f"  не .mp3 (пропущено):               {st.not_mp3}",
        f"  ошибок:                            {st.errors}",
        "",
        f"Длительностей исправлено:            {st.duration_fixed}",
        f"Обложек добавлено (SoundCloud):      {st.covers_added}",
        f"Обложек пережато в JPEG 320x320:     {st.covers_normalized}",
        f"Обложек уже было в порядке:          {st.covers_ok}",
        f"Обложек найти не удалось:            {st.covers_not_found}",
        f"Сброшено file_id (перезальются):     {st.file_id_reset}",
    ]
    if topics is not None:
        lines.append(f"Оффлайн-тем помечено на пересборку:  {topics}")
    print("\n".join(lines))


# ============================================================
# MAIN
# ============================================================
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Чинит длительность и обложки старых треков.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"путь к music_bot.db (по умолчанию {DEFAULT_DB})")
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать ни в БД, ни в файлы")
    ap.add_argument("--limit", type=int, default=0, help="обработать только первые N треков")
    ap.add_argument("--ids", type=int, nargs="+", help="обработать только эти id треков")
    ap.add_argument("--tolerance", type=int, default=1, help="допуск по длительности, сек (по умолчанию 1)")
    ap.add_argument("--skip-covers", action="store_true", help="только длительности, без обложек и сети")
    ap.add_argument("--keep-file-id", action="store_true", help="не сбрасывать tracks.file_id")
    ap.add_argument("--rebuild-topics", action="store_true",
                    help="пометить оффлайн-темы со сброшенными треками на пересборку")
    ap.add_argument("--backup-dir", type=Path, help="копировать оригинал mp3 сюда перед изменением")
    ap.add_argument("--no-db-backup", action="store_true", help="не делать копию БД перед записью")
    ap.add_argument("--sleep", type=float, default=0.7, help="пауза между запросами к SoundCloud, сек")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    for stream in (sys.stdout, sys.stderr):      # не падать на редких символах в консоли Windows
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(BASE_DIR / "fix_tracks_media.log", encoding="utf-8")],
    )
    logging.getLogger("yt_dlp").setLevel(logging.ERROR)

    if not args.db.is_file():
        logger.error("БД не найдена: %s (укажи --db)", args.db)
        return 1
    if not args.dry_run and not args.no_db_backup:
        logger.info("Копия БД: %s", backup_db(args.db))

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")      # бот может работать параллельно (WAL)
    has_file_id = any(r[1] == "file_id" for r in conn.execute("PRAGMA table_info(tracks)"))

    sql = (f"SELECT id, artist, title, file_path, duration, {'file_id' if has_file_id else 'NULL'} AS file_id "
           f"FROM tracks WHERE file_path IS NOT NULL AND TRIM(file_path) <> ''")
    params: list = []
    if args.ids:
        sql += f" AND id IN ({','.join('?' * len(args.ids))})"
        params += args.ids
    sql += " ORDER BY id"
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()

    stats = Stats(total=len(rows))
    ctx = Ctx(args, conn, stats, None if args.skip_covers else CoverFinder(args.sleep), has_file_id)
    logger.info("К обработке: %d треков%s", len(rows), " (DRY-RUN)" if args.dry_run else "")

    interrupted = False
    try:
        for i, row in enumerate(rows, 1):
            try:
                process_track(row, ctx)
            except KeyboardInterrupt:
                raise
            except Exception as e:                   # ошибка одного трека не роняет весь процесс
                conn.rollback()
                stats.errors += 1
                logger.error("#%s %s - %s: ошибка: %s", row["id"], row["artist"], row["title"], e,
                             exc_info=args.verbose)
            if i % 25 == 0:
                logger.info("... %d / %d", i, len(rows))
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("Прервано (Ctrl+C). Всё, что уже обработано, сохранено.")

    topics = None
    if args.rebuild_topics and stats.reset_ids and not args.dry_run:
        topics = rebuild_topics(conn, stats.reset_ids)
    conn.close()

    print_report(stats, args, interrupted, topics)
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
