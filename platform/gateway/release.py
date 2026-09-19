"""Release rule: decides which part of a job's output may return to the agent.

The job ran untrusted code, so its output is untrusted too. Only the file declared in
the problem's artifact contract is released, and only after type checks and
small-group suppression. Rejection messages never echo output content, because an
error message is otherwise a way to smuggle data past the rule.

When the contract declares `group_by`, the platform does not take the UDF's word for
anything it can check itself: it recomputes each group's true size from the snapshot
(suppression uses the true size), rejects artifacts whose claimed counts differ, and
recomputes any metric the contract declares under `verify`.
"""

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

CASTS = {"str": str, "int": int, "float": float}
AGGREGATES = {"mean": "avg", "sum": "sum", "min": "min", "max": "max", "median": "median"}


@dataclass
class Release:
    released: bool
    csv_text: str = ""
    rows: int = 0
    suppressed: int = 0
    notes: list[str] = field(default_factory=list)


def ground_truth(spec: dict, snapshot: Path) -> dict[tuple, dict]:
    """True group sizes (and declared metrics) per group, computed from the snapshot."""
    keys = spec["group_by"]
    cols = ", ".join(f'"{k}"' for k in keys)
    metrics = "".join(f', {AGGREGATES[v["aggregate"]]}("{v["of"]}") AS "{name}"'
                      for name, v in spec.get("verify", {}).items())
    rows = duckdb.sql(f"SELECT {cols}, count(*) AS _n{metrics} FROM read_parquet('{snapshot}') GROUP BY {cols}")
    names = rows.columns
    out = {}
    for r in rows.fetchall():
        rec = dict(zip(names, r))
        out[tuple(str(rec[k]) for k in keys)] = rec
    return out


def apply(contract: dict, output_dir: Path, snapshot: Path | None = None) -> Release:
    spec = contract["artifact"]
    path = output_dir / spec["file"]
    if not path.is_file():
        return Release(False, notes=[f"missing declared artifact {spec['file']}"])

    size = path.stat().st_size
    if size > spec["max_bytes"]:
        return Release(False, notes=[f"artifact is {size} bytes; contract allows {spec['max_bytes']}"])

    cols = [c["name"] for c in spec["columns"]]
    casts = [CASTS[c["type"]] for c in spec["columns"]]
    reader = csv.reader(io.StringIO(path.read_text(errors="replace")))
    if next(reader, None) != cols:
        return Release(False, notes=[f"header does not match contract columns {cols}"])

    k_rule = spec.get("min_group_size")
    k_idx = cols.index(k_rule["column"]) if k_rule else None
    truth = ground_truth(spec, snapshot) if spec.get("group_by") and snapshot else None
    key_idx = [cols.index(k) for k in spec.get("group_by", [])]

    kept, suppressed, count_mismatch, metric_mismatch, unknown = [], 0, 0, 0, 0
    for lineno, row in enumerate(reader, start=2):
        if len(row) != len(cols):
            return Release(False, notes=[f"line {lineno}: expected {len(cols)} fields"])
        try:
            typed = [cast(value) for cast, value in zip(casts, row)]
        except ValueError:
            return Release(False, notes=[f"line {lineno}: a value does not match its declared column type"])

        if truth is not None:
            actual = truth.get(tuple(str(typed[i]) for i in key_idx))
            if actual is None:          # a group that doesn't exist in the snapshot
                unknown += 1
                continue
            true_n = actual["_n"]
            if typed[k_idx] != true_n:
                count_mismatch += 1
            for name, v in spec.get("verify", {}).items():
                claimed, real = typed[cols.index(name)], actual[name]
                if real is None or abs(claimed - real) > v.get("tolerance", 0.01) * max(1.0, abs(real)):
                    metric_mismatch += 1
            if k_rule and true_n < k_rule["k"]:
                suppressed += 1
                continue
        elif k_rule and typed[k_idx] < k_rule["k"]:
            suppressed += 1
            continue
        kept.append(row)

    if count_mismatch:
        return Release(False, notes=[f"{count_mismatch} row(s) claim a {k_rule['column']} that differs from the "
                                     "group's true size in the snapshot; counts must be computed, not written"])
    if metric_mismatch:
        return Release(False, notes=[f"{metric_mismatch} value(s) differ from the platform's recomputation of "
                                     f"{', '.join(spec['verify'])}"])
    if unknown:
        return Release(False, notes=[f"{unknown} row(s) name groups that do not exist in the snapshot"])
    if len(kept) > spec["max_rows"]:
        return Release(False, notes=[f"{len(kept)} rows; contract allows {spec['max_rows']}"])

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(cols)
    writer.writerows(kept)

    notes = []
    if suppressed:
        basis = "true group size" if truth is not None else k_rule["column"]
        notes.append(f"suppressed {suppressed} row(s) with {basis} < {k_rule['k']}")
    extra = sorted(p.name for p in output_dir.iterdir() if p.name != spec["file"])
    if extra:
        notes.append(f"ignored {len(extra)} undeclared output file(s)")
    return Release(True, out.getvalue(), len(kept), suppressed, notes)
