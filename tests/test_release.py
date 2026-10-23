"""Tests for the release staging script."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "build" / "make_release.py"

spec = importlib.util.spec_from_file_location("make_release", SCRIPT)
make_release = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(make_release)


class VersionTests(unittest.TestCase):
    def test_strips_leading_v(self) -> None:
        self.assertEqual(make_release.normalise_version("v1.0.0"), "1.0.0")
        self.assertEqual(make_release.normalise_version("V2.3.4"), "2.3.4")

    def test_plain_version_is_unchanged(self) -> None:
        self.assertEqual(make_release.normalise_version("1.0.0"), "1.0.0")

    def test_archive_name_keeps_the_full_version(self) -> None:
        stage = Path("release") / "SeasAudioTools-1.0.0-win64"
        self.assertEqual(
            make_release.archive_path_for(stage).name,
            "SeasAudioTools-1.0.0-win64.zip",
        )


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

        self.dist = self.root / "dist" / "SeasAudioTools"
        (self.dist / "_internal").mkdir(parents=True)
        (self.dist / "SeasAudioTools.exe").write_bytes(b"MZ fake exe")
        (self.dist / "_internal" / "fpcalc.exe").write_bytes(b"MZ fake fpcalc")
        (self.dist / "_internal" / "data.bin").write_bytes(b"payload")

        for name in ("LICENSE", "THIRD-PARTY-NOTICES.md", "README.md"):
            (self.root / name).write_text(f"{name} contents", encoding="utf-8")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_stage_copies_binary_and_sidecars(self) -> None:
        stage_dir = self.root / "release" / "SeasAudioTools-9.9.9-win64"
        make_release.stage(self.dist, stage_dir, self.root)

        self.assertTrue((stage_dir / "SeasAudioTools.exe").is_file())
        self.assertTrue((stage_dir / "_internal" / "fpcalc.exe").is_file())
        self.assertTrue((stage_dir / "LICENSE.txt").is_file())
        self.assertTrue((stage_dir / "THIRD-PARTY-NOTICES.txt").is_file())
        self.assertTrue((stage_dir / "README.md").is_file())

    def test_zip_is_posix_friendly_and_rooted(self) -> None:
        stage_dir = self.root / "release" / "SeasAudioTools-9.9.9-win64"
        make_release.stage(self.dist, stage_dir, self.root)
        archive = make_release.archive_path_for(stage_dir)
        make_release.zip_directory(stage_dir, archive)

        with zipfile.ZipFile(archive) as handle:
            names = handle.namelist()
            self.assertTrue(all("\\" not in name for name in names))
            self.assertEqual(
                {name.split("/")[0] for name in names},
                {"SeasAudioTools-9.9.9-win64"},
            )
            self.assertIn("SeasAudioTools-9.9.9-win64/SeasAudioTools.exe", names)
            self.assertIn(
                "SeasAudioTools-9.9.9-win64/_internal/fpcalc.exe", names
            )
            self.assertIsNone(handle.testzip())

    def test_missing_fpcalc_is_rejected(self) -> None:
        (self.dist / "_internal" / "fpcalc.exe").unlink()
        stage_dir = self.root / "release" / "SeasAudioTools-9.9.9-win64"
        with self.assertRaises(SystemExit):
            make_release.stage(self.dist, stage_dir, self.root)


if __name__ == "__main__":
    unittest.main()