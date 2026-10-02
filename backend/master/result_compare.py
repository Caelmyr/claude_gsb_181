"""Key-aligned comparison for two completed jobs' result sets.

The comparison deliberately does *not* compare records by array position. Result
files can have a different number of partitions and records, and partitioning or
reducer output order can change between runs. Records are grouped by their
business identifier first. Within one identifier, duplicate records are treated
as multisets and paired by their canonical value, so a harmless reordering is
not reported as a difference.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from backend.common import constants as C
from backend.common.storage import Storage, list_files, read_json

EQUAL = "equal"
CHANGED = "changed"
LEFT_ONLY = "left_only"
RIGHT_ONLY = "right_only"
DUPLICATE = "duplicate"

DIFF_STATUSES = {CHANGED, LEFT_ONLY, RIGHT_ONLY, DUPLICATE}


@dataclass(frozen=True)
class ResultRecord:
    """One result record with enough metadata to locate its source partition."""

    record: dict[str, Any]
    index: int
    partition: Optional[int] = None
    partition_name: str = ""
    task_id: str = ""


@dataclass(frozen=True)
class _Occurrence:
    record: dict[str, Any]
    index: int
    partition: Optional[int]
    partition_name: str
    task_id: str
    key_field: str

    @property
    def payload(self) -> dict[str, Any]:
        # ``key`` has already been used for alignment and is shown separately.
        return {k: v for k, v in self.record.items() if k != self.key_field}


def load_result_records(storage: Storage, job_id: str) -> list[ResultRecord]:
    """Read all reduce result files for a job, ignoring partition boundaries."""
    out: list[ResultRecord] = []
    root = storage.path("jobs", job_id, "results", C.STAGE_REDUCE)
    for path in list_files(root, suffix=".json"):
        doc = read_json(path)
        if not doc:
            continue
        partition = doc.get("partition")
        partition_name = str(doc.get("partition_name") or "")
        task_id = str(doc.get("task_id") or "")
        for rec in doc.get("records", []):
            out.append(ResultRecord(
                record=rec,
                index=len(out),
                partition=partition,
                partition_name=partition_name,
                task_id=task_id,
            ))
    return out


def compare_result_records(
    left: Iterable[ResultRecord],
    right: Iterable[ResultRecord],
    key_field: str = "key",
    numeric_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Compare two result record collections by identifier, not position.

    Returns every aligned key group, including equal groups. Callers use
    :func:`page_comparison` to filter and paginate the potentially large result.
    """
    if not key_field:
        raise ValueError("key_field is required")
    numeric_tolerance = float(numeric_tolerance)
    if not math.isfinite(numeric_tolerance) or numeric_tolerance < 0:
        raise ValueError("numeric_tolerance must be a finite non-negative number")

    groups: dict[Any, dict[str, list[_Occurrence]]] = defaultdict(
        lambda: {"left": [], "right": []}
    )
    for side, records in (("left", left), ("right", right)):
        for item in records:
            if not isinstance(item.record, dict):
                raise ValueError(f"result record at position {item.index} is not an object")
            if key_field not in item.record:
                raise ValueError(
                    f"result record at position {item.index} is missing key field {key_field!r}"
                )
            occ = _Occurrence(
                record=item.record,
                index=item.index,
                partition=item.partition,
                partition_name=item.partition_name,
                task_id=item.task_id,
                key_field=key_field,
            )
            groups[item.record.get(key_field)][side].append(occ)

    compared: list[dict[str, Any]] = []
    summary = {
        "left_records": 0,
        "right_records": 0,
        "total_keys": 0,
        "equal_keys": 0,
        "changed_keys": 0,
        "left_only_keys": 0,
        "right_only_keys": 0,
        "duplicate_status_keys": 0,
        "duplicate_identifier_keys": 0,
        "different_keys": 0,
    }

    for key, buckets in groups.items():
        left_occ = buckets["left"]
        right_occ = buckets["right"]
        summary["left_records"] += len(left_occ)
        summary["right_records"] += len(right_occ)

        pairs, extra_left, extra_right = _pair_occurrences(left_occ, right_occ, numeric_tolerance)
        surplus_left = {o.index for o in extra_left}
        surplus_right = {o.index for o in extra_right}
        pair_equal = [
            payloads_equal(a.payload, b.payload, numeric_tolerance) for a, b in pairs
        ]
        all_paired_equal = all(pair_equal) if pair_equal else True
        duplicate_identifier = len(left_occ) != 1 or len(right_occ) != 1

        if not left_occ or not right_occ:
            status = LEFT_ONLY if left_occ else RIGHT_ONLY
        elif not all_paired_equal:
            status = CHANGED
        elif duplicate_identifier or len(left_occ) != len(right_occ):
            status = DUPLICATE
        else:
            status = EQUAL

        differences: list[dict[str, Any]] = []
        if status == CHANGED:
            for (a, b), is_equal in zip(pairs, pair_equal):
                if not is_equal:
                    collect_payload_diffs(a.payload, b.payload, numeric_tolerance, differences)

        group = {
            "key": key,
            "status": status,
            "equal": status == EQUAL,
            "duplicate": duplicate_identifier,
            "left_count": len(left_occ),
            "right_count": len(right_occ),
            "differences": differences,
            "left_rows": [_row_view(o, surplus=o.index in surplus_left) for o in left_occ],
            "right_rows": [_row_view(o, surplus=o.index in surplus_right) for o in right_occ],
        }
        compared.append(group)

        summary["total_keys"] += 1
        if status == EQUAL:
            summary["equal_keys"] += 1
        elif status == CHANGED:
            summary["changed_keys"] += 1
        elif status == LEFT_ONLY:
            summary["left_only_keys"] += 1
        elif status == RIGHT_ONLY:
            summary["right_only_keys"] += 1
        elif status == DUPLICATE:
            summary["duplicate_status_keys"] += 1
        if duplicate_identifier:
            summary["duplicate_identifier_keys"] += 1
        if status in DIFF_STATUSES:
            summary["different_keys"] += 1

    compared.sort(key=lambda g: _group_sort_key(g["key"]))
    return {
        "key_field": key_field,
        "numeric_tolerance": numeric_tolerance,
        "summary": summary,
        "groups": compared,
    }


def page_comparison(
    comparison: dict[str, Any],
    query: str = "",
    statuses: Optional[Iterable[str]] = None,
    page: int = 1,
    page_size: int = 200,
) -> dict[str, Any]:
    """Filter difference groups by key/status and return one deterministic page."""
    page = max(1, int(page))
    page_size = min(1000, max(1, int(page_size)))
    if statuses is None:
        wanted = None
    else:
        wanted = {s for s in statuses if s}
        if not wanted:
            return {
                "items": [],
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": 0,
                    "total_pages": 1,
                },
            }
    needle = str(query or "").strip().lower()

    filtered: list[dict[str, Any]] = []
    for group in comparison["groups"]:
        if group["status"] == EQUAL:
            continue
        if wanted is not None and not any(
            (s == "duplicate" and group.get("duplicate")) or s == group["status"]
            for s in wanted
        ):
            continue
        if needle and needle not in str(group["key"]).lower():
            continue
        filtered.append(group)

    total = len(filtered)
    start = (page - 1) * page_size
    return {
        "items": filtered[start:start + page_size],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, math.ceil(total / page_size)) if total else 1,
        },
    }


# ---------------------------------------------------------------------------
# Alignment / equality
# ---------------------------------------------------------------------------
def _pair_occurrences(
    left: list[_Occurrence], right: list[_Occurrence], tolerance: float
) -> tuple[list[tuple[_Occurrence, _Occurrence]], list[_Occurrence], list[_Occurrence]]:
    """Pair duplicate occurrences as multisets, independent of input order."""
    ls = sorted(left, key=lambda o: (canonical_value(o.payload), o.index))
    rs = sorted(right, key=lambda o: (canonical_value(o.payload), o.index))

    pairs: list[tuple[_Occurrence, _Occurrence]] = []
    i = j = 0
    while i < len(ls) and j < len(rs):
        lk = canonical_value(ls[i].payload)
        rk = canonical_value(rs[j].payload)
        if lk == rk:
            pairs.append((ls[i], rs[j]))
            ls.pop(i)
            rs.pop(j)
        elif lk < rk:
            i += 1
        else:
            j += 1

    # A non-zero tolerance can make non-identical canonical values equal. Match
    # those greedily after exact matches, retaining deterministic source order.
    if tolerance > 0 and ls and rs:
        used_left: set[int] = set()
        used_right: set[int] = set()
        for li, loc in enumerate(ls):
            for ri, roc in enumerate(rs):
                if ri in used_right:
                    continue
                if payloads_equal(loc.payload, roc.payload, tolerance):
                    pairs.append((loc, roc))
                    used_left.add(li)
                    used_right.add(ri)
                    break
        ls = [x for i, x in enumerate(ls) if i not in used_left]
        rs = [x for i, x in enumerate(rs) if i not in used_right]

    # Remaining records are aligned by canonical order; extras identify a
    # multiplicity difference without assigning it based on file position.
    remaining = min(len(ls), len(rs))
    pairs.extend((ls[i], rs[i]) for i in range(remaining))
    return pairs, ls[remaining:], rs[remaining:]


def canonical_value(value: Any) -> Any:
    """Return a hashable/orderable representation where 1 and 1.0 are equal."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number):
            return ("number", "nan")
        return ("number", number)
    if value is None:
        return ("null",)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, dict):
        return ("dict", tuple(sorted(
            (str(k), canonical_value(v)) for k, v in value.items()
        )))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(canonical_value(v) for v in value))
    return ("other", str(type(value).__name__), str(value))


def payloads_equal(left: Any, right: Any, tolerance: float = 0.0) -> bool:
    """Deep equality with numeric semantics; booleans stay distinct from numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if math.isnan(float(left)) and math.isnan(float(right)):
            return True
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(payloads_equal(left[k], right[k], tolerance) for k in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            payloads_equal(a, b, tolerance) for a, b in zip(left, right)
        )
    return left == right


def collect_payload_diffs(
    left: Any, right: Any, tolerance: float, out: list[dict[str, Any]], path: str = ""
) -> None:
    """Append concrete leaf-level value differences with JSON-path-like locations."""
    if payloads_equal(left, right, tolerance):
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in left:
                out.append({"path": child, "left": None, "right": right[key], "kind": "missing_left"})
            elif key not in right:
                out.append({"path": child, "left": left[key], "right": None, "kind": "missing_right"})
            else:
                collect_payload_diffs(left[key], right[key], tolerance, out, child)
    elif isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        for idx in range(max(len(left), len(right))):
            child = f"{path}[{idx}]"
            if idx >= len(left):
                out.append({"path": child, "left": None, "right": right[idx], "kind": "missing_left"})
            elif idx >= len(right):
                out.append({"path": child, "left": left[idx], "right": None, "kind": "missing_right"})
            else:
                collect_payload_diffs(left[idx], right[idx], tolerance, out, child)
    else:
        kind = "numeric" if isinstance(left, (int, float)) and isinstance(right, (int, float)) else "value"
        out.append({"path": path, "left": left, "right": right, "kind": kind})


def _row_view(occ: _Occurrence, surplus: bool = False) -> dict[str, Any]:
    return {
        "index": occ.index,
        "partition": occ.partition,
        "partition_name": occ.partition_name,
        "task_id": occ.task_id,
        "record": occ.record,
        "payload": occ.payload,
        "surplus": surplus,
    }


def _group_sort_key(key: Any) -> tuple:
    if isinstance(key, bool):
        return (3, int(key), "")
    if isinstance(key, (int, float)) and not isinstance(key, bool):
        return (0, float(key), "")
    if isinstance(key, str):
        return (1, 0.0, key)
    if key is None:
        return (4, 0.0, "")
    return (5, 0.0, str(key))
