import asyncio
import os
import shutil
import subprocess
import tempfile

import httpx

from config import YANDEX_API_KEY, YANDEX_FOLDER_ID, YANDEX_STT_URL

# Синхронное распознавание Yandex SpeechKit: максимум 30 секунд и 1 МБ.
MAX_SYNC_SECONDS = 30
CHUNK_SECONDS = 25


class STTError(Exception):
    pass


def _ffmpeg() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _split_sync(data: bytes, seconds: int) -> list[bytes]:
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        raise STTError(
            f"Голосовое длиннее {MAX_SYNC_SECONDS} секунд, а ffmpeg не найден. "
            "Установите imageio-ffmpeg или пришлите голосовое короче 30 секунд."
        )
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "input.ogg")
        with open(src, "wb") as f:
            f.write(data)
        pattern = os.path.join(td, "part_%03d.mp3")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", src,
            "-ar", "16000", "-ac", "1",
            "-c:a", "libmp3lame", "-b:a", "48k",
            "-f", "segment", "-segment_time", str(seconds),
            pattern,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise STTError("ffmpeg не успел обработать голосовое.")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "ignore")[:300]
            raise STTError(f"ffmpeg error: {err}")
        parts = []
        for name in sorted(os.listdir(td)):
            if name.startswith("part_") and name.endswith(".mp3"):
                with open(os.path.join(td, name), "rb") as f:
                    parts.append(f.read())
        if not parts:
            raise STTError("ffmpeg не создал ни одного фрагмента.")
        return parts


async def _recognize_one(data: bytes, audio_format: str) -> str:
    params = {"lang": "ru-RU", "format": audio_format, "topic": "general"}
    if YANDEX_FOLDER_ID:
        params["folderId"] = YANDEX_FOLDER_ID
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                YANDEX_STT_URL, params=params, headers=headers, content=data
            )
    except httpx.HTTPError as e:
        raise STTError(f"Сетевая ошибка: {e}")
    try:
        payload = r.json()
    except ValueError:
        raise STTError(f"HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code != 200 or payload.get("error_code"):
        msg = payload.get("error_message") or payload.get("message") or r.text[:300]
        raise STTError(f"HTTP {r.status_code}: {msg}")
    return (payload.get("result") or "").strip()


async def recognize(data: bytes, duration: int = 0) -> str:
    """Распознаёт голосовое Telegram (OGG/Opus) через Yandex SpeechKit."""
    if not YANDEX_API_KEY:
        raise STTError("YANDEX_API_KEY не задан в .env")
    if not data:
        raise STTError("Пустой аудиофайл.")

    too_long = duration > MAX_SYNC_SECONDS - 1 or len(data) > 1_000_000
    if not too_long:
        return await _recognize_one(data, "oggopus")

    chunks = await asyncio.to_thread(_split_sync, data, CHUNK_SECONDS)
    parts = []
    for chunk in chunks:
        text = await _recognize_one(chunk, "mp3")
        if text:
            parts.append(text)
    return " ".join(parts).strip()
