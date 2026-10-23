"""Build a fingerprinted library, reusing whatever the cache already knows."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from seas_audio.core.cache import FingerprintCache
from seas_audio.core.fpcalc import FingerprintError, fingerprint

# Called as (done, total). Runs on a worker thread, so the UI must marshal it.
ProgressCallback = Callable[[int, int], None]


@dataclass
class LibraryBuild:
    """Outcome of a library scan."""

    records: list[dict] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    cache_used: bool = False
    cache_note: str | None = None

    @property
    def ok_count(self) -> int:
        return len(self.records)


def build_library(
    files: Sequence[Path],
    cache: FingerprintCache,
    *,
    use_cache: bool = True,
    on_progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> LibraryBuild:
    """Fingerprint ``files``, doing as little work as possible.

    Successful fingerprints are cached. Failures are deliberately *not* cached,
    so a file that was locked last time gets retried instead of being written
    off forever.
    """
    build = LibraryBuild()
    wanted = [str(Path(p)) for p in files]
    wanted_set = set(wanted)

    existing: list[dict] = []
    if use_cache:
        loaded = cache.load()
        if cache.last_error:
            build.cache_note = cache.last_error
        if loaded:
            cached_paths = {record["path"] for record in loaded}
            existing = [record for record in loaded if record["path"] in wanted_set]
            build.removed = len(cached_paths - wanted_set)
            build.cache_used = bool(existing)

    known = {record["path"] for record in existing}
    to_scan = [Path(p) for p in wanted if p not in known]

    build.added = len(to_scan)
    total = len(to_scan)
    fresh: list[dict] = []

    for index, path in enumerate(to_scan, start=1):
        if should_cancel is not None and should_cancel():
            break
        try:
            fresh.append(fingerprint(path).as_record())
        except (FingerprintError, OSError) as exc:
            build.failures.append((str(path), str(exc)))
        if on_progress is not None:
            on_progress(index, total)

    build.records = existing + fresh

    if use_cache and (fresh or build.removed):
        try:
            cache.save(build.records)
        except OSError as exc:
            build.cache_note = f"Could not write cache: {exc}"

    return build


def group_by_extension(paths: Iterable[Path]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        suffix = Path(path).suffix.lower().lstrip(".") or "unknown"
        counts[suffix] = counts.get(suffix, 0) + 1
    return dict(sorted(counts.items()))