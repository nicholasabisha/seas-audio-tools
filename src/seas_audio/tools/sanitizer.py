"""Rewrite MP3 cover art to baseline JPEG and force ID3v2.3.

Car stereos and TVs commonly choke on progressive JPEG artwork and on ID3v2.4
frames. This normalises both, and - unlike the original script - it does not
rewrite a file that is already correct.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from PIL import Image

from seas_audio.core.config import SANITIZABLE_EXTENSIONS

TARGET_ID3_VERSION = (2, 3)
TARGET_ID3_VERSION_TEXT = "2.3.0"
JPEG_QUALITY = 95

STATUS_SANITIZED = "sanitized"
STATUS_CLEAN = "clean"
STATUS_NO_ART = "no-art"
STATUS_DRY_RUN = "would-change"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

ProgressCallback = Callable[[int, int, str], None]


def normalise_artwork(data: bytes) -> tuple[bytes, bool, str]:
    """Coerce embedded artwork to a baseline JPEG.

    Returns ``(jpeg_bytes, changed, note)``. ``changed`` is ``False`` when the
    payload is already baseline JPEG and can be left untouched - that is what
    keeps already-clean libraries from being rewritten byte for byte.
    """
    with Image.open(io.BytesIO(data)) as image:
        format_name = (image.format or "unknown").upper()
        is_jpeg = format_name == "JPEG"
        is_progressive = bool(image.info.get("progressive", 0))

        if is_jpeg and not is_progressive:
            return data, False, "already baseline JPEG"

        # convert('RGB') drops palette/alpha, which is what JPEG cannot store.
        buffer = io.BytesIO()
        image.convert("RGB").save(
            buffer,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=False,
            progressive=False,
        )
        note = (
            "converted from progressive JPEG"
            if is_jpeg
            else f"converted from {format_name} to baseline JPEG"
        )
        return buffer.getvalue(), True, note


@dataclass(frozen=True)
class SanitizeResult:
    path: Path
    status: str
    message: str = ""
    art_reencoded: bool = False
    mime_fixed: bool = False
    version_before: str = ""
    version_after: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status != STATUS_ERROR


def _version_text(tags: ID3) -> str:
    try:
        return ".".join(str(part) for part in tags.version)
    except Exception:  # pragma: no cover - defensive against odd tag objects
        return "unknown"


def sanitize_file(
    path: str | Path,
    *,
    dry_run: bool = False,
    backup: bool = False,
) -> SanitizeResult:
    """Normalise one file. Never raises for per-file problems."""
    audio_path = Path(path)

    if audio_path.suffix.lower() not in SANITIZABLE_EXTENSIONS:
        return SanitizeResult(
            audio_path, STATUS_SKIPPED, "only MP3 files carry ID3 tags"
        )
    if not audio_path.is_file():
        return SanitizeResult(audio_path, STATUS_ERROR, "file not found")

    try:
        audio = MP3(str(audio_path), ID3=ID3)
        try:
            tags = audio.tags
        except ID3NoHeaderError:
            tags = None
        if tags is None:
            audio.add_tags()
            tags = audio.tags
    except Exception as exc:
        return SanitizeResult(audio_path, STATUS_ERROR, f"could not read tags: {exc}")

    version_before = _version_text(tags)
    notes: list[str] = []
    reencoded = False
    mime_fixed = False
    changed = False

    art_keys = [key for key in tags.keys() if key.startswith("APIC")]
    for key in art_keys:
        frame = tags[key]
        try:
            payload, needs_rewrite, note = normalise_artwork(frame.data)
        except Exception as exc:
            notes.append(f"{key}: unreadable artwork ({exc})")
            continue

        if needs_rewrite:
            frame.data = payload
            reencoded = True
            changed = True
        notes.append(f"{key}: {note}")

        if frame.mime != "image/jpeg":
            frame.mime = "image/jpeg"
            mime_fixed = True
            changed = True
            notes.append(f"{key}: mime corrected to image/jpeg")

    version_needs_fix = tags.version[:2] != TARGET_ID3_VERSION
    write_needed = changed or version_needs_fix

    if not art_keys:
        if not write_needed:
            return SanitizeResult(
                audio_path, STATUS_NO_ART, "no artwork, already ID3v2.3", version_before=version_before
            )
    elif not write_needed:
        return SanitizeResult(
            audio_path,
            STATUS_CLEAN,
            "artwork already baseline JPEG, tags already ID3v2.3",
            version_before=version_before,
            notes=tuple(notes),
        )

    if dry_run:
        reasons = []
        if reencoded:
            reasons.append("re-encode artwork")
        if mime_fixed:
            reasons.append("fix MIME type")
        if version_needs_fix:
            reasons.append(f"upgrade ID3v{version_before} to 2.3.0")
        return SanitizeResult(
            audio_path,
            STATUS_DRY_RUN,
            ", ".join(reasons) or "would rewrite tags",
            art_reencoded=reencoded,
            mime_fixed=mime_fixed,
            version_before=version_before,
            notes=tuple(notes),
        )

    if backup:
        backup_path = audio_path.with_suffix(audio_path.suffix + ".bak")
        try:
            if not backup_path.exists():
                shutil.copy2(audio_path, backup_path)
        except OSError as exc:
            return SanitizeResult(
                audio_path, STATUS_ERROR, f"backup failed, file left untouched: {exc}",
                version_before=version_before,
            )

    try:
        audio.save(v2_version=3)
    except Exception as exc:
        return SanitizeResult(
            audio_path, STATUS_ERROR, f"could not write tags: {exc}", version_before=version_before
        )

    status = STATUS_SANITIZED if art_keys else STATUS_NO_ART
    message = "rewrote tags as ID3v2.3"
    if reencoded:
        message = "artwork re-encoded to baseline JPEG, tags rewritten as ID3v2.3"

    return SanitizeResult(
        audio_path,
        status,
        message,
        art_reencoded=reencoded,
        mime_fixed=mime_fixed,
        version_before=version_before,
        version_after=TARGET_ID3_VERSION_TEXT,
        notes=tuple(notes),
    )


def sanitize_many(
    files: Iterable[str | Path],
    *,
    dry_run: bool = False,
    backup: bool = False,
    on_progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[SanitizeResult]:
    """Sanitize a batch, reporting progress and honouring cancellation."""
    paths = [Path(p) for p in files]
    total = len(paths)
    results: list[SanitizeResult] = []

    for index, path in enumerate(paths, start=1):
        if should_cancel is not None and should_cancel():
            break
        result = sanitize_file(path, dry_run=dry_run, backup=backup)
        results.append(result)
        if on_progress is not None:
            on_progress(index, total, path.name)

    return results


def summarize(results: Iterable[SanitizeResult]) -> dict[str, int]:
    """Count results by status."""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return dict(sorted(counts.items()))