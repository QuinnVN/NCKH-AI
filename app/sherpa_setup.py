"""Install and validate the pinned Vietnamese Zipformer model bundle."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
from urllib.request import Request, urlopen

from app.config import BACKEND_ROOT


MODEL_NAME = "sherpa-onnx-zipformer-vi-30M-int8-2026-02-09"
MODEL_ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_NAME}.tar.bz2"
)

# Filled from the upstream release archive. Setup rejects any byte that does
# not match these values, so a partial or replaced download is never installed.
MODEL_ARCHIVE_SHA256 = "da8b637947091829d7ee9eda23da2a4ec7caa399233a3f4e34eb719fb2ea6b9b"
MODEL_FILE_SHA256: dict[str, str] = {
    "encoder.int8.onnx": "8ef5286dd427eb108055c2ddc1982aa31e544706072d5ea228729292dacade68",
    "decoder.onnx": "cf2aa385b82c9d5d40cd29c3188af52d0249b3b78f0d4b7eb84ad502d50c7e7f",
    "joiner.int8.onnx": "7311d2e17b810ecea515d79c71cc4668af8759256a06fa01d27047772320c821",
    "tokens.txt": "ca8171f8bbd516c050b627582f2125c8f5f1f6ed967ab41b0fa9aae2cf61b492",
}


@dataclass(frozen=True)
class ModelSetupResult:
    installed: bool
    model_dir: Path


def resolve_model_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = BACKEND_ROOT / path
    return path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_bundle_error(model_dir: Path) -> str | None:
    for name, expected in MODEL_FILE_SHA256.items():
        path = model_dir / name
        if not path.is_file():
            return f"missing {name}"
        if expected == "TO_BE_FILLED" or _sha256(path) != expected:
            return f"invalid SHA-256 for {name}"
    return None


def _download_archive(destination: Path) -> None:
    request = Request(MODEL_ARCHIVE_URL, headers={"User-Agent": "NCKH-AI-setup/1"})
    digest = hashlib.sha256()
    with urlopen(request, timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    if MODEL_ARCHIVE_SHA256 == "TO_BE_FILLED" or digest.hexdigest() != MODEL_ARCHIVE_SHA256:
        raise ValueError("Downloaded model archive failed SHA-256 verification.")


def _extract_verified_files(archive: Path, destination: Path) -> None:
    prefix = f"{MODEL_NAME}/"
    with tarfile.open(archive, mode="r:bz2") as bundle:
        members = {member.name: member for member in bundle.getmembers() if member.isfile()}
        for name, expected in MODEL_FILE_SHA256.items():
            member = members.get(prefix + name)
            if member is None:
                raise ValueError(f"Model archive is missing {name}.")
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"Model archive cannot read {name}.")
            target = destination / name
            digest = hashlib.sha256()
            with target.open("wb") as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                    digest.update(chunk)
            if expected == "TO_BE_FILLED" or digest.hexdigest() != expected:
                raise ValueError(f"Model archive contains an invalid {name}.")


def install_model_bundle(model_dir: Path) -> ModelSetupResult:
    """Install the pinned model atomically at the file level."""

    if model_bundle_error(model_dir) is None:
        return ModelSetupResult(installed=False, model_dir=model_dir)

    model_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="zipformer-setup-", dir=model_dir.parent) as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / f"{MODEL_NAME}.tar.bz2"
        extracted = temporary_path / "model"
        extracted.mkdir()
        try:
            _download_archive(archive)
            _extract_verified_files(archive, extracted)
        except tarfile.TarError as exc:
            raise ValueError("Downloaded model archive is unreadable.") from exc
        model_dir.mkdir(parents=True, exist_ok=True)
        for name in MODEL_FILE_SHA256:
            os.replace(extracted / name, model_dir / name)

    error = model_bundle_error(model_dir)
    if error is not None:
        raise ValueError(f"Installed model validation failed: {error}.")
    return ModelSetupResult(installed=True, model_dir=model_dir)


def main() -> int:
    from app.config import get_settings

    model_dir = resolve_model_dir(get_settings().sherpa_model_dir)
    try:
        result = install_model_bundle(model_dir)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"Zipformer setup failed: {exc}")
        return 1
    state = "Installed" if result.installed else "Already installed"
    print(f"{state}: {result.model_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
