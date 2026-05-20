#!/usr/bin/env python3
"""Profile a CSV file using only the stdlib. Prints JSON to stdout."""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path


_INT_RE   = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+([eE][+-]?\d+)?$")


def _guess_dtype(values: list[str]) -> str:
    sample = [v for v in values if v != ""][:200]
    if not sample:
        return "empty"
    if all(_INT_RE.match(v) for v in sample):
        return "int"
    if all(_INT_RE.match(v) or _FLOAT_RE.match(v) for v in sample):
        return "float"
    return "str"


def profile(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {"rows": 0, "cols": 0, "columns": []}
        cols = [{"name": h, "values": [], "nulls": 0, "counter": Counter()}
                for h in header]
        rows = 0
        for row in reader:
            rows += 1
            for i, h in enumerate(header):
                v = row[i] if i < len(row) else ""
                if v == "":
                    cols[i]["nulls"] += 1
                else:
                    if len(cols[i]["values"]) < 200:
                        cols[i]["values"].append(v)
                    cols[i]["counter"][v] += 1

    out_cols = []
    for c in cols:
        out_cols.append({
            "name": c["name"],
            "dtype_guess": _guess_dtype(c["values"]),
            "nulls": c["nulls"],
            "unique": len(c["counter"]),
            "top": c["counter"].most_common(5),
        })
    return {"rows": rows, "cols": len(header), "columns": out_cols}


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: profile.py <path>"}))
        sys.exit(2)
    p = Path(sys.argv[1])
    if not p.is_file():
        print(json.dumps({"error": f"not a file: {p}"}))
        sys.exit(2)
    print(json.dumps(profile(p), ensure_ascii=False))


if __name__ == "__main__":
    main()
