"""Turn a pile of dropped paths into a sorted list of audio files.

One implementation so the sanitizer and the duplicate scanner always agree on
what counts as audio and how deep the walk goes.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from seas_audio.core.config import AUDIO_EXTENSIONS

Warn = Callable[[str], None]


def _normkey(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def collect_audio_files(
    targets: Iterable[str | os.PathLike[str]],
    *,
    extensions: frozenset[str] = AUDIO_EXTENSIONS,
    recursive: bool = True,
    skip_dirs: Sequence[str | os.PathLike[str]] = (),
    warn: Warn | None = None,
) -> list[Path]:
    """Expand files and folders into a de-duplicated, sorted list of audio files.

    A dropped file is kept only if its extension is supported. A dropped folder
    is walked (recursively by default) and every supported file inside is kept.
    Missing or unreadable targets are reported through ``warn`` and skipped.
    """
    skip = {_normkey(Path(p)) for p in skip_dirs}
    found: dict[str, Path] = {}

    def note(message: str) -> None:
        if warn is not None:
            warn(message)

    for raw in targets:
        if raw is None:
            continue
        target = Path(raw)
        if not target.exists():
            note(f"Skipping missing path: {target}")
            continue

        if target.is_file():
            if target.suffix.lower() in extensions:
                found.setdefault(_normkey(target), target)
            else:
                note(f"Skipping unsupported file: {target.name}")
            continue

        if not target.is_dir():
            note(f"Skipping unsupported target: {target}")
            continue

        try:
            entries = target.rglob("*") if recursive else target.glob("*")
            for entry in entries:
                if entry.suffix.lower() not in extensions:
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if skip and any(_is_under(entry, s) for s in skip):
                    continue
                found.setdefault(_normkey(entry), entry)
        except OSError as exc:
            note(f"Could not read folder {target}: {exc}")

    return sorted(found.values(), key=lambda p: str(p).lower())


def _is_under(path: Path, parent_key: str) -> bool:
    return _normkey(path).startswith(parent_key + os.sep)


def count_by_extension(files: Iterable[Path]) -> dict[str, int]:
    """Summary helper used by the CLI and the GUI status line."""
    counts: dict[str, int] = {}
    for path in files:
        suffix = path.suffix.lower().lstrip(".") or "unknown"
        counts[suffix] = counts.get(suffix, 0) + 1
    return dict(sorted(counts.items()))