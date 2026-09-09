import base64
import binascii
import io
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import wave

MAX_RECORDING_BYTES = 8 * 1024 * 1024
ROUND_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
BACKEND_ROOT = Path(__file__).resolve().parent.parent


def get_recordings_directory() -> Path:
    configured = os.environ.get("RECORDINGS_DIR", "recordings").strip() or "recordings"
    directory = Path(configured).expanduser()
    if not directory.is_absolute():
        directory = BACKEND_ROOT / directory
    return directory.resolve()

def decode_defense_recording(data: dict[str, Any]) -> tuple[str, bytes]:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    round_id = payload.get("roundId")
    if not isinstance(round_id, str) or ROUND_ID_PATTERN.fullmatch(round_id) is None:
        raise ValueError("payload.roundId must be a 32-character hexadecimal ID")

    audio = payload.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("payload.audio must be an object")
    if audio.get("mimeType") != "audio/wav":
        raise ValueError("payload.audio.mimeType must be audio/wav")
    if audio.get("encoding") != "pcm_s16le":
        raise ValueError("payload.audio.encoding must be pcm_s16le")
    if type(audio.get("sampleRateHz")) is not int or audio["sampleRateHz"] != 16000:
        raise ValueError("payload.audio.sampleRateHz must be 16000")
    if type(audio.get("channels")) is not int or audio["channels"] != 1:
        raise ValueError("payload.audio.channels must be 1")

    encoded = audio.get("dataBase64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("payload.audio.dataBase64 must be a non-empty string")
    maximum_encoded_length = 4 * ((MAX_RECORDING_BYTES + 2) // 3)
    if len(encoded) > maximum_encoded_length:
        raise ValueError("recording exceeds the 8 MiB limit")

    try:
        wav_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exception:
        raise ValueError("payload.audio.dataBase64 is not valid Base64") from exception
    if len(wav_bytes) > MAX_RECORDING_BYTES:
        raise ValueError("recording exceeds the 8 MiB limit")

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as recording:
            channels = recording.getnchannels()
            sample_width = recording.getsampwidth()
            sample_rate = recording.getframerate()
            frame_count = recording.getnframes()
            compression = recording.getcomptype()
    except (EOFError, wave.Error) as exception:
        raise ValueError("recording is not a valid PCM WAV file") from exception

    if compression != "NONE" or sample_width != 2:
        raise ValueError("recording must use signed 16-bit PCM samples")
    if channels != audio["channels"]:
        raise ValueError("WAV channel count does not match payload.audio.channels")
    if sample_rate != audio["sampleRateHz"]:
        raise ValueError("WAV sample rate does not match payload.audio.sampleRateHz")
    if frame_count <= 0:
        raise ValueError("recording must contain at least one audio frame")

    return round_id.lower(), wav_bytes

def write_defense_recording(round_id: str, wav_bytes: bytes) -> Path:
    directory = get_recordings_directory()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"lawyer-defense-{round_id}.wav"
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.stem}-",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(wav_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    return destination