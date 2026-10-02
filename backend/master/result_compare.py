"""Align and diff the reduce results of two jobs by record key.

Users frequently need to know how two runs of the same business computation
differ (two versions, two parameter sets).  The two jobs may differ in scale,
partition count and record order, so a positional diff is meaningless — this
module aligns records by their *identity* and compares values:

* **identity** — a record's ``key`` field; records without one are their own
  identity (they only match an identical record on the other side);
* **duplicates** — a key may appear several times on either side; groups are
  then compared as multisets of canonical record signatures, so identical
  duplication is *not* flagged while a multiplicity or content difference is;
* **numeric unification** — ``3`` and ``3.0`` are the same value (JSON does
  not distinguish them reliably across reducers), but ``True`` is never equal
  to ``1`` and a string is never equal to a number, so real differences are
  never silently absorbed;
* **tolerance** — an optional relative/absolute tolerance for numeric fields
  (never applied to keys: keys are identifiers, not measurements).

The module is pure (no Flask, no globals) so the whole alignment logic is
unit-testable; the HTTP layer in ``server.py`` is a thin shell over it.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any, Optional

from backend.common import constants as C
from backend.common.jsonutil import sanitize
from backend.common.storage import Storage, list_files, read_json

# ---------------------------------------------------------------------------
# Diff types (the vocabulary shared with the frontend)
# ---------------------------------------------------------------------------
DIFF_ONLY_IN_A = "only_in_a"          # key present only in job A
DIFF_ONLY_IN_B = "only_in_b"          # key present only in job B
DIFF_VALUE_MISMATCH = "value_mismatch"  # key on both sides, content differs

DIFF_TYPES = [DIFF_VALUE_MISMATCH, DIFF_ONLY_IN_A, DIFF_ONLY_IN_B]

# How many sample records a diff entry carries per side for duplicate groups.
SAMPLE_RECORDS = 10


# ---------------------------------------------------------------------------
# Value normalisation and equality
# ---------------------------------------------------------------------------
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_numbers(value: Any) -> Any:
    """Recursively rewrite integral floats as ints so ``3`` and ``3.0`` hash alike."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {k: _normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_numbers(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    """Stable string identity for any JSON value, used for grouping.

    Integral floats are unified with ints; everything else keeps its JSON
    type, so ``3`` groups with ``3.0`` but never with ``"3"`` or ``True``.
    """
    return json.dumps(
        sanitize(_normalize_numbers(value)),
        sort_keys=True, ensure_ascii=False, default=str,
    )


def _numbers_equal(a: float, b: float, rel_tol: float, abs_tol: float) -> bool:
    fa, fb = float(a), float(b)
    if math.isnan(fa) or math.isnan(fb):
        return math.isnan(fa) and math.isnan(fb)
    if fa == fb:  # exact match, covers int/float unification and inf == inf
        return True
    if math.isinf(fa) or math.isinf(fb):
        return False
    return abs(fa - fb) <= max(abs_tol, rel_tol * max(abs(fa), abs(fb)))


def values_equal(a: Any, b: Any, rel_tol: float = 0.0, abs_tol: float = 0.0) -> bool:
    """Type-strict equality with numeric unification and optional tolerance.

    Numbers compare numerically (with tolerance); booleans, strings, nulls,
    lists and dicts compare only within their own type, recursively.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return _numbers_equal(a, b, rel_tol, abs_tol)
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(values_equal(a[k], b[k], rel_tol, abs_tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(values_equal(x, y, rel_tol, abs_tol) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


# ---------------------------------------------------------------------------
# Record identity and grouping
# ---------------------------------------------------------------------------
def _identity_key(record: Any) -> Any:
    """The alignment key of a result record: its ``key`` field, else itself."""
    if isinstance(record, dict) and "key" in record:
        return record["key"]
    return record


def _group_by_key(records: list[Any]) -> dict[str, dict]:
    """Group records by canonical identity key, preserving the original key."""
    groups: dict[str, dict] = {}
    for rec in records:
        key = _identity_key(rec)
        ck = _canonical(key)
        group = groups.get(ck)
        if group is None:
            group = {"key": key, "records": []}
            groups[ck] = group
        group["records"].append(rec)
    return groups


# ---------------------------------------------------------------------------
# Per-key comparison
# ---------------------------------------------------------------------------
def _field_diffs(rec_a: dict, rec_b: dict, rel_tol: float, abs_tol: float) -> list[dict]:
    """Field-by-field comparison of two dict records (excluding ``key``)."""
    out: list[dict] = []
    names = sorted((set(rec_a) | set(rec_b)) - {"key"}, key=str)
    for name in names:
        in_a = name in rec_a
        in_b = name in rec_b
        va = rec_a.get(name)
        vb = rec_b.get(name)
        same = in_a and in_b and values_equal(va, vb, rel_tol, abs_tol)
        entry: dict[str, Any] = {"field": name, "a": va, "b": vb, "same": same}
        if not in_a:
            entry["missing"] = "a"
        elif not in_b:
            entry["missing"] = "b"
        if _is_number(va) and _is_number(vb):
            entry["delta"] = float(vb) - float(va)
        out.append(entry)
    return out


def _compare_group(key: Any, recs_a: list, recs_b: list,
                   rel_tol: float, abs_tol: float, sample: int) -> Optional[dict]:
    """Compare the aligned record groups for one key; ``None`` means identical."""
    if not recs_a or not recs_b:
        # Sort samples canonically so the report is a pure function of the
        # input *multiset*, independent of record/partition order.
        only_type = DIFF_ONLY_IN_B if not recs_a else DIFF_ONLY_IN_A
        return {
            "type": only_type, "key": key,
            "count_a": len(recs_a), "count_b": len(recs_b),
            "records_a": sorted(recs_a, key=_canonical)[:sample],
            "records_b": sorted(recs_b, key=_canonical)[:sample],
        }
    if len(recs_a) == 1 and len(recs_b) == 1:
        ra, rb = recs_a[0], recs_b[0]
        if values_equal(ra, rb, rel_tol, abs_tol):
            return None
        diff: dict[str, Any] = {
            "type": DIFF_VALUE_MISMATCH, "key": key,
            "count_a": 1, "count_b": 1, "a": ra, "b": rb,
        }
        if isinstance(ra, dict) and isinstance(rb, dict):
            diff["fields"] = _field_diffs(ra, rb, rel_tol, abs_tol)
        return diff
    # Duplicate identifiers on at least one side: multiset semantics.  The
    # canonical signature is exact (tolerance cannot be hashed), so identical
    # duplication passes silently while any multiplicity/content skew is shown
    # with both samples and counts.
    sigs_a = Counter(_canonical(r) for r in recs_a)
    sigs_b = Counter(_canonical(r) for r in recs_b)
    if sigs_a == sigs_b:
        return None
    return {
        "type": DIFF_VALUE_MISMATCH, "key": key,
        "count_a": len(recs_a), "count_b": len(recs_b),
        "duplicate": True,
        "records_a": sorted(recs_a, key=_canonical)[:sample],
        "records_b": sorted(recs_b, key=_canonical)[:sample],
    }


# ---------------------------------------------------------------------------
# Top-level comparison
# ---------------------------------------------------------------------------
def _side_stats(records: list[Any], groups: dict[str, dict]) -> dict:
    return {
        "records": len(records),
        "keys": len(groups),
        "duplicate_keys": sum(1 for g in groups.values() if len(g["records"]) > 1),
    }


def compare_record_sets(records_a: list[Any], records_b: list[Any],
                        tolerance: float = 0.0,
                        sample_records: int = SAMPLE_RECORDS) -> dict:
    """Compare two flat result-record lists, aligning by record key.

    ``tolerance`` is applied both relatively and absolutely to numeric fields
    (a difference within either bound counts as equal); keys always align
    exactly.  Returns side stats, a summary and the full, deterministically
    ordered diff list.
    """
    rel_tol = abs_tol = max(0.0, float(tolerance or 0.0))
    groups_a = _group_by_key(records_a)
    groups_b = _group_by_key(records_b)

    diffs: list[dict] = []
    same = 0
    mismatched = 0
    only_a = only_b = 0
    records_only_a = records_only_b = 0

    for ck in sorted(set(groups_a) | set(groups_b)):
        ga = groups_a.get(ck)
        gb = groups_b.get(ck)
        key = ga["key"] if ga is not None else gb["key"]
        recs_a = ga["records"] if ga is not None else []
        recs_b = gb["records"] if gb is not None else []
        diff = _compare_group(key, recs_a, recs_b, rel_tol, abs_tol, sample_records)
        if diff is None:
            same += 1
        elif diff["type"] == DIFF_ONLY_IN_A:
            only_a += 1
            records_only_a += diff["count_a"]
            diffs.append(diff)
        elif diff["type"] == DIFF_ONLY_IN_B:
            only_b += 1
            records_only_b += diff["count_b"]
            diffs.append(diff)
        else:
            mismatched += 1
            diffs.append(diff)

    common = same + mismatched
    return {
        "a": _side_stats(records_a, groups_a),
        "b": _side_stats(records_b, groups_b),
        "summary": {
            "common_keys": common,
            "same": same,
            "value_mismatch": mismatched,
            "only_in_a": only_a,
            "only_in_b": only_b,
            "records_only_in_a": records_only_a,
            "records_only_in_b": records_only_b,
            "diffs_total": len(diffs),
            "identical": not diffs,
        },
        "diffs": diffs,
    }


def filter_diffs(diffs: list[dict], diff_type: str = "", query: str = "") -> list[dict]:
    """Filter a diff list by type and/or a case-insensitive key substring."""
    out = diffs
    if diff_type:
        out = [d for d in out if d["type"] == diff_type]
    if query:
        needle = str(query).lower()
        out = [d for d in out if needle in str(d["key"]).lower()]
    return out


# ---------------------------------------------------------------------------
# Storage-backed helpers (single code path for the HTTP layer and tests)
# ---------------------------------------------------------------------------
def read_job_results(storage: Storage, job_id: str) -> list[Any]:
    """Read every reduce-result record of a job, across all partitions."""
    records: list[Any] = []
    root = storage.path("jobs", job_id, "results", C.STAGE_REDUCE)
    for path in list_files(root, suffix=".json"):
        doc = read_json(path)
        if isinstance(doc, dict):
            records.extend(doc.get("records", []))
    return records


def compare_jobs(storage: Storage, job_id_a: str, job_id_b: str,
                 tolerance: float = 0.0) -> dict:
    """Load the stored results of two jobs and compare them."""
    return compare_record_sets(
        read_job_results(storage, job_id_a),
        read_job_results(storage, job_id_b),
        tolerance=tolerance,
    )
