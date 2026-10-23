"""Command line entry point: ``seas-audio <command> ...``."""

from __future__ import annotations

import argparse
import contextlib
import csv
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from seas_audio import __version__
from seas_audio.core.cache import FingerprintCache
from seas_audio.core.config import (
    DEFAULT_DURATION_TOLERANCE,
    DEFAULT_MAX_OFFSET_FRAMES,
    DEFAULT_SIMILARITY_THRESHOLD,
    AppPaths,
    Settings,
)
from seas_audio.core.fpcalc import FpcalcNotFoundError, probe
from seas_audio.core.library import build_library
from seas_audio.core.scanner import collect_audio_files
from seas_audio.tools import duplicates, sanitizer


def _write_csv(path: Path, rows: Sequence[dict], fieldnames: Sequence[str]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


@contextlib.contextmanager
def _progress(total: int, description: str, enabled: bool) -> Iterator[Any]:
    """tqdm only when it can render; otherwise a no-op."""
    if not enabled or total <= 0:
        yield None
        return
    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover - tqdm is a declared dependency
        yield None
        return
    with tqdm(total=total, desc=description, unit="item") as bar:
        yield bar


def _report_progress(bar: Any, done: int, total: int) -> None:
    if bar is None:
        return
    bar.update(max(0, done - bar.n))


def _echo(enabled: bool, message: str) -> None:
    if enabled:
        print(message)


def cmd_sanitize(args: argparse.Namespace) -> int:
    paths = AppPaths.default().ensure()
    settings = Settings.load(paths.settings_file)

    def warn(message: str) -> None:
        _echo(not args.quiet, f"  ! {message}")

    files = collect_audio_files(args.targets, warn=warn)
    _echo(not args.quiet, f"Found {len(files)} audio file(s).")
    if not files:
        return 0

    with _progress(len(files), "Sanitizing", not args.quiet) as bar:
        results = sanitizer.sanitize_many(
            files,
            dry_run=args.dry_run,
            backup=args.backup,
            on_progress=lambda done, total, name: _report_progress(bar, done, total),
        )

    for result in results:
        if result.status in (sanitizer.STATUS_ERROR,):
            print(f"ERROR  {result.path.name}: {result.message}", file=sys.stderr)
        elif result.status == sanitizer.STATUS_SKIPPED:
            if args.verbose:
                print(f"skipped  {result.path.name}: {result.message}")
        elif result.status in (sanitizer.STATUS_CLEAN,):
            if args.verbose:
                print(f"clean    {result.path.name}")
        elif args.quiet and result.status == sanitizer.STATUS_DRY_RUN:
            continue
        else:
            print(f"{result.status:<12} {result.path.name}: {result.message}")

    summary = sanitizer.summarize(results)
    print("")
    print("Summary: " + ", ".join(f"{status}={count}" for status, count in summary.items()))

    if args.report:
        rows = [
            {
                "Status": result.status,
                "Message": result.message,
                "Artwork Re-encoded": result.art_reencoded,
                "Mime Fixed": result.mime_fixed,
                "Version Before": result.version_before,
                "Version After": result.version_after,
                "Path": str(result.path),
            }
            for result in results
        ]
        written = _write_csv(
            Path(args.report),
            rows,
            [
                "Status",
                "Message",
                "Artwork Re-encoded",
                "Mime Fixed",
                "Version Before",
                "Version After",
                "Path",
            ],
        )
        print(f"Report written to {written}")

    if args.dry_run:
        print(f"(dry run - no files were modified; using {paths.data_dir})")

    return 1 if summary.get(sanitizer.STATUS_ERROR) else 0


def cmd_dupes(args: argparse.Namespace) -> int:
    paths = AppPaths.default().ensure()
    settings = Settings.load(paths.settings_file)

    def warn(message: str) -> None:
        _echo(not args.quiet, f"  ! {message}")

    verbose = not args.quiet
    primary_files = collect_audio_files(args.targets, warn=warn)
    if not primary_files:
        print("No audio files found in the given paths.", file=sys.stderr)
        return 1

    reference_files: list[Path] = []
    if args.reference:
        reference_files = collect_audio_files(args.reference, warn=warn)
        _echo(verbose, f"Reference library: {len(reference_files)} file(s).")

    cache_path = Path(args.cache) if args.cache else paths.fingerprint_cache
    cache = FingerprintCache(cache_path)

    print(f"Primary library: {len(primary_files)} file(s).")
    if cache_path.is_file() and not args.no_cache:
        print(f"Using cache: {cache_path}")

    with _progress(len(primary_files), "Fingerprinting primary", verbose) as bar:
        primary_build = build_library(
            primary_files,
            cache,
            use_cache=not args.no_cache,
            on_progress=lambda done, total: _report_progress(bar, done, total),
        )

    if primary_build.cache_note:
        print(f"  {primary_build.cache_note}")
    if primary_build.added:
        print(f"  Fingerprinted {primary_build.added} new file(s).")
    if primary_build.failures:
        print(f"  {len(primary_build.failures)} file(s) could not be read.")
        for path, reason in primary_build.failures:
            print(f"    ! {Path(path).name}: {reason}", file=sys.stderr)

    reference_records = None
    if reference_files:
        with _progress(len(reference_files), "Fingerprinting reference", verbose) as bar:
            reference_build = build_library(
                reference_files,
                cache,
                use_cache=not args.no_cache,
                on_progress=lambda done, total: _report_progress(bar, done, total),
            )
        if reference_build.failures:
            print(f"  {len(reference_build.failures)} reference file(s) could not be read.")
        reference_records = reference_build.records

    params = duplicates.SimilarityParams(
        duration_tolerance=args.tolerance,
        threshold=args.threshold,
        max_offset_frames=args.max_offset,
    )

    candidate_total = duplicates.count_candidate_pairs(
        primary_build.records, reference_records, params.duration_tolerance
    )
    print(f"Comparing {candidate_total:,} candidate pair(s) "
          f"(threshold {params.threshold:.2f}, tolerance {params.duration_tolerance:g}s).")

    with _progress(candidate_total, "Comparing pairs", verbose) as bar:
        pairs = duplicates.find_duplicates(
            primary_build.records,
            reference_records,
            params,
            on_progress=lambda done, total: _report_progress(bar, done, total),
        )

    if not pairs:
        print("No duplicates above the similarity threshold.")
        return 0

    rows = [pair.as_row() for pair in pairs]
    report_path = Path(args.report) if args.report else paths.report_path("duplicate_report.csv")
    written = _write_csv(report_path, rows, duplicates.ROW_FIELDS)

    print(f"Found {len(pairs)} duplicate pair(s).")
    for pair in pairs[: args.show]:
        print(
            f"  {pair.similarity * 100:5.1f}%  {pair.filename_a}  <->  {pair.filename_b}"
        )
    if len(pairs) > args.show:
        print(f"  ... {len(pairs) - args.show} more")
    print(f"Report written to {written}")
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    paths = AppPaths.default().ensure()
    cache = FingerprintCache(paths.fingerprint_cache)
    if args.cache_action == "clear":
        removed = cache.clear()
        print(f"Cache {'cleared' if removed else 'was already empty'}: {cache.path}")
        return 0
    if args.cache_action == "path":
        print(cache.path)
        return 0
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from seas_audio.ui.app import main as gui_main

    return gui_main(list(args.targets or []))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seas-audio",
        description="MP3 tag sanitizer and audio duplicate finder.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sanitize = subparsers.add_parser(
        "sanitize", help="Rewrite cover art to baseline JPEG and force ID3v2.3."
    )
    sanitize.add_argument("targets", nargs="+", help="Files and/or folders to process.")
    sanitize.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    sanitize.add_argument("--backup", action="store_true", help="Keep a .bak copy before writing.")
    sanitize.add_argument("--report", help="Write a CSV report to this path.")
    sanitize.add_argument("--quiet", action="store_true", help="Only print errors and the summary.")
    sanitize.add_argument("--verbose", action="store_true", help="Also list clean and skipped files.")
    sanitize.set_defaults(func=cmd_sanitize)

    dupes = subparsers.add_parser("dupes", help="Find near-duplicate audio files.")
    dupes.add_argument("targets", nargs="+", help="Files and/or folders to scan.")
    dupes.add_argument(
        "--reference", nargs="+", metavar="PATH",
        help="Optional reference library to compare the targets against.",
    )
    dupes.add_argument(
        "--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD,
        help=f"Similarity cut-off 0-1 (default {DEFAULT_SIMILARITY_THRESHOLD}).",
    )
    dupes.add_argument(
        "--tolerance", type=float, default=DEFAULT_DURATION_TOLERANCE,
        help=f"Duration window in seconds (default {DEFAULT_DURATION_TOLERANCE:g}).",
    )
    dupes.add_argument(
        "--max-offset", type=int, default=DEFAULT_MAX_OFFSET_FRAMES,
        help=f"Offset search range (default {DEFAULT_MAX_OFFSET_FRAMES}).",
    )
    dupes.add_argument("--report", help="CSV output path.")
    dupes.add_argument("--show", type=int, default=15, help="How many matches to print (default 15).")
    dupes.add_argument("--no-cache", action="store_true", help="Ignore and rewrite the cache.")
    dupes.add_argument("--cache", help="Use a specific cache file.")
    dupes.add_argument("--quiet", action="store_true", help="No progress bars.")
    dupes.set_defaults(func=cmd_dupes)

    cache_parser = subparsers.add_parser("cache", help="Inspect or clear the fingerprint cache.")
    cache_parser.add_argument(
        "cache_action", choices=("clear", "path"), nargs="?", default="path"
    )
    cache_parser.set_defaults(func=cmd_cache)

    gui = subparsers.add_parser("gui", help="Open the drag-and-drop window.")
    gui.add_argument("targets", nargs="*", help="Optional paths to pre-load.")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args) or 0)
    except FpcalcNotFoundError as exc:
        print(f"fpcalc not found.\n{exc}", file=sys.stderr)
        print(f"\nDiagnostic: {probe()}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())