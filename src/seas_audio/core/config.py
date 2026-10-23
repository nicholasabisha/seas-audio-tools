"""Application paths and persisted user settings.

Everything the tools write (caches, CSV reports, backups) is funneled through
``AppPaths`` so it never lands next to the scripts or inside the music library.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "seas-audio-tools"
APP_TITLE = "Seas Audio Tools"

#: Formats the duplicate scanner understands (whatever fpcalc/ffmpeg can decode).
AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav"})

#: The sanitizer only rewrites ID3 tags, which means MP3 only.
SANITIZABLE_EXTENSIONS = frozenset({".mp3"})

DEFAULT_DURATION_TOLERANCE = 5.0
DEFAULT_SIMILARITY_THRESHOLD = 0.82
DEFAULT_MAX_OFFSET_FRAMES = 50


def _env_home() -> Path | None:
    override = os.environ.get("SEAS_AUDIO_HOME")
    if override:
        return Path(override).expanduser()
    return None


@dataclass
class Settings:
    """User-tunable knobs, persisted as JSON."""

    library_paths: list[str] = field(default_factory=list)
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    max_offset_frames: int = DEFAULT_MAX_OFFSET_FRAMES

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """Read settings, falling back to defaults on any problem."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()

        if not isinstance(raw, dict):
            return cls()

        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known and v is not None}
        try:
            return cls(**clean)
        except TypeError:
            return cls()

    def save(self, path: Path) -> None:
        """Write settings atomically."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, target)


@dataclass(frozen=True)
class AppPaths:
    """Every directory the app writes to."""

    data_dir: Path
    cache_dir: Path
    report_dir: Path
    quarantine_dir: Path

    @classmethod
    def default(cls, base: Path | None = None) -> "AppPaths":
        if base is None:
            base = _env_home()
        if base is None:
            local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            base = Path(local) / APP_NAME if local else Path.home() / f".{APP_NAME}"
        base = Path(base)
        return cls(
            data_dir=base,
            cache_dir=base / "cache",
            report_dir=base / "reports",
            quarantine_dir=base / "quarantine",
        )

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def fingerprint_cache(self) -> Path:
        return self.cache_dir / "fingerprints.pkl"

    def ensure(self) -> "AppPaths":
        """Create the directories if they are missing."""
        for directory in (self.data_dir, self.cache_dir, self.report_dir, self.quarantine_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def report_path(self, name: str) -> Path:
        return self.report_dir / name