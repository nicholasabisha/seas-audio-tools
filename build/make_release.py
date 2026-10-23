"""Stage the PyInstaller output into a portable, distributable zip.

Used by both the release workflow and local builds so the two cannot drift.
The zip is written with forward-slash separators so it unpacks correctly with
Explorer, 7-Zip and anything POSIX-based.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

APP_NAME = "SeasAudioTools"

#: Files copied beside the executable so the licence obligations travel with it.
SIDECARS = (
    ("LICENSE", "LICENSE.txt"),
    ("THIRD-PARTY-NOTICES.md", "THIRD-PARTY-NOTICES.txt"),
    ("README.md", "README.md"),
)


def normalise_version(raw: str) -> str:
    version = raw.strip()
    return version[1:] if version.lower().startswith("v") else version


def stage(dist: Path, stage_dir: Path, project: Path) -> None:
    if not dist.is_dir():
        raise SystemExit(f"PyInstaller output not found: {dist}")

    executable = dist / f"{APP_NAME}.exe"
    if not executable.is_file():
        raise SystemExit(f"Missing executable: {executable}")

    bundled_fpcalc = dist / "_internal" / "fpcalc.exe"
    if not bundled_fpcalc.is_file():
        raise SystemExit(f"fpcalc.exe was not bundled: {bundled_fpcalc}")

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    for item in dist.iterdir():
        destination = stage_dir / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)

    for source_name, target_name in SIDECARS:
        source = project / source_name
        if not source.is_file():
            raise SystemExit(f"Missing release file: {source}")
        shutil.copy2(source, stage_dir / target_name)


def zip_directory(source: Path, target: Path, compress_level: int = 6) -> None:
    """Zip ``source`` so it unpacks into a folder named after ``source``."""
    if target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)

    root_name = source.name
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compress_level
    ) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source).as_posix()
            arcname = f"{root_name}/{relative}"
            if path.is_dir():
                archive.writestr(f"{arcname}/", b"")
            else:
                archive.write(path, arcname)


def archive_path_for(stage_dir: Path) -> Path:
    """`SeasAudioTools-1.0.0-win64` -> `SeasAudioTools-1.0.0-win64.zip`.

    Deliberately not ``with_suffix``: pathlib treats the ``.0-win64`` in a
    version string as a suffix and would produce ``SeasAudioTools-1.0.zip``.
    """
    return stage_dir.parent / f"{stage_dir.name}.zip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        help="Release version or git tag; a leading 'v' is stripped.",
    )
    parser.add_argument("--dist", default="build/dist/SeasAudioTools")
    parser.add_argument("--out", default="release")
    parser.add_argument("--project", default=".")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    dist = (project / args.dist).resolve()
    out = (project / args.out).resolve()

    version = normalise_version(args.version)
    stage_dir = out / f"{APP_NAME}-{version}-win64"

    stage(dist, stage_dir, project)
    archive = archive_path_for(stage_dir)
    zip_directory(stage_dir, archive)

    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"Staged : {stage_dir}")
    print(f"Archive: {archive}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())