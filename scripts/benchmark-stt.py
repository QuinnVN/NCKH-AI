"""Compare PhoWhisper and Zipformer on private, transcripted recordings."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time
import unicodedata


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


KNOWN_TERMS = ("camera", "tivi", "size", "cỡ", "1a", "3b", "8a")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("recordings_dir", type=Path)
    parser.add_argument("--whisper-bin", type=Path, required=True)
    parser.add_argument("--whisper-model", type=Path, required=True)
    parser.add_argument("--sherpa-model-dir", type=Path, required=True)
    parser.add_argument("--warm-runs", type=int, default=10)
    parser.add_argument("--max-rtf", type=float, default=0.25)
    return parser.parse_args()


def normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text).casefold()
    normalized = "".join(character if character.isalnum() else " " for character in normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    expected = normalize(reference)
    actual = normalize(hypothesis)
    return edit_distance(expected, actual) / max(1, len(expected))


def duration_seconds(path: Path) -> float:
    import wave

    with wave.open(str(path), "rb") as recording:
        return recording.getnframes() / recording.getframerate()


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


async def timed_transcription(transcriber, path: Path) -> tuple[str, float]:
    started = time.perf_counter()
    text = await transcriber.transcribe(path)
    return text, time.perf_counter() - started


class WhisperCliBenchmarkTranscriber:
    """Keep the retired PhoWhisper baseline outside application runtime code."""

    def __init__(self, executable: Path, model: Path) -> None:
        self.executable = executable
        self.model = model

    async def transcribe(self, wav_path: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="phowhisper-benchmark-") as temporary:
            output_prefix = Path(temporary) / "transcript"
            process = await asyncio.create_subprocess_exec(
                str(self.executable),
                "-m", str(self.model),
                "-f", str(wav_path),
                "-otxt", "-of", str(output_prefix),
                "-l", "vi", "-nt", "-np",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            if process.returncode != 0:
                raise RuntimeError("PhoWhisper baseline failed.")
            return output_prefix.with_suffix(".txt").read_text(encoding="utf-8").strip()


async def run(args: argparse.Namespace) -> int:
    os.environ["WHISPER_CPP_BIN"] = str(args.whisper_bin.resolve())
    os.environ["PHOWHISPER_MODEL"] = str(args.whisper_model.resolve())
    os.environ["SHERPA_MODEL_DIR"] = str(args.sherpa_model_dir.resolve())

    from app.sherpa_stt import SherpaOnnxTranscriber

    pairs: list[tuple[Path, Path]] = []
    for transcript in sorted(args.recordings_dir.glob("*-transcript.txt")):
        audio = transcript.with_name(transcript.name.removesuffix("-transcript.txt") + ".wav")
        if audio.is_file() and audio.name.startswith(("lawyer-defense-", "sales-persuasion-")):
            pairs.append((audio, transcript))
    if not pairs:
        raise ValueError("No approved WAV and transcript pairs were found.")

    sherpa = SherpaOnnxTranscriber()
    if not await sherpa.initialize():
        raise RuntimeError(sherpa.last_error)
    whisper = WhisperCliBenchmarkTranscriber(args.whisper_bin, args.whisper_model)

    whisper_cers: list[float] = []
    sherpa_cers: list[float] = []
    all_passed = True
    try:
        for audio, transcript_path in pairs:
            reference = transcript_path.read_text(encoding="utf-8-sig").strip()
            whisper_text, whisper_time = await timed_transcription(whisper, audio)
            sherpa_times: list[float] = []
            sherpa_text = ""
            for _ in range(args.warm_runs):
                sherpa_text, elapsed = await timed_transcription(sherpa, audio)
                sherpa_times.append(elapsed)

            audio_duration = duration_seconds(audio)
            whisper_cer = character_error_rate(reference, whisper_text)
            sherpa_cer = character_error_rate(reference, sherpa_text)
            whisper_cers.append(whisper_cer)
            sherpa_cers.append(sherpa_cer)
            p95 = percentile_95(sherpa_times)
            rtf = p95 / audio_duration
            required_terms = [term for term in KNOWN_TERMS if term in normalize(reference)]
            missing_terms = [term for term in required_terms if term not in normalize(sherpa_text)]
            file_passed = rtf <= args.max_rtf and not missing_terms
            all_passed = all_passed and file_passed

            print(f"File: {audio.name}")
            print(f"  Duration: {audio_duration:.3f}s")
            print(f"  PhoWhisper: {whisper_time:.3f}s, CER {whisper_cer:.4f}")
            print(f"  Zipformer warm p95: {p95:.3f}s, RTF {rtf:.4f}, CER {sherpa_cer:.4f}")
            print(f"  Required terms: {required_terms or 'none'}")
            print(f"  Missing terms: {missing_terms or 'none'}")
            print(f"  Reference: {reference}")
            print(f"  PhoWhisper text: {whisper_text}")
            print(f"  Zipformer text: {sherpa_text}")

        mean_whisper_cer = statistics.fmean(whisper_cers)
        mean_sherpa_cer = statistics.fmean(sherpa_cers)
        quality_passed = mean_sherpa_cer <= mean_whisper_cer
        all_passed = all_passed and quality_passed
        print(f"Mean PhoWhisper CER: {mean_whisper_cer:.4f}")
        print(f"Mean Zipformer CER: {mean_sherpa_cer:.4f}")
        print(f"Result: {'PASS' if all_passed else 'FAIL'}")
        return 0 if all_passed else 1
    finally:
        await sherpa.close()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Benchmark failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
