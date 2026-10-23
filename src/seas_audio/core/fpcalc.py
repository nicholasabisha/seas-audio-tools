"""Locate and invoke the Chromaprint ``fpcalc`` binary.

The old scripts hardcoded ``e:/.TheSeas/Music/Script/similarity/fpcalc.exe`` in
one place and used a relative path in the other. This resolves it properly:
explicit argument -> environment variable -> PyInstaller bundle -> repo ``bin``
-> the package folder -> ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BINARY_NAME = "fpcalc.exe" if os.name == "nt" else "fpcalc"
ENV_OVERRIDE = "SEAS_FPCALC"
DEFAULT_TIMEOUT = 180.0

# Stop fpcalc from flashing a console window when the app runs windowed.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class FpcalcNotFoundError(RuntimeError):
    """Raised when the fpcalc binary cannot be located."""


class FingerprintError(RuntimeError):
    """Raised when fpcalc runs but produces no usable fingerprint."""


@dataclass(frozen=True)
class Fingerprint:
    """One decoded track."""

    path: str
    filename: str
    duration: float
    fp: np.ndarray
    size_mb: float
    kbps: int

    def as_record(self) -> dict:
        return {
            "path": self.path,
            "filename": self.filename,
            "duration": self.duration,
            "fp": self.fp,
            "size_mb": self.size_mb,
            "kbps": self.kbps,
        }


def _candidates(explicit: str | os.PathLike[str] | None = None) -> list[Path]:
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit))

    env_value = os.environ.get(ENV_OVERRIDE)
    if env_value:
        out.append(Path(env_value))

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        out.append(Path(bundle_dir) / BINARY_NAME)

    here = Path(__file__).resolve()
    # <repo>/src/seas_audio/core/fpcalc.py -> <repo>/bin/fpcalc.exe
    out.append(here.parents[3] / "bin" / BINARY_NAME)
    out.append(here.parent / BINARY_NAME)

    on_path = shutil.which("fpcalc") or shutil.which(BINARY_NAME)
    if on_path:
        out.append(Path(on_path))
    return out


def find_fpcalc(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Return the first usable fpcalc path, or raise ``FpcalcNotFoundError``."""
    tried: list[str] = []
    for candidate in _candidates(explicit):
        tried.append(str(candidate))
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue

    detail = "\n  ".join(tried) if tried else "(no candidates)"
    raise FpcalcNotFoundError(
        f"Could not find {BINARY_NAME}. Tried:\n  {detail}\n"
        f"Put it in the project's bin folder or set {ENV_OVERRIDE}."
    )


def fingerprint(
    path: str | os.PathLike[str],
    *,
    fpcalc_path: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Fingerprint:
    """Run fpcalc on one file and parse its raw integer fingerprint."""
    audio = Path(path)
    binary = find_fpcalc(fpcalc_path)

    cmd = [str(binary), "-raw", str(audio)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(f"fpcalc timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise FingerprintError(f"Could not start fpcalc: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise FingerprintError(
            f"fpcalc failed: {stderr or 'exit code ' + str(result.returncode)}"
        )

    duration = 0.0
    raw_fp: list[int] = []
    for line in result.stdout.splitlines():
        if line.startswith("DURATION="):
            try:
                duration = float(line.split("=", 1)[1])
            except ValueError:
                duration = 0.0
        elif line.startswith("FINGERPRINT="):
            payload = line.split("=", 1)[1].strip()
            if payload:
                raw_fp = [int(chunk) for chunk in payload.split(",") if chunk.strip()]

    if not raw_fp:
        raise FingerprintError("fpcalc produced no fingerprint")

    try:
        size_bytes = audio.stat().st_size
    except OSError as exc:
        raise FingerprintError(f"Could not stat file: {exc}") from exc

    kbps = int(size_bytes * 8 / duration / 1000) if duration > 0 else 0

    return Fingerprint(
        path=str(audio),
        filename=audio.name,
        duration=duration,
        fp=np.asarray(raw_fp, dtype=np.uint32),
        size_mb=round(size_bytes / (1024 * 1024), 2),
        kbps=kbps,
    )


def probe(fpcalc_path: str | os.PathLike[str] | None = None) -> str:
    """Human-readable version string, or an error message. Never raises."""
    try:
        binary = find_fpcalc(fpcalc_path)
    except FpcalcNotFoundError as exc:
        return str(exc)
    try:
        result = subprocess.run(
            [str(binary), "-version"],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=20,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"fpcalc found at {binary} but not runnable: {exc}"
    return (result.stdout or result.stderr or "").strip() or f"fpcalc at {binary}"