"""Tests for cross-job result comparison (key alignment, duplicates, tolerance).

The comparison must align results by record key — never by position — so two
jobs with different scales, partition counts and record orders compare
correctly: no false positives (identical data reported as different) and no
missed differences.
"""

import random
import shutil
import tempfile
import unittest

from backend.common.storage import Storage, atomic_write_json
from backend.master.result_compare import (
    DIFF_ONLY_IN_A, DIFF_ONLY_IN_B, DIFF_VALUE_MISMATCH,
    compare_jobs, compare_record_sets, filter_diffs, read_job_results,
    values_equal,
)


def rec(key, **fields):
    return {"key": key, **fields}


def diff_keys(report, diff_type):
    return {d["key"] for d in report["diffs"] if d["type"] == diff_type}


class TestValuesEqual(unittest.TestCase):
    def test_int_float_unified(self):
        self.assertTrue(values_equal(3, 3.0))
        self.assertTrue(values_equal(0, -0.0))
        self.assertFalse(values_equal(3, 3.5))

    def test_bool_is_not_a_number(self):
        self.assertFalse(values_equal(True, 1))
        self.assertFalse(values_equal(False, 0))
        self.assertTrue(values_equal(True, True))

    def test_string_is_not_a_number(self):
        self.assertFalse(values_equal("3", 3))
        self.assertTrue(values_equal("abc", "abc"))

    def test_none(self):
        self.assertTrue(values_equal(None, None))
        self.assertFalse(values_equal(None, 0))
        self.assertFalse(values_equal(None, ""))

    def test_nested_structures(self):
        self.assertTrue(values_equal({"a": [1, 2.0], "b": {"c": "x"}},
                                     {"a": [1, 2], "b": {"c": "x"}}))
        self.assertFalse(values_equal({"a": [1, 2]}, {"a": [1, 2, 3]}))
        self.assertFalse(values_equal({"a": 1}, {"a": 1, "b": 2}))
        self.assertFalse(values_equal([1, 2], (1, 3)))

    def test_tolerance(self):
        self.assertFalse(values_equal(1.000, 1.001))
        self.assertTrue(values_equal(1.000, 1.001, rel_tol=0.01, abs_tol=0.01))
        self.assertTrue(values_equal(1000.0, 1000.4, rel_tol=0.001, abs_tol=0.0))
        self.assertFalse(values_equal(1000.0, 1002.0, rel_tol=0.001, abs_tol=0.0))

    def test_nan_and_inf(self):
        self.assertTrue(values_equal(float("nan"), float("nan")))
        self.assertTrue(values_equal(float("inf"), float("inf")))
        self.assertFalse(values_equal(float("inf"), float("-inf")))
        self.assertFalse(values_equal(float("nan"), 1.0))


class TestCompareRecordSets(unittest.TestCase):
    def test_identical_despite_order(self):
        a = [rec("x", count=1), rec("y", count=2), rec("z", count=3)]
        b = [rec("z", count=3), rec("x", count=1), rec("y", count=2)]
        report = compare_record_sets(a, b)
        self.assertTrue(report["summary"]["identical"])
        self.assertEqual(report["diffs"], [])
        self.assertEqual(report["summary"]["same"], 3)

    def test_aligns_by_key_not_position(self):
        # Same length, same keys, every position different — must not misalign.
        a = [rec(f"k{i}", count=i) for i in range(50)]
        b = [rec(f"k{i}", count=i) for i in reversed(range(50))]
        report = compare_record_sets(a, b)
        self.assertTrue(report["summary"]["identical"])

    def test_only_on_one_side(self):
        report = compare_record_sets([rec("a", count=1), rec("b", count=2)],
                                     [rec("b", count=2), rec("c", count=3)])
        s = report["summary"]
        self.assertFalse(s["identical"])
        self.assertEqual(diff_keys(report, DIFF_ONLY_IN_A), {"a"})
        self.assertEqual(diff_keys(report, DIFF_ONLY_IN_B), {"c"})
        self.assertEqual(s["only_in_a"], 1)
        self.assertEqual(s["only_in_b"], 1)
        self.assertEqual(s["records_only_in_a"], 1)
        self.assertEqual(s["records_only_in_b"], 1)
        self.assertEqual(s["same"], 1)

    def test_value_mismatch_field_detail(self):
        report = compare_record_sets([rec("k", count=5, avg=1.5)],
                                     [rec("k", count=8, avg=1.5)])
        self.assertEqual(diff_keys(report, DIFF_VALUE_MISMATCH), {"k"})
        diff = report["diffs"][0]
        fields = {f["field"]: f for f in diff["fields"]}
        self.assertFalse(fields["count"]["same"])
        self.assertEqual(fields["count"]["delta"], 3.0)
        self.assertTrue(fields["avg"]["same"])

    def test_missing_field_detected(self):
        report = compare_record_sets([rec("k", count=5)], [rec("k", count=5, extra=1)])
        diff = report["diffs"][0]
        self.assertEqual(diff["type"], DIFF_VALUE_MISMATCH)
        fields = {f["field"]: f for f in diff["fields"]}
        self.assertEqual(fields["extra"]["missing"], "a")
        self.assertFalse(fields["extra"]["same"])

    def test_int_float_across_jobs_not_flagged(self):
        # One reducer emitted ints, the other floats: not a difference.
        report = compare_record_sets([rec("k", count=3)], [rec("k", count=3.0)])
        self.assertTrue(report["summary"]["identical"])

    def test_bool_vs_number_flagged(self):
        report = compare_record_sets([rec("k", flag=True)], [rec("k", flag=1)])
        self.assertEqual(diff_keys(report, DIFF_VALUE_MISMATCH), {"k"})

    def test_string_vs_number_flagged(self):
        report = compare_record_sets([rec("k", count="3")], [rec("k", count=3)])
        self.assertEqual(diff_keys(report, DIFF_VALUE_MISMATCH), {"k"})

    def test_duplicate_identical_multisets_not_flagged(self):
        a = [rec("k", count=1), rec("k", count=2), rec("k", count=2)]
        b = [rec("k", count=2), rec("k", count=1), rec("k", count=2)]
        report = compare_record_sets(a, b)
        self.assertTrue(report["summary"]["identical"])
        # ...but the duplication is still visible in the side stats.
        self.assertEqual(report["a"]["duplicate_keys"], 1)
        self.assertEqual(report["b"]["duplicate_keys"], 1)

    def test_duplicate_count_mismatch(self):
        a = [rec("k", count=1), rec("k", count=1)]
        b = [rec("k", count=1)]
        report = compare_record_sets(a, b)
        diff = report["diffs"][0]
        self.assertEqual(diff["type"], DIFF_VALUE_MISMATCH)
        self.assertTrue(diff["duplicate"])
        self.assertEqual((diff["count_a"], diff["count_b"]), (2, 1))

    def test_duplicate_content_mismatch(self):
        a = [rec("k", count=1), rec("k", count=2)]
        b = [rec("k", count=1), rec("k", count=3)]
        report = compare_record_sets(a, b)
        self.assertEqual(diff_keys(report, DIFF_VALUE_MISMATCH), {"k"})
        self.assertTrue(report["diffs"][0]["duplicate"])

    def test_unequal_record_counts(self):
        a = [rec(f"k{i}", v=i) for i in range(10)]
        b = [rec(f"k{i}", v=i) for i in range(4)]
        report = compare_record_sets(a, b)
        s = report["summary"]
        self.assertEqual(s["only_in_a"], 6)
        self.assertEqual(s["records_only_in_a"], 6)
        self.assertEqual(s["same"], 4)

    def test_empty_sides(self):
        report = compare_record_sets([], [])
        self.assertTrue(report["summary"]["identical"])
        report = compare_record_sets([rec("a")], [])
        self.assertEqual(report["summary"]["only_in_a"], 1)

    def test_tolerance_applies_to_values_not_keys(self):
        a = [rec("k", avg=1.000)]
        b = [rec("k", avg=1.001)]
        self.assertEqual(compare_record_sets(a, b)["summary"]["value_mismatch"], 1)
        self.assertTrue(compare_record_sets(a, b, tolerance=0.01)["summary"]["identical"])
        # Keys always align exactly, tolerance never merges distinct keys.
        report = compare_record_sets([rec(1.000, v=1)], [rec(1.001, v=1)], tolerance=0.01)
        self.assertEqual(report["summary"]["only_in_a"], 1)
        self.assertEqual(report["summary"]["only_in_b"], 1)

    def test_deterministic_ordering(self):
        a = [rec("z"), rec("a"), rec("m")]
        b = []
        r1 = compare_record_sets(a, b)
        r2 = compare_record_sets(list(reversed(a)), b)
        self.assertEqual([d["key"] for d in r1["diffs"]], ["a", "m", "z"])
        self.assertEqual(r1["diffs"], r2["diffs"])

    def test_records_without_key_use_whole_record_identity(self):
        a = [{"v": 1}, {"v": 2}]
        b = [{"v": 2}, {"v": 3}]
        report = compare_record_sets(a, b)
        self.assertEqual(report["summary"]["same"], 1)
        self.assertEqual(report["summary"]["only_in_a"], 1)
        self.assertEqual(report["summary"]["only_in_b"], 1)

    def test_numeric_and_string_keys_do_not_collide(self):
        report = compare_record_sets([rec(3, v=1)], [rec("3", v=1)])
        s = report["summary"]
        self.assertEqual((s["only_in_a"], s["only_in_b"], s["same"]), (1, 1, 0))


class TestCompareJobsStorage(unittest.TestCase):
    """End-to-end through the on-disk result layout (partition files)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = Storage(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_partitions(self, job_id, partitions):
        for i, records in enumerate(partitions):
            atomic_write_json(
                self.storage.path("jobs", job_id, "results", "reduce", f"part-{i:04d}.json"),
                {"job_id": job_id, "partition": i, "partition_name": f"part-{i:04d}",
                 "task_id": f"r-{i:04d}", "records": records, "count": len(records)},
            )

    def test_partition_count_and_order_irrelevant(self):
        # Job A: 4 partitions; job B: same logical records in 2 partitions,
        # different order.  Must compare identical.
        rng = random.Random(1)
        records = [rec(f"key-{i}", count=i, avg=round(rng.random(), 3)) for i in range(300)]
        shuffled = records[:]
        rng.shuffle(shuffled)
        self._write_partitions("job-a", [records[i::4] for i in range(4)])
        self._write_partitions("job-b", [shuffled[i::2] for i in range(2)])
        report = compare_jobs(self.storage, "job-a", "job-b")
        self.assertTrue(report["summary"]["identical"])
        self.assertEqual(report["a"]["records"], 300)
        self.assertEqual(report["b"]["records"], 300)

    def test_read_job_results_merges_partitions(self):
        self._write_partitions("job-x", [[rec("a", count=1)], [rec("b", count=2)]])
        records = read_job_results(self.storage, "job-x")
        self.assertEqual(len(records), 2)
        self.assertEqual(read_job_results(self.storage, "job-missing"), [])

    def test_diffs_found_across_partitions(self):
        self._write_partitions("job-a", [[rec("x", count=1), rec("y", count=2)],
                                         [rec("z", count=3)]])
        self._write_partitions("job-b", [[rec("x", count=1)],
                                         [rec("y", count=20), rec("w", count=4)]])
        report = compare_jobs(self.storage, "job-a", "job-b")
        self.assertEqual(diff_keys(report, DIFF_VALUE_MISMATCH), {"y"})
        self.assertEqual(diff_keys(report, DIFF_ONLY_IN_A), {"z"})
        self.assertEqual(diff_keys(report, DIFF_ONLY_IN_B), {"w"})


class TestCompareScale(unittest.TestCase):
    """Large, randomly shaped inputs checked against exact expectations."""

    def test_no_false_positives_no_misses(self):
        rng = random.Random(2024)
        a, b = [], []
        expected_only_a, expected_only_b, expected_mismatch = set(), set(), set()

        # 5000 common keys with identical values (order differs).
        for i in range(5000):
            value = {"key": f"common-{i}", "count": i, "avg": round(rng.random(), 3)}
            a.append(dict(value))
            b.append(dict(value))
        # 300 keys only in A, 250 only in B.
        for i in range(300):
            a.append(rec(f"only-a-{i}", count=i))
            expected_only_a.add(f"only-a-{i}")
        for i in range(250):
            b.append(rec(f"only-b-{i}", count=i))
            expected_only_b.add(f"only-b-{i}")
        # 400 keys with a numeric difference.
        for i in range(400):
            a.append(rec(f"diff-{i}", count=i))
            b.append(rec(f"diff-{i}", count=i + 1))
            expected_mismatch.add(f"diff-{i}")
        # 50 keys duplicated identically on both sides -> no diff.
        for i in range(50):
            for _ in range(3):
                a.append(rec(f"dup-{i}", count=i))
                b.append(rec(f"dup-{i}", count=i))
        # 40 keys with different multiplicity -> mismatch.
        for i in range(40):
            a.append(rec(f"dupskew-{i}", count=i))
            a.append(rec(f"dupskew-{i}", count=i))
            b.append(rec(f"dupskew-{i}", count=i))
            expected_mismatch.add(f"dupskew-{i}")
        # int/float duplicates across sides -> no diff.
        a.append(rec("numform", count=3))
        b.append(rec("numform", count=3.0))

        rng.shuffle(a)
        rng.shuffle(b)
        report = compare_record_sets(a, b)
        s = report["summary"]

        self.assertEqual(diff_keys(report, DIFF_ONLY_IN_A), expected_only_a)
        self.assertEqual(diff_keys(report, DIFF_ONLY_IN_B), expected_only_b)
        self.assertEqual(diff_keys(report, DIFF_VALUE_MISMATCH), expected_mismatch)
        self.assertEqual(s["diffs_total"],
                         len(expected_only_a) + len(expected_only_b) + len(expected_mismatch))
        self.assertEqual(s["records_only_in_a"], 300)
        self.assertEqual(s["records_only_in_b"], 250)
        self.assertEqual(s["same"], 5000 + 50 + 1)
        self.assertEqual(report["a"]["records"], len(a))
        self.assertEqual(report["b"]["records"], len(b))
        self.assertEqual(report["a"]["duplicate_keys"], 50 + 40)
        self.assertEqual(report["b"]["duplicate_keys"], 50)

    def test_rerun_determinism_at_scale(self):
        rng = random.Random(7)
        a = [rec(f"k{rng.randrange(2000)}", v=rng.random()) for _ in range(8000)]
        b = [rec(f"k{rng.randrange(2000)}", v=rng.random()) for _ in range(8000)]
        r1 = compare_record_sets(a, b)
        r2 = compare_record_sets(list(reversed(a)), list(reversed(b)))
        self.assertEqual(r1["summary"], r2["summary"])
        self.assertEqual(r1["diffs"], r2["diffs"])


class TestFilterDiffs(unittest.TestCase):
    def setUp(self):
        self.diffs = compare_record_sets(
            [rec("alpha", count=1), rec("beta", count=2), rec("gamma", count=3)],
            [rec("alpha", count=9), rec("delta", count=4)],
        )["diffs"]

    def test_filter_by_type(self):
        out = filter_diffs(self.diffs, diff_type=DIFF_ONLY_IN_B)
        self.assertEqual([d["key"] for d in out], ["delta"])
        out = filter_diffs(self.diffs, diff_type=DIFF_VALUE_MISMATCH)
        self.assertEqual([d["key"] for d in out], ["alpha"])

    def test_filter_by_query_case_insensitive(self):
        out = filter_diffs(self.diffs, query="ALP")
        self.assertEqual([d["key"] for d in out], ["alpha"])
        self.assertEqual(filter_diffs(self.diffs, query="zzz"), [])

    def test_filter_combined(self):
        out = filter_diffs(self.diffs, diff_type=DIFF_ONLY_IN_A, query="a")
        self.assertEqual({d["key"] for d in out}, {"beta", "gamma"})


if __name__ == "__main__":
    unittest.main()
