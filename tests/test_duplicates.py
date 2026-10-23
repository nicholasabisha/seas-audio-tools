"""Tests for the fingerprint comparison maths and pair generation."""

from __future__ import annotations

import os
import unittest

import numpy as np

from seas_audio.tools import duplicates


def make_fingerprint(seed: int, length: int = 200) -> np.ndarray:
    """Deterministic pseudo-random uint32 fingerprint."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 2**32, size=length, dtype=np.uint32)


def make_record(path: str, duration: float, seed: int, length: int = 200) -> dict:
    return {
        "path": path,
        "filename": os.path.basename(path),
        "duration": duration,
        "fp": make_fingerprint(seed, length),
        "size_mb": 4.2,
        "kbps": 320,
    }


class SlidingSimilarityTests(unittest.TestCase):
    def test_identical_fingerprints_score_one(self) -> None:
        fingerprint = make_fingerprint(1)
        self.assertAlmostEqual(
            duplicates.sliding_similarity(fingerprint, fingerprint.copy(), 50), 1.0
        )

    def test_offset_copy_still_scores_high(self) -> None:
        base = make_fingerprint(2, 400)
        # Same audio, but shifted 12 values later - like a trimmed intro.
        shifted = np.concatenate([np.zeros(12, dtype=np.uint32), base[:-12]])
        score = duplicates.sliding_similarity(base, shifted, 50)
        self.assertGreater(score, 0.98)

    def test_unrelated_fingerprints_score_low(self) -> None:
        one = make_fingerprint(3)
        other = make_fingerprint(4)
        score = duplicates.sliding_similarity(one, other, 50)
        self.assertLess(score, 0.75)

    def test_too_short_input_is_zero(self) -> None:
        short = make_fingerprint(5, 10)
        self.assertEqual(duplicates.sliding_similarity(short, short, 50), 0.0)

    def test_single_bit_difference_is_measurable(self) -> None:
        base = np.full(100, 0b1010, dtype=np.uint32)
        mutated = base.copy()
        mutated[0] ^= np.uint32(1 << 3)
        expected = 1.0 - (1 / (100 * 32))
        self.assertAlmostEqual(
            duplicates.sliding_similarity(base, mutated, 0), expected, places=9
        )


class PairGenerationTests(unittest.TestCase):
    def test_duration_window_filters_pairs(self) -> None:
        near = make_record("near.mp3", 200.0, seed=10)
        also_near = make_record("also_near.mp3", 203.0, seed=11)
        far = make_record("far.mp3", 400.0, seed=12)
        pairs = list(duplicates.iter_internal_pairs([near, also_near, far], 5.0))
        self.assertEqual(len(pairs), 1)
        names = {pairs[0][0]["path"], pairs[0][1]["path"]}
        self.assertEqual(names, {"near.mp3", "also_near.mp3"})

    def test_candidate_count_matches_generated_pairs(self) -> None:
        records = [
            make_record(f"track{i}.mp3", 100.0 + i * 2, seed=20 + i) for i in range(12)
        ]
        counted = duplicates.count_candidate_pairs(records, None, 5.0)
        generated = sum(1 for _ in duplicates.iter_internal_pairs(records, 5.0))
        self.assertEqual(counted, generated)

    def test_cross_pair_count_matches_generated_pairs(self) -> None:
        primary = [
            make_record(f"new{i}.mp3", 150.0 + i, seed=40 + i) for i in range(6)
        ]
        reference = [
            make_record(f"old{i}.mp3", 150.0 + i, seed=60 + i) for i in range(7)
        ]
        counted = duplicates.count_candidate_pairs(primary, reference, 2.0)
        # The counter must match exactly what find_duplicates walks: every
        # primary-vs-reference pair plus every pair inside the primary set.
        generated = sum(
            1 for _ in duplicates.iter_cross_pairs(primary, reference, 2.0)
        ) + sum(1 for _ in duplicates.iter_internal_pairs(primary, 2.0))
        self.assertEqual(counted, generated)


class FindDuplicatesTests(unittest.TestCase):
    def test_finds_planted_duplicate(self) -> None:
        original = make_record("Song.mp3", 210.0, seed=100)
        reencode = dict(original)
        reencode["path"] = "Song (1).mp3"
        reencode["filename"] = "Song (1).mp3"
        reencode["duration"] = 210.4
        different = make_record("Other.mp3", 210.0, seed=101)

        found = duplicates.find_duplicates([original, reencode, different])

        self.assertEqual(len(found), 1)
        pair = found[0]
        self.assertAlmostEqual(pair.similarity, 1.0)
        self.assertEqual({pair.filename_a, pair.filename_b}, {"Song.mp3", "Song (1).mp3"})

    def test_duration_gap_hides_identical_fingerprints(self) -> None:
        first = make_record("a.mp3", 100.0, seed=200)
        second = make_record("b.mp3", 400.0, seed=200)
        found = duplicates.find_duplicates([first, second])
        self.assertEqual(found, [])

    def test_reference_mode_compares_against_reference(self) -> None:
        primary = [make_record("new_track.mp3", 180.0, seed=300)]
        reference = [make_record("library_track.mp3", 180.2, seed=300)]
        found = duplicates.find_duplicates(primary, reference)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path_a, "new_track.mp3")
        self.assertEqual(found[0].path_b, "library_track.mp3")

    def test_identical_path_is_never_its_own_duplicate(self) -> None:
        record = make_record("same.mp3", 180.0, seed=400)
        found = duplicates.find_duplicates([record], [dict(record)])
        self.assertEqual(found, [])

    def test_threshold_is_respected(self) -> None:
        first = make_record("x.mp3", 200.0, seed=500)
        second = make_record("y.mp3", 200.0, seed=501)
        permissive = duplicates.find_duplicates(
            [first, second], params=duplicates.SimilarityParams(threshold=0.0)
        )
        strict = duplicates.find_duplicates(
            [first, second], params=duplicates.SimilarityParams(threshold=0.99)
        )
        self.assertEqual(len(permissive), 1)
        self.assertEqual(strict, [])

    def test_results_are_sorted_by_similarity(self) -> None:
        base = make_record("base.mp3", 200.0, seed=600, length=300)
        exact = dict(base, path="exact.mp3", filename="exact.mp3")
        noisy_fp = base["fp"].copy()
        noisy_fp[::40] ^= np.uint32(0xFFFF)
        noisy = dict(
            base, path="noisy.mp3", filename="noisy.mp3", fp=noisy_fp
        )
        found = duplicates.find_duplicates([base, exact, noisy], params=duplicates.SimilarityParams(threshold=0.5))
        self.assertGreaterEqual(len(found), 2)
        scores = [pair.similarity for pair in found]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_short_fingerprints_are_ignored(self) -> None:
        tiny = make_record("tiny.mp3", 200.0, seed=700, length=5)
        twin = make_record("twin.mp3", 200.0, seed=700, length=5)
        self.assertEqual(duplicates.find_duplicates([tiny, twin]), [])

    def test_report_row_has_expected_columns(self) -> None:
        pair = duplicates.DuplicatePair(
            similarity=0.93,
            path_a="a.mp3",
            filename_a="a.mp3",
            kbps_a=320,
            size_a=9.1,
            path_b="b.mp3",
            filename_b="b.mp3",
            kbps_b=192,
            size_b=5.4,
        )
        self.assertEqual(set(pair.as_row()), set(duplicates.ROW_FIELDS))
        self.assertEqual(pair.as_row()["Similarity"], 93.0)

    def test_progress_callback_reports_completion(self) -> None:
        records = [
            make_record(f"p{i}.mp3", 100.0 + i, seed=800 + i) for i in range(8)
        ]
        seen: list[tuple[int, int]] = []
        duplicates.find_duplicates(
            records, on_progress=lambda done, total: seen.append((done, total))
        )
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])


if __name__ == "__main__":
    unittest.main()