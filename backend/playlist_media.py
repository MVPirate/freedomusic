import asyncio
import logging
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from mutagen import File as MutagenFile

from backend.database import set_playlist_trailer


TRAILER_MAX_BYTES = 3 * 1024 * 1024
TRAILER_DIR = Path(__file__).resolve().parent.parent / "uploads" / "trailers"
TRAILER_EXTENSIONS = {
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/ogg": ".ogg",
    "audio/opus": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
}
logger = logging.getLogger(__name__)


def _save_trailer(content: bytes, playlist_id: int) -> Path:
    try:
        audio = MutagenFile(BytesIO(content))
        extension = next((TRAILER_EXTENSIONS[mime] for mime in audio.mime if mime in TRAILER_EXTENSIONS), None)
        duration = audio.info.length
    except Exception as exc:
        raise ValueError("Выберите аудиофайл MP3, M4A, OGG или WAV.") from exc
    if not extension:
        raise ValueError("Выберите аудиофайл MP3, M4A, OGG или WAV.")
    if not 0 < duration <= 30.05:
        raise ValueError("Трейлер должен длиться не более 30 секунд.")
    TRAILER_DIR.mkdir(parents=True, exist_ok=True)
    target = TRAILER_DIR / f"trailer_{playlist_id}_{uuid4().hex}{extension}"
    target.write_bytes(content)
    return target


async def upload_playlist_trailer(playlist_id: int, user_id: int, content: bytes) -> None:
    if len(content) > TRAILER_MAX_BYTES:
        raise ValueError("Трейлер должен быть не больше 3 МБ.")
    target = await asyncio.to_thread(_save_trailer, content, playlist_id)
    try:
        previous = await set_playlist_trailer(playlist_id, user_id, str(target))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if previous and Path(previous).resolve().parent == TRAILER_DIR:
        try:
            Path(previous).unlink(missing_ok=True)
        except OSError:
            logger.warning("Не удалось удалить предыдущий трейлер плейлиста %s", playlist_id)
