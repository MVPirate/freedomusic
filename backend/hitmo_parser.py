"""Парсер Hitmo (ru.hitmox.net / hitmo.me / hitmo2.ru) для FreeDom Music Bot.

Ищет трек по запросу "Исполнитель - Название", заходит на страницу трека,
находит прямую ссылку на MP3 и скачивает её. Также умеет собирать все
треки со страницы артиста, автоматически переходя по ?page=N.

Возвращает: (file_path | None, err_type | None, source_url | None)
- Успех: (путь_к_mp3, None, url_страницы_трека)
- Ошибка: (None, "error" | "not_found" | "wrong_version", None)
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import logging
import time
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
import urllib3

# Отключаем спам про невалидные SSL-сертификаты в консоли
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# BeautifulSoup опционален — если не установлен, используется regex-фолбэк
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# Базовые домены Hitmo — пробуем по очереди. Зеркала часто меняются: можно переопределить
# без правки кода переменной окружения HITMO_HOSTS="https://a.example,https://b.example"
_DEFAULT_HOSTS = [
    "https://ru.hitmox.net",
    "https://hitmo.me",
    "https://hitmo2.ru",
]
HITMO_HOSTS = [h.strip().rstrip("/") for h in os.getenv("HITMO_HOSTS", "").split(",") if h.strip()] or _DEFAULT_HOSTS
# Если Hitmo режет IP дата-центра (VPS) — укажи прокси: HITMO_PROXY="http://user:pass@host:port"
HITMO_PROXY = os.getenv("HITMO_PROXY", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Регулярки для парсинга (без BeautifulSoup — работает без доп. зависимостей)
RE_TRACK_LINK = re.compile(r'href="(/tracks?/[^"]+|/song/[^"]+)"', re.I)
RE_MP3_LINK = re.compile(r'(?:data-url|href)="([^"]+\.mp3[^"]*)"', re.I)
RE_TITLE_H1 = re.compile(r'<h1[^>]*>(.*?)</h1>', re.I | re.S)
RE_TAGS = re.compile(r'<[^>]+>')
# Для пагинации без bs4: <a ... href="/song/xxx" ...>Текст</a>
RE_SONG_ANCHOR = re.compile(r'<a\s+[^>]*href="(/song/[^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
RE_OG_IMAGE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"', re.I)


def _clean_html(s: str) -> str:
    return RE_TAGS.sub('', s).strip()


def _normalize(s: str) -> str:
    """Приводит строку к нижнему регистру, оставляет только буквы/цифры."""
    return re.sub(r'[^\w\s]', '', s.lower()).strip()


def _clean_artist_from_h1(raw_h1: str) -> str:
    """Очищает H1 от служебных фраз вроде 'Слушать и скачать все песни автора X'."""
    if not raw_h1:
        return "Unknown"
    s = raw_h1.strip()
    # Убираем служебные куски после "автора" и "все песни"
    if "автора" in s:
        s = s.split("автора")[-1].strip()
    if "все песни" in s:
        s = s.split("все песни")[-1].strip()
    # Дополнительные служебные слова
    s = re.sub(r'^(?:скачать|слушать|бесплатно)\s+', '', s, flags=re.IGNORECASE).strip()
    return s or "Unknown"


def _clean_track_title(title_text: str, page_artist: str) -> str:
    """Убирает имя артиста из названия трека (префикс + суффикс)."""
    if not title_text:
        return ""

    # 1. Чистим префикс "Ноггано - ..." / "Ноггано ..."
    if page_artist != "Unknown" and title_text.lower().startswith(page_artist.lower()):
        title_text = title_text[len(page_artist):].strip(" -–—,")

    # 2. Чистим формат "Ноггано - Название"
    if " - " in title_text:
        left, right = title_text.split(" - ", 1)
        if page_artist != "Unknown" and _normalize(left) == _normalize(page_artist):
            title_text = right.strip()

    # 3. Чистим СУФФИКС — если артист склеился в конце (баг с parent.text)
    if page_artist != "Unknown":
        # Если "Ноггано, GUF" в конце — режем всё после (включая запятую и фитов)
        suffix_re = re.compile(rf"\s+{re.escape(page_artist)}\b.*$", re.IGNORECASE)
        title_text = suffix_re.sub("", title_text).strip()

        # Финальная зачистка: если артист в самом конце
        suffix_words = re.compile(rf"\s+{re.escape(page_artist)}\s*$", re.IGNORECASE)
        title_text = suffix_words.sub("", title_text).strip()

    return title_text.strip(" -–—,")


# ============================================================
# УСТОЙЧИВЫЙ ПОИСК: очистка названий, варианты запросов, сравнение
# (эти же функции использует downloader.py для SoundCloud)
# ============================================================
_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})

# Разделители артистов: запятая, &, /, +, feat., ft., x/х, "и", and ...
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|/|\+|×|\bfeat\.?(?=\s|$)|\bft\.?(?=\s|$)|\bfeaturing\b|\bvs\.?(?=\s|$)"
    r"|\bprod\.?(?=\s|$)|\bwith\b|(?<=\s)[xх](?=\s)|(?<=\s)и(?=\s)|(?<=\s)and(?=\s))\s*",
    re.IGNORECASE,
)
# Мусор в названиях: (feat. ...), [Remix], - Remastered 2011, "при уч. ..." и т.п.
_BRACKETS_RE = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")
_VERSION_TAIL_RE = re.compile(
    r"\s+[-–—]\s+[^-–—]*\b(?:remaster(?:ed)?|radio edit|version|edit|remix|mix|live|acoustic|instrumental|from)\b[^-–—]*$",
    re.IGNORECASE,
)
_FEAT_TAIL_RE = re.compile(r"\s*(?:[-–—]\s*)?\b(?:feat\.?|ft\.?|featuring|при уч(?:астии|\.)?|prod\.?)\s+.*$", re.IGNORECASE)
_JUNK_WORDS_RE = re.compile(r"\b(?:official|audio|video|lyric video|lyrics|explicit|mp3|320\s*kbps|hq|hd)\b", re.IGNORECASE)


def norm(s: str) -> str:
    """lower + ё→е + только буквы/цифры/пробелы + схлопнутые пробелы."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def translit(s: str) -> str:
    return norm(s).translate(_TRANSLIT)


def split_artists(artist: str) -> list[str]:
    """'Каспийский Груз, Rigos & SLIMUS' -> ['Каспийский Груз', 'Rigos', 'SLIMUS']."""
    parts = [p.strip(" .-–—") for p in _ARTIST_SPLIT_RE.split(artist or "")]
    return list(dict.fromkeys(p for p in parts if p))


def clean_title(title: str) -> str:
    """Название без скобок, feat-хвостов, версий и знаков препинания ('18+' -> '18')."""
    t = title or ""
    t = _BRACKETS_RE.sub("", t)
    t = _VERSION_TAIL_RE.sub("", t)
    t = _FEAT_TAIL_RE.sub("", t)
    t = _JUNK_WORDS_RE.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t:
        return t
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title or "")).strip()


def first_words(s: str, n: int = 3) -> str:
    return " ".join((s or "").split()[:n])


def build_search_queries(artist: str, title: str, limit: int = 5) -> list[str]:
    """Варианты поисковых запросов от точного к мягкому (без дублей)."""
    artists = [a for a in split_artists(artist) if norm(a) != "unknown"]
    main = artists[0] if artists else ""
    ctitle = clean_title(title)
    short = first_words(ctitle, 3) if len(ctitle.split()) >= 4 else ""

    raw: list[str] = []
    if main:
        raw.append(f"{main} {ctitle}")                      # 1) главный артист + чистое название
    if len(artists) > 1:
        raw.append(f"{' '.join(artists)} {ctitle}")         # 2) все артисты + название
    if main and short:
        raw.append(f"{main} {short}")                       # 3) артист + первые слова названия
    if len(artists) > 1:
        raw.append(f"{artists[1]} {ctitle}")                # 4) фит-артист + название
    if len(norm(ctitle)) >= 5:
        raw.append(ctitle)                                  # 5) только название (если оно не слишком короткое)
    if not raw:
        raw.append(ctitle or title or "")

    out, seen = [], set()
    for q in raw:
        q = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", q)).strip()
        key = norm(q)
        if q and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:limit]


def _has_word(hay: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay) is not None


def _segments(raw: str) -> list[str]:
    parts = re.split(r"\s[-–—|:•]\s|[()\[\]]|\s{2,}", raw or "")
    return [n for n in (norm(p) for p in parts) if n]


def _fuzzy_in(needle: str, segs: list[str], thr: float) -> bool:
    return any(SequenceMatcher(None, needle, seg).ratio() >= thr for seg in segs)


def match_track(candidate_text: str, artist: str, title: str) -> bool:
    """Подходит ли кандидат (заголовок страницы / заголовок трека) под запрошенный артист+название.

    Требуем совпадение И названия, И (любого из) артистов — иначе короткие названия
    вроде '18' или 'Тюльпан' цепляют чужие треки.
    """
    cand = norm(candidate_text)
    if not cand:
        return False
    cand_tr = translit(cand)
    segs = _segments(candidate_text)
    segs_tr = [translit(s) for s in segs]

    # --- название ---
    ctitle = norm(clean_title(title))
    t_norms = [n for n in dict.fromkeys([ctitle, norm(title)]) if n]
    words = ctitle.split()
    if len(words) >= 4:
        t_norms.append(" ".join(words[:3]))
    title_ok = any(
        _has_word(cand, t) or _has_word(cand_tr, translit(t))
        or _fuzzy_in(t, segs, 0.84) or _fuzzy_in(translit(t), segs_tr, 0.84)
        for t in t_norms
    )
    if not title_ok:
        return False

    # --- артист ---
    a_norms = [n for n in dict.fromkeys([norm(a) for a in split_artists(artist)] + [norm(artist)]) if n]
    if not a_norms or norm(artist) == "unknown":
        return True
    return any(
        _has_word(cand, a) or _has_word(cand_tr, translit(a))
        or _fuzzy_in(a, segs, 0.82) or _fuzzy_in(translit(a), segs_tr, 0.82)
        for a in a_norms
    )


# ============================================================
# ОТСЕВ «НЕ ТЕХ» ВЕРСИЙ: по маркерам в названии и по длительности
# ============================================================
# '*' на конце — основа слова (ускорен* ловит «ускоренная», «ускоренный»...).
BAD_WORDS = [
    '8d', '8 d', '8-d', '8d audio', 'echo', 'эхо', 'cover', 'instrumental', 'минус', 'кавер',
    'slowed', 'reverb', 'speed up', 'sped up', 'speedup', 'nightcore', 'найткор',
    'ускорен*', 'замедлен*', 'remix', 'ремикс*', 'mashup', 'karaoke', 'караоке', 'live',
]


def _compile_bad_patterns():
    pats = []
    for bad in BAD_WORDS:
        core = norm(bad.rstrip("*"))
        if core:
            tail = r"\w*" if bad.endswith("*") else r"(?!\w)"
            pats.append(re.compile(rf"(?<!\w){re.escape(core)}{tail}"))
    return pats


_BAD_PATTERNS = _compile_bad_patterns()


def has_bad_marker(text: str, requested_title: str = "") -> bool:
    """True, если в тексте есть маркер другой версии (slowed, 8D, remix, live...),
    которого нет в самом запрошенном названии (трек 'Echo' или '... (Remix)' не режем)."""
    t, req = norm(text), norm(requested_title)
    return any(p.search(t) and not p.search(req) for p in _BAD_PATTERNS)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# Допуск по длительности: max(8 сек, 5% от ожидаемой). Замедленные/ускоренные версии обычно
# отличаются на 10-30%, поэтому проходят только «родные» версии. Настраивается через .env.
DURATION_TOLERANCE_PCT = _env_float("DURATION_TOLERANCE_PCT", 0.05)
DURATION_TOLERANCE_MIN = _env_float("DURATION_TOLERANCE_MIN", 8.0)


def duration_ok(actual: float, expected: float) -> bool:
    """Совпадает ли длительность. Нет данных (0/None) — не режем."""
    if not actual or not expected:
        return True
    return abs(actual - expected) <= max(DURATION_TOLERANCE_MIN, expected * DURATION_TOLERANCE_PCT)


def mp3_length(path) -> float:
    try:
        from mutagen.mp3 import MP3
        return float(MP3(str(path)).info.length)
    except Exception:
        return 0.0


HITMO_MAX_DOWNLOADS = 3   # сколько кандидатов максимум скачиваем на один трек (если длительность не сошлась)
HITMO_MAX_PAGE_FETCHES = 14  # сколько страниц треков максимум открываем на один трек


# ============================================================
# СЕТЬ: сессия, здоровье зеркал, диагностика
# ============================================================
_HOST_DOWN_UNTIL: dict[str, float] = {}
_HOST_COOLDOWN = 180  # сек. не трогаем зеркало после сбоя (иначе каждый трек ждёт таймауты)
_BLOCK_MARKERS = ("just a moment", "cf-challenge", "captcha", "ddos-guard", "attention required", "access denied")


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if HITMO_PROXY:
        s.proxies.update({"http": HITMO_PROXY, "https": HITMO_PROXY})
    return s


def _get(session: requests.Session, url: str, *, timeout=(6, 15), stream: bool = False):
    """GET без исключений: (response | None, причина_сбоя | None)."""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True, verify=False, stream=stream)
        return r, None
    except requests.exceptions.RequestException as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def _alive_hosts() -> list[str]:
    now = time.time()
    return [h for h in HITMO_HOSTS if _HOST_DOWN_UNTIL.get(h, 0) <= now]


def _mark_down(host: str, why: str) -> None:
    if _HOST_DOWN_UNTIL.get(host, 0) <= time.time():
        logger.warning(f"[Hitmo] {host} не отвечает нормально ({why}) — пауза {_HOST_COOLDOWN} c")
    _HOST_DOWN_UNTIL[host] = time.time() + _HOST_COOLDOWN


def _looks_blocked(text: str) -> bool:
    low = (text or "")[:4000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


RE_CAND_ANCHOR = re.compile(r'<a\s+[^>]*href="([^"]*/(?:song|tracks?)/[^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_BAD_CAND_PATH = re.compile(r"/(?:tracks?)/?$", re.I)


def _extract_candidates(page: str, host: str) -> dict[str, str]:
    """{url: текст_ссылки} со страницы поиска (порядок сохраняется)."""
    out: dict[str, str] = {}
    base = host.rstrip("/") + "/"
    for m in RE_CAND_ANCHOR.finditer(page):
        url = urljoin(base, m.group(1))
        if _BAD_CAND_PATH.search(urlparse(url).path):
            continue
        text = " ".join(_clean_html(m.group(2)).split())[:200]
        if len(text) > len(out.get(url, "")):
            out[url] = text
    for rel in RE_TRACK_LINK.findall(page):
        url = urljoin(base, rel)
        if not _BAD_CAND_PATH.search(urlparse(url).path):
            out.setdefault(url, "")
    return out


def _rank_candidates(cands: dict[str, str], artist: str, title: str) -> list[str]:
    main = (split_artists(artist) or [""])[0]
    target = norm(f"{main} {clean_title(title)}")

    def score(item):
        text = norm(item[1])
        return SequenceMatcher(None, target, text).ratio() if text else 0.35

    return [u for u, _ in sorted(cands.items(), key=score, reverse=True)]


def _page_signature(page: str, with_description: bool = True) -> str:
    """Всё, где на странице трека может быть 'Артист - Название': h1, <title>, og/meta.
    with_description=False — только заголовки (для поиска маркеров версий, описание шумит)."""
    parts: list[str] = []
    m = RE_TITLE_H1.search(page)
    if m:
        parts.append(_clean_html(m.group(1)))
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if m:
        parts.append(_clean_html(m.group(1)))
    props = ("og:title", "og:description", "description") if with_description else ("og:title",)
    for prop in props:
        m = re.search(rf'<meta\s+(?:property|name)="{prop}"\s+content="([^"]*)"', page, re.I)
        if m:
            parts.append(m.group(1))
    return " | ".join(" ".join(unescape(p).split()) for p in parts if p)


def _iter_matches(session: requests.Session, artist: str, title: str, state: dict):
    """Генератор подтверждённых кандидатов (url, html_страницы) от лучшего к худшему.

    Нужен именно генератор: у одного трека на Hitmo бывает несколько загрузок (оригинал, slowed,
    sped up, ремиксы). Если первая не прошла по длительности — вызывающий код берёт следующую.
    После исчерпания в state['status'] лежит 'not_found' (сайт ответил, подходящего нет)
    или 'network' (сайт недоступен/блокирует).
    """
    reached = False
    seen: set[str] = set()
    fetched = 0

    for q in build_search_queries(artist, title):
        logger.info(f"[Hitmo] Пробую поиск по запросу: '{q}'")
        hosts = _alive_hosts()
        if not hosts:
            logger.warning("[Hitmo] все зеркала в паузе после сбоев — пропускаю поиск")
            state["status"] = "not_found" if reached else "network"
            return

        responded = False
        for host in hosts:
            r, why = _get(session, f"{host}/search?q={quote(q)}")
            if r is None or r.status_code != 200:
                _mark_down(host, why or f"HTTP {r.status_code}")
                continue
            cands = _extract_candidates(r.text, host)
            if not cands and _looks_blocked(r.text):
                _mark_down(host, "антибот-страница (captcha/challenge)")
                continue

            responded = reached = True
            if not cands:
                break  # сайт ответил, но по этому запросу пусто — пробуем следующий вариант запроса

            for url in _rank_candidates(cands, artist, title):
                if url in seen:
                    continue
                if fetched >= HITMO_MAX_PAGE_FETCHES:
                    state["status"] = "not_found"
                    return
                if cands[url] and has_bad_marker(cands[url], title):
                    continue  # в тексте ссылки уже видно slowed/remix/live — страницу не открываем
                rr, _why = _get(session, url)
                fetched += 1
                if rr is None or rr.status_code != 200:
                    continue
                seen.add(url)
                if has_bad_marker(_page_signature(rr.text, with_description=False), title):
                    logger.info(f"[Hitmo] Пропускаю другую версию: {url}")
                    continue
                sig = _page_signature(rr.text)
                if match_track(sig, artist, title):
                    logger.info(f"[Hitmo] Совпадение: {sig[:90]} -> {url}")
                    yield url, rr.text
            break  # зеркало живое — другие зеркала для этого запроса не нужны

        if not responded:
            state["status"] = "not_found" if reached else "network"
            return  # все зеркала легли — следующие варианты запроса бессмысленны
        time.sleep(random.uniform(0.3, 0.8))

    state["status"] = "not_found" if reached else "network"


def _mp3_from_page(page: str, base_url: str) -> Optional[str]:
    links = RE_MP3_LINK.findall(page)
    if links:
        return urljoin(base_url, links[0])
    m = re.search(r'data-url="([^"]+)"', page)
    if m and ("download" in m.group(1).lower() or ".mp3" in m.group(1).lower()):
        return urljoin(base_url, m.group(1))
    return None


def _extract_mp3_url(session: requests.Session, track_page_url: str) -> Optional[str]:
    """Со страницы трека достаёт прямую ссылку на mp3 (оставлено для обратной совместимости)."""
    r, why = _get(session, track_page_url)
    if r is None or r.status_code != 200:
        return None
    return _mp3_from_page(r.text, track_page_url)


def _embed_hitmo_cover(session: requests.Session, page: str, dest: Path) -> None:
    """Вшивает обложку из og:image страницы Hitmo (JPEG <=320px)."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC, ID3NoHeaderError
        from PIL import Image
        from io import BytesIO

        og_match = RE_OG_IMAGE.search(page)
        if not og_match:
            return
        cover_url = og_match.group(1)
        if cover_url.startswith("//"): cover_url = "https:" + cover_url

        img_resp = session.get(cover_url, headers=HEADERS, timeout=10, verify=False)
        if img_resp.status_code == 200:
            img = Image.open(BytesIO(img_resp.content)).convert("RGB")
            img.thumbnail((320, 320))
            out = BytesIO()
            img.save(out, format="JPEG", quality=85)

            try:
                audio = MP3(dest, ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(dest)
                audio.add_tags()
                audio = MP3(dest, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()

            audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=out.getvalue()))
            audio.save()
            logger.info(f"[Hitmo] ✅ Обложка вшита в {dest.name}")
    except Exception as e:
        logger.error(f"[Hitmo] Не удалось вшить обложку: {e}")


def _download_candidate(
    session: requests.Session, track_url: str, page: str, dest: Path, duration: int
) -> tuple[bool, Optional[str]]:
    """Качает mp3 одного кандидата. -> (успех, err): err = 'error' | 'wrong_version'."""
    try:
        mp3_url = _mp3_from_page(page, track_url)
        if not mp3_url:
            logger.info(f"[Hitmo] На странице {track_url} не найдена mp3-ссылка")
            return False, "error"

        logger.info(f"[Hitmo] Скачиваю: {mp3_url}")
        with session.get(mp3_url, headers=HEADERS, timeout=(8, 120), stream=True, verify=False) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        if not dest.exists() or dest.stat().st_size < 50_000:
            logger.warning(f"[Hitmo] Файл слишком маленький или пустой: {dest}")
            dest.unlink(missing_ok=True)
            return False, "error"

        # Сверка длительности с Яндексом: отсеивает slowed/sped up/удлинённые версии и превью
        if duration:
            real = mp3_length(dest)
            if not duration_ok(real, duration):
                logger.warning(f"[Hitmo] Длительность не совпала ({real:.0f}с вместо ~{duration}с), беру другого кандидата: {track_url}")
                dest.unlink(missing_ok=True)
                return False, "wrong_version"

        _embed_hitmo_cover(session, page, dest)
        logger.info(f"[Hitmo] ✅ Скачано: {dest} ({dest.stat().st_size} байт)")
        return True, None

    except Exception as e:
        logger.error(f"[Hitmo] Ошибка скачивания {track_url}: {e}")
        try: dest.unlink(missing_ok=True)
        except Exception: pass
        return False, "error"


def _sync_download_from_hitmo(
    artist: str, title: str, uploads_dir: Path, duration: int = 0
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Синхронная функция — вызывается из asyncio.to_thread."""
    uploads_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(
        c for c in f"{artist} - {title}" if c.isalnum() or c in " -_"
    ).strip() or "track"
    dest = uploads_dir / f"hitmo_{safe_name}.mp3"

    session = _make_session()
    state: dict = {}
    tried = wrong = 0

    try:
        for track_url, page in _iter_matches(session, artist, title, state):
            tried += 1
            ok, err = _download_candidate(session, track_url, page, dest, duration)
            if ok:
                return str(dest), None, track_url
            if err == "wrong_version":
                wrong += 1
            if tried >= HITMO_MAX_DOWNLOADS:
                break
    except Exception as e:
        logger.error(f"[Hitmo] Ошибка поиска/скачивания {artist} - {title}: {e}")
        try: dest.unlink(missing_ok=True)
        except Exception: pass
        return None, "error", None

    if tried == 0:
        if state.get("status") == "network":
            # Раньше это тоже превращалось в "Не найдено" — теперь отличаем блокировку от отсутствия трека
            logger.warning(f"[Hitmo] Сайт недоступен/блокирует запросы: {artist} - {title}")
            return None, "error", None
        logger.info(f"[Hitmo] Не найдено: {artist} - {title}")
        return None, "not_found", None

    logger.info(f"[Hitmo] Кандидатов проверено: {tried}, не подошли по длительности: {wrong} ({artist} - {title})")
    return None, ("wrong_version" if wrong else "error"), None


# ============================================================
# УМНАЯ ПАГИНАЦИЯ: собираем все треки со страницы артиста
# ============================================================
def _sync_parse_artist_page(url: str) -> list[dict]:
    """Парсит ВСЕ страницы артиста, автоматически переходя по ?page=N.

    Возвращает список словарей {artist, title, url}.
    """
    session = _make_session()
    all_tracks: list[dict] = []

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # Начинаем с указанной страницы (по умолчанию 1)
    try:
        start_page = int(query.get('page', [1])[0])
    except (ValueError, TypeError):
        start_page = 1
    max_pages = 30  # Защита от бесконечности

    for page in range(start_page, start_page + max_pages):
        query['page'] = [str(page)]
        new_query = urlencode(query, doseq=True)
        current_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                                  parsed.params, new_query, parsed.fragment))

        logger.info(f"[Hitmo Artist] Парсинг страницы: {current_url}")
        try:
            r = session.get(current_url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code != 200:
                logger.warning(f"[Hitmo Artist] Страница {page} вернула {r.status_code}")
                break

            page_tracks: list[dict] = []

            if BS4_AVAILABLE:
                soup = BeautifulSoup(r.text, 'html.parser')

                # 1. Берём имя артиста из заголовка страницы <h1>
                h1_tag = soup.find('h1')
                page_artist = _clean_artist_from_h1(h1_tag.text.strip() if h1_tag else "")
                
                # Если H1 пустой или Unknown — ищем в <title>
                if page_artist == "Unknown":
                    title_tag = soup.find('title')
                    if title_tag:
                        title_text = title_tag.text.strip()
                        if " - " in title_text:
                            page_artist = title_text.split(" - ")[0].strip()
                        elif "Скачать песни" in title_text:
                            page_artist = title_text.split("Скачать песни")[-1].split("бесплатно")[0].strip()

                logger.info(f"[Hitmo Artist] Имя артиста: '{page_artist}'")

                for a_tag in soup.find_all('a', href=re.compile(r'/(song|track)/')):
                    track_url = urljoin(current_url, a_tag['href'])

                    # 2. Берём название из текста ссылки (или родителя)
                    title_text = a_tag.text.strip()
                    if not title_text:
                        parent = a_tag.find_parent(['div', 'li'])
                        if parent:
                            title_text = parent.text.strip()

                    title_text = ' '.join(title_text.split())
                    if not title_text:
                        continue

                    # 3. Чистим префикс/суффикс артиста
                    title_text = _clean_track_title(title_text, page_artist)

                    if title_text:
                        page_tracks.append({
                            "artist": page_artist,
                            "title": title_text,
                            "url": track_url,
                        })
            else:
                # Regex-фолбэк, если bs4 не установлен
                h1_match = RE_TITLE_H1.search(r.text)
                page_artist = _clean_artist_from_h1(_clean_html(h1_match.group(1)) if h1_match else "")
                logger.info(f"[Hitmo Artist] Имя артиста из H1 (regex): '{page_artist}'")

                for m in RE_SONG_ANCHOR.finditer(r.text):
                    rel_url = m.group(1)
                    raw_text = m.group(2)
                    track_url = urljoin(current_url, rel_url)
                    title_text = _clean_html(raw_text)
                    title_text = ' '.join(title_text.split())
                    if not title_text:
                        continue

                    title_text = _clean_track_title(title_text, page_artist)

                    if title_text:
                        page_tracks.append({
                            "artist": page_artist,
                            "title": title_text,
                            "url": track_url,
                        })

            if not page_tracks:
                logger.info(f"[Hitmo Artist] Страница {page} не содержит треков. Конец.")
                break

            all_tracks.extend(page_tracks)
            time.sleep(1.5)  # Пауза, чтобы Hitmo не забанил

        except Exception as e:
            logger.error(f"[Hitmo Artist] Ошибка на странице {page}: {e}")
            break

    # Убираем дубликаты по URL
    unique_tracks = {t['url']: t for t in all_tracks}.values()
    logger.info(f"[Hitmo Artist] Всего найдено {len(unique_tracks)} уникальных треков.")
    return list(unique_tracks)


async def parse_artist_page_hitmo(url: str) -> list[dict]:
    """Асинхронная обёртка над _sync_parse_artist_page."""
    return await asyncio.to_thread(_sync_parse_artist_page, url)


async def download_from_hitmo(
    artist: str, title: str, uploads_dir: Path, duration: int = 0
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Асинхронная обёртка. Возвращает (file_path, err_type, source_url).

    err_type: 'not_found' (сайт ответил, подходящего трека нет) | 'error' (сайт недоступен/блокирует/сбой)
              | 'wrong_version' (кандидаты были, но ни один не прошёл по длительности).
    """
    return await asyncio.to_thread(_sync_download_from_hitmo, artist, title, uploads_dir, duration)