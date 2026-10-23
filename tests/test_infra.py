"""Tests for scanning, caching and settings."""

from __future__ import annotations

import os
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from seas_audio.core import cache as cache_module
from seas_audio.core.cache import FingerprintCache
from seas_audio.core.config import AppPaths, Settings
from seas_audio.core.scanner import collect_audio_files, count_by_extension


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        (self.root / "album").mkdir()
        (self.root / "album" / "nested").mkdir()
        (self.root / "track1.mp3").write_bytes(b"x")
        (self.root / "track2.MP3").write_bytes(b"x")
        (self.root / "cover.jpg").write_bytes(b"x")
        (self.root / "album" / "track3.flac").write_bytes(b"x")
        (self.root / "album" / "nested" / "track4.ogg").write_bytes(b"x")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_recursive_walk_finds_every_audio_file(self) -> None:
        found = collect_audio_files([self.root])
        names = sorted(path.name for path in found)
        self.assertEqual(names, ["track1.mp3", "track2.MP3", "track3.flac", "track4.ogg"])

    def test_non_recursive_stays_at_top_level(self) -> None:
        found = collect_audio_files([self.root], recursive=False)
        names = sorted(path.name for path in found)
        self.assertEqual(names, ["track1.mp3", "track2.MP3"])

    def test_unsupported_extensions_are_ignored(self) -> None:
        found = collect_audio_files([self.root])
        self.assertNotIn("cover.jpg", [path.name for path in found])

    def test_single_file_target_is_accepted(self) -> None:
        found = collect_audio_files([self.root / "track1.mp3"])
        self.assertEqual([path.name for path in found], ["track1.mp3"])

    def test_duplicate_targets_are_collapsed(self) -> None:
        found = collect_audio_files([self.root, self.root, self.root / "track1.mp3"])
        self.assertEqual(len(found), 4)

    def test_missing_target_warns_instead_of_raising(self) -> None:
        warnings: list[str] = []
        found = collect_audio_files(
            [self.root / "does-not-exist"], warn=warnings.append
        )
        self.assertEqual(found, [])
        self.assertTrue(warnings)

    def test_skipped_directories_are_excluded(self) -> None:
        found = collect_audio_files([self.root], skip_dirs=[self.root / "album"])
        names = sorted(path.name for path in found)
        self.assertEqual(names, ["track1.mp3", "track2.MP3"])

    def test_count_by_extension(self) -> None:
        found = collect_audio_files([self.root])
        self.assertEqual(count_by_extension(found), {"flac": 1, "mp3": 2, "ogg": 1})


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "cache.pkl"
        self.cache = FingerprintCache(self.path)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _record(self, name: str) -> dict:
        return {
            "path": name,
            "filename": os.path.basename(name),
            "duration": 180.0,
            "fp": np.arange(40, dtype=np.uint32),
            "size_mb": 4.0,
            "kbps": 320,
        }

    def test_round_trip_preserves_fingerprints(self) -> None:
        self.cache.save([self._record("a.mp3")])
        loaded = self.cache.load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(len(loaded), 1)
        np.testing.assert_array_equal(loaded[0]["fp"], np.arange(40, dtype=np.uint32))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(self.cache.load())

    def test_schema_mismatch_is_reported_and_ignored(self) -> None:
        self.path.write_bytes(pickle.dumps({"schema": 999, "records": []}))
        self.assertIsNone(self.cache.load())
        self.assertIn("schema", (self.cache.last_error or "").lower())

    def test_corrupt_file_returns_none(self) -> None:
        self.path.write_bytes(b"this is not a pickle")
        self.assertIsNone(self.cache.load())
        self.assertIsNotNone(self.cache.last_error)

    def test_save_is_atomic_leaving_no_temp_file(self) -> None:
        self.cache.save([self._record("a.mp3")])
        leftovers = list(Path(self._dir.name).glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_clear_reports_whether_anything_was_removed(self) -> None:
        self.cache.save([self._record("a.mp3")])
        self.assertTrue(self.cache.clear())
        self.assertFalse(self.cache.clear())

    def test_schema_constant_is_current(self) -> None:
        self.assertEqual(cache_module.CACHE_SCHEMA, 2)


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "settings.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_round_trip(self) -> None:
        settings = Settings(
            library_paths=["E:\\Music"], similarity_threshold=0.9, duration_tolerance=3.0
        )
        settings.save(self.path)
        loaded = Settings.load(self.path)
        self.assertEqual(loaded.library_paths, ["E:\\Music"])
        self.assertAlmostEqual(loaded.similarity_threshold, 0.9)

    def test_missing_file_falls_back_to_defaults(self) -> None:
        loaded = Settings.load(self.path)
        self.assertEqual(loaded.library_paths, [])
        self.assertAlmostEqual(loaded.similarity_threshold, 0.82)

    def test_corrupt_json_falls_back_to_defaults(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(Settings.load(self.path).library_paths, [])

    def test_unknown_keys_are_ignored(self) -> None:
        self.path.write_text('{"similarity_threshold": 0.75, "nonsense": 1}', encoding="utf-8")
        self.assertAlmostEqual(Settings.load(self.path).similarity_threshold, 0.75)


class AppPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_ensure_creates_every_directory(self) -> None:
        paths = AppPaths.default(base=self.base).ensure()
        for directory in (paths.data_dir, paths.cache_dir, paths.report_dir, paths.quarantine_dir):
            self.assertTrue(directory.is_dir(), directory)

    def test_derived_paths_live_under_the_base(self) -> None:
        paths = AppPaths.default(base=self.base)
        self.assertEqual(paths.settings_file, self.base / "settings.json")
        self.assertEqual(paths.fingerprint_cache, self.base / "cache" / "fingerprints.pkl")

    def test_environment_override_is_honoured(self) -> None:
        target = self.base / "custom"
        with mock.patch.dict(os.environ, {"SEAS_AUDIO_HOME": str(target)}):
            self.assertEqual(AppPaths.default().data_dir, target)


if __name__ == "__main__":
    unittest.main()