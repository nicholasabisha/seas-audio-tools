"""Tests for artwork normalisation and the MP3 tag rewrite."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from PIL import Image

from seas_audio.tools import sanitizer

# A syntactically valid MPEG-1 Layer III frame (128 kbps, 44.1 kHz, stereo).
# Repeating it gives mutagen enough frames to read a real duration.
MPEG_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(417 - 4)


def jpeg_bytes(progressive: bool = False, size: tuple[int, int] = (16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (20, 120, 200)).save(
        buffer, format="JPEG", progressive=progressive
    )
    return buffer.getvalue()


def png_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (200, 30, 30, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


def write_mp3(path: Path, *, art: tuple[str, bytes] | None = None, id3_version: int = 4) -> None:
    """Write a tiny but genuinely decodable MP3, optionally with cover art."""
    path.write_bytes(MPEG_FRAME * 8)
    audio = MP3(str(path), ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    if art is not None:
        mime, data = art
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    audio.save(v2_version=id3_version)


class NormaliseArtworkTests(unittest.TestCase):
    def test_png_is_converted_to_baseline_jpeg(self) -> None:
        output, changed, note = sanitizer.normalise_artwork(png_bytes())
        self.assertTrue(changed)
        self.assertIn("JPEG", note)
        with Image.open(io.BytesIO(output)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertFalse(image.info.get("progressive"))

    def test_baseline_jpeg_is_left_untouched(self) -> None:
        original = jpeg_bytes(progressive=False)
        output, changed, _note = sanitizer.normalise_artwork(original)
        self.assertFalse(changed)
        self.assertIs(output, original)

    def test_progressive_jpeg_is_reencoded(self) -> None:
        output, changed, note = sanitizer.normalise_artwork(jpeg_bytes(progressive=True))
        self.assertTrue(changed)
        self.assertIn("progressive", note)
        with Image.open(io.BytesIO(output)) as image:
            self.assertFalse(image.info.get("progressive"))

    def test_transparency_is_dropped_to_rgb(self) -> None:
        output, _changed, _note = sanitizer.normalise_artwork(png_bytes())
        with Image.open(io.BytesIO(output)) as image:
            self.assertEqual(image.mode, "RGB")


class SanitizeFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.folder = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_png_artwork_is_rewritten_as_baseline_jpeg(self) -> None:
        target = self.folder / "track.mp3"
        write_mp3(target, art=("image/png", png_bytes()), id3_version=4)

        result = sanitizer.sanitize_file(target)

        self.assertEqual(result.status, sanitizer.STATUS_SANITIZED)
        self.assertTrue(result.art_reencoded)
        self.assertEqual(result.version_after, "2.3.0")

        tags = ID3(str(target))
        self.assertEqual(tags.version[:2], (2, 3))
        frames = tags.getall("APIC")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].mime, "image/jpeg")
        self.assertTrue(frames[0].data.startswith(b"\xff\xd8"))
        with Image.open(io.BytesIO(frames[0].data)) as image:
            self.assertFalse(image.info.get("progressive"))

    def test_second_run_reports_clean_and_changes_nothing(self) -> None:
        target = self.folder / "track.mp3"
        write_mp3(target, art=("image/png", png_bytes()), id3_version=4)

        sanitizer.sanitize_file(target)
        after_first = target.read_bytes()
        second = sanitizer.sanitize_file(target)

        self.assertEqual(second.status, sanitizer.STATUS_CLEAN)
        self.assertEqual(target.read_bytes(), after_first)

    def test_dry_run_leaves_the_file_byte_identical(self) -> None:
        target = self.folder / "track.mp3"
        write_mp3(target, art=("image/png", png_bytes()), id3_version=4)
        before = target.read_bytes()

        result = sanitizer.sanitize_file(target, dry_run=True)

        self.assertEqual(result.status, sanitizer.STATUS_DRY_RUN)
        self.assertTrue(result.art_reencoded)
        self.assertEqual(target.read_bytes(), before)

    def test_backup_keeps_the_original(self) -> None:
        target = self.folder / "track.mp3"
        write_mp3(target, art=("image/png", png_bytes()), id3_version=4)
        original = target.read_bytes()

        sanitizer.sanitize_file(target, backup=True)

        backup = target.with_suffix(".mp3.bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), original)

    def test_art_without_cover_is_still_upgraded_to_v23(self) -> None:
        target = self.folder / "noart.mp3"
        write_mp3(target, art=None, id3_version=4)

        result = sanitizer.sanitize_file(target)

        self.assertEqual(result.status, sanitizer.STATUS_NO_ART)
        self.assertEqual(ID3(str(target)).version[:2], (2, 3))

    def test_non_mp3_is_skipped(self) -> None:
        target = self.folder / "song.flac"
        target.write_bytes(b"not really a flac")

        result = sanitizer.sanitize_file(target)

        self.assertEqual(result.status, sanitizer.STATUS_SKIPPED)

    def test_missing_file_is_an_error_not_a_crash(self) -> None:
        result = sanitizer.sanitize_file(self.folder / "ghost.mp3")
        self.assertEqual(result.status, sanitizer.STATUS_ERROR)
        self.assertFalse(result.ok)

    def test_mime_only_problem_is_still_fixed(self) -> None:
        target = self.folder / "mismatch.mp3"
        # Baseline JPEG data mislabelled as PNG.
        write_mp3(target, art=("image/png", jpeg_bytes(progressive=False)), id3_version=3)

        result = sanitizer.sanitize_file(target)

        self.assertEqual(result.status, sanitizer.STATUS_SANITIZED)
        self.assertFalse(result.art_reencoded)
        self.assertTrue(result.mime_fixed)
        self.assertEqual(ID3(str(target)).getall("APIC")[0].mime, "image/jpeg")


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.folder = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_batch_summary_and_progress(self) -> None:
        write_mp3(self.folder / "one.mp3", art=("image/png", png_bytes()))
        write_mp3(self.folder / "two.mp3", art=("image/png", png_bytes()))
        (self.folder / "notes.txt").write_text("ignore me", encoding="utf-8")

        seen: list[tuple[int, int]] = []
        files = [self.folder / "one.mp3", self.folder / "two.mp3", self.folder / "notes.txt"]
        results = sanitizer.sanitize_many(
            files, on_progress=lambda done, total, _name: seen.append((done, total))
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(seen[-1], (3, 3))
        summary = sanitizer.summarize(results)
        self.assertEqual(summary[sanitizer.STATUS_SANITIZED], 2)
        self.assertEqual(summary[sanitizer.STATUS_SKIPPED], 1)

    def test_cancellation_stops_early(self) -> None:
        for index in range(4):
            write_mp3(self.folder / f"track{index}.mp3", art=("image/png", png_bytes()))
        files = sorted(self.folder.glob("*.mp3"))

        results = sanitizer.sanitize_many(files, should_cancel=lambda: True)

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()