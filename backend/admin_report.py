"""Daily private report for the last completed Moscow reporting period."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend import database as db

logger = logging.getLogger(__name__)


def period_end(now: datetime) -> datetime:
    cutoff = now.replace(hour=5, minute=0, second=0, microsecond=0)
    return cutoff if now >= cutoff else cutoff - timedelta(days=1)


def utc_stamp(local: datetime) -> str:
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def build_report(end: datetime) -> str | None:
    start = end - timedelta(days=1)
    async with db.get_db() as conn, conn.execute("""
        WITH window(lo, hi) AS (VALUES (?, ?))
        SELECT
            (SELECT started_at FROM admin_stats_start WHERE id = 1) AS started,
            EXISTS (SELECT 1 FROM admin_daily_reports WHERE period_end = hi) AS sent,
            (SELECT COUNT(*) FROM users WHERE created_at >= lo AND created_at < hi) AS new_users,
            (SELECT COUNT(*) FROM users) AS total_users,
            (SELECT COUNT(*) FROM track_publications WHERE published_at >= lo AND published_at < hi) AS published,
            (SELECT COUNT(*) FROM premium_payments WHERE paid_at >= lo AND paid_at < hi) AS purchases,
            (SELECT COALESCE(SUM(stars), 0) FROM premium_payments WHERE paid_at >= lo AND paid_at < hi) AS stars,
            (SELECT COALESCE(SUM(total), 0) FROM download_minutes WHERE minute >= lo AND minute < hi) AS downloads,
            (SELECT COUNT(*) FROM track_complaints WHERE created_at >= lo AND created_at < hi) AS complaints,
            (SELECT COUNT(DISTINCT track_id) FROM track_complaints WHERE created_at >= lo AND created_at < hi) AS reported_tracks,
            (SELECT COUNT(*) FROM track_complaints WHERE resolved_at >= lo AND resolved_at < hi) AS resolved,
            (SELECT COUNT(*) FROM track_complaints WHERE resolved_at IS NULL) AS open_complaints,
            (SELECT COUNT(*) FROM tracks WHERE status = 'pending') AS pending,
            (SELECT COUNT(*) FROM artist_import_items WHERE state IN ('found', 'missing')) AS yandex
        FROM window
    """, (utc_stamp(start), utc_stamp(end))) as cursor:
        stats = await cursor.fetchone()
    if stats["sent"] or stats["started"] >= utc_stamp(end):
        return None
    partial = "\n<i>Учёт публикаций, платежей, скачиваний и жалоб начался в этом периоде.</i>" if stats["started"] > utc_stamp(start) else ""
    return (
        f"<b>FreeDom Music · отчёт за сутки</b>\n{start:%d.%m %H:%M} — {end:%d.%m %H:%M} МСК\n\n"
        f"Новые пользователи: <b>{stats['new_users']}</b> · всего {stats['total_users']}\n"
        f"Опубликовано треков: <b>{stats['published']}</b>\n"
        f"Покупки Premium: <b>{stats['purchases']}</b> · {stats['stars']} ⭐\n"
        f"Скачивания: <b>{stats['downloads']}</b>\n"
        f"Жалобы: <b>{stats['complaints']}</b> · на {stats['reported_tracks']} разных треков\n"
        f"Обработано жалоб за сутки: {stats['resolved']}\n\n"
        f"Сейчас в очередях: обычных {stats['pending']}, Яндекс {stats['yandex']}, открытых жалоб {stats['open_complaints']}"
        f"{partial}"
    )


async def send_report(bot, admin_id: int, end: datetime) -> None:
    report = await build_report(end)
    if report is None:
        return
    await bot.send_message(admin_id, report, request_timeout=20)
    async with db.get_db() as conn:
        await conn.execute("INSERT OR IGNORE INTO admin_daily_reports(period_end) VALUES (?)", (utc_stamp(end),))
        await conn.commit()


async def worker(bot, admin_id: int) -> None:
    moscow = ZoneInfo("Europe/Moscow")
    while True:
        end = period_end(datetime.now(moscow))
        try:
            await send_report(bot, admin_id, end)
        except Exception:
            logger.exception("Не удалось отправить утренний отчёт")
            await asyncio.sleep(300)
            continue
        next_run = end + timedelta(days=1)
        await asyncio.sleep(max(0, (next_run - datetime.now(moscow)).total_seconds()))
