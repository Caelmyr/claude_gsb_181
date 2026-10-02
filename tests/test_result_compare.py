"""Tests for key-aligned result comparison across ordering, size and duplicates."""

from __future__ import annotations

import math
import unittest

from backend.master.result_compare import (
    CHANGED,
    DUPLICATE,
    EQUAL,
    LEFT_ONLY,
    RIGHT_ONLY,
    ResultRecord,
    compare_result_records,
    page_comparison,
    payloads_equal,
)


def rr(record: dict, index: int, partition: int = 0) -> ResultRecord:
    return ResultRecord(
        record=record,
        index=index,
        partition=partition,
        partition_name=f"r-{partition:04d}",
        task_id=f"r-{partition:04d}",
    )


def by_key(comparison, key):
    return next(group for group in comparison["groups"] if group["key"] == key)


class ResultCompareTests(unittest.TestCase):
    def test_aligns_by_key_ignoring_position_partition_and_order(self):
        left = [
            rr({"key": "a", "count": 1}, 0, partition=0),
            rr({"key": "b", "count": 2}, 1, partition=1),
        ]
        right = [
            rr({"key": "b", "count": 2}, 0, partition=7),
            rr({"key": "a", "count": 1}, 1, partition=3),
        ]
        result = compare_result_records(left, right)
        self.assertEqual(result["summary"]["equal_keys"], 2)
        self.assertEqual(result["summary"]["different_keys"], 0)
        self.assertEqual([g["key"] for g in result["groups"]], ["a", "b"])

    def test_detects_missing_records_and_value_differences(self):
        result = compare_result_records(
            [rr({"key": "same", "count": 1}, 0), rr({"key": "only_left", "count": 3}, 1)],
            [rr({"key": "same", "count": 9}, 0), rr({"key": "only_right", "count": 2}, 1)],
        )
        summary = result["summary"]
        self.assertEqual(summary["left_only_keys"], 1)
        self.assertEqual(summary["right_only_keys"], 1)
        self.assertEqual(summary["changed_keys"], 1)
        self.assertEqual(by_key(result, "same")["differences"][0]["path"], "count")

    def test_duplicate_order_aligns_as_multiset_without_false_changed(self):
        result = compare_result_records(
            [rr({"key": "k", "count": 1}, 0), rr({"key": "k", "count": 2}, 1)],
            [rr({"key": "k", "count": 2}, 0), rr({"key": "k", "count": 1}, 1)],
        )
        group = by_key(result, "k")
        self.assertTrue(group["duplicate"])
        self.assertEqual(group["status"], DUPLICATE)
        self.assertEqual(group["differences"], [])

    def test_duplicate_multiplicity_is_reported_even_when_one_record_matches(self):
        result = compare_result_records(
            [rr({"key": "k", "count": 1}, 0), rr({"key": "k", "count": 2}, 1)],
            [rr({"key": "k", "count": 1}, 0)],
        )
        group = by_key(result, "k")
        self.assertEqual(group["status"], DUPLICATE)
        self.assertEqual((group["left_count"], group["right_count"]), (2, 1))
        self.assertEqual([r["surplus"] for r in group["left_rows"]], [False, True])

    def test_duplicate_values_are_compared_after_multiset_alignment(self):
        result = compare_result_records(
            [rr({"key": "k", "count": 1}, 0), rr({"key": "k", "count": 3}, 1)],
            [rr({"key": "k", "count": 2}, 0), rr({"key": "k", "count": 1}, 1)],
        )
        group = by_key(result, "k")
        self.assertEqual(group["status"], CHANGED)
        self.assertEqual([d["path"] for d in group["differences"]], ["count"])

    def test_numeric_tolerance_and_type_semantics(self):
        self.assertTrue(payloads_equal(1, 1.0, 0.0))
        self.assertFalse(payloads_equal(True, 1, 0.0))
        self.assertTrue(payloads_equal(1.0, 1.0001, 0.001))
        self.assertTrue(payloads_equal(math.nan, math.nan, 0.0))
        result = compare_result_records(
            [rr({"key": "n", "count": 1.0}, 0)],
            [rr({"key": "n", "count": 1.0001}, 0)],
            numeric_tolerance=0.001,
        )
        self.assertEqual(by_key(result, "n")["status"], EQUAL)

    def test_pagination_filters_differences_and_search(self):
        left = [rr({"key": f"a{i}", "count": i}, i) for i in range(3)]
        right = [
            rr({"key": "a0", "count": 0}, 0),
            rr({"key": "a1", "count": 99}, 1),
            rr({"key": "other", "count": 1}, 2),
        ]
        comparison = compare_result_records(left, right)
        changed = page_comparison(comparison, statuses=["changed"], page_size=10)
        self.assertEqual([g["key"] for g in changed["items"]], ["a1"])
        searched = page_comparison(comparison, query="other", page_size=10)
        self.assertEqual(searched["pagination"]["total"], 1)
        page = page_comparison(comparison, page=2, page_size=1)
        self.assertEqual(page["pagination"]["total"], 3)
        self.assertEqual(len(page["items"]), 1)

    def test_empty_status_filter_is_not_treated_as_all_statuses(self):
        result = compare_result_records(
            [rr({"key": "a", "count": 1}, 0)],
            [rr({"key": "a", "count": 2}, 0)],
        )
        page = page_comparison(result, statuses=[], page_size=10)
        self.assertEqual(page["items"], [])

    def test_missing_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing key field"):
            compare_result_records([rr({"id": "x"}, 0)], [rr({"key": "x"}, 0)], key_field="key")


if __name__ == "__main__":
    unittest.main()
