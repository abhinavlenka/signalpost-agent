"""Compare two runs of the same batch, company by company.

    uv run python scripts/check_determinism.py out/run1 out/run2

Envelopes are matched by organisation number, not by line order. Run metadata (``run``,
``operations`` and ``evidence[].retrieved_at``) is left out; every other field must be equal, and
equal records must also be written out with the same key order.
Exit code 0 means zero semantic differences.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from abhikilde.determinism import differences, load_records, same_serialisation  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first", help="envelopes.jsonl of the first run, or its directory")
    parser.add_argument("second", help="envelopes.jsonl of the second run, or its directory")
    parser.add_argument("--show", type=int, default=20, help="how many differing fields to print")
    args = parser.parse_args()
    first, second = load_records(args.first), load_records(args.second)
    only_first, only_second = sorted(set(first) - set(second)), sorted(set(second) - set(first))
    fields: Counter[str] = Counter()
    example: dict[str, tuple[str, object, object]] = {}
    differing: list[str] = []
    reordered: list[str] = []
    for org in sorted(set(first) & set(second)):
        found = differences(first[org], second[org])
        if found:
            differing.append(org)
        elif not same_serialisation(first[org], second[org]):
            reordered.append(org)
        for path in {path for path, _, _ in found}:
            fields[path] += 1
        for path, before, after in found:
            example.setdefault(path, (org, before, after))
    print(f"companies: {len(first)} and {len(second)}; compared {len(set(first) & set(second))}; differing {len(differing)}; same values in a different key order {len(reordered)}")
    if only_first or only_second:
        print(f"only in first: {only_first[:10]}; only in second: {only_second[:10]}")
    for path, count in fields.most_common(args.show):
        org, before, after = example[path]
        print(f"  {count:>5} companies  {path}\n          {org}: {str(before)[:100]!r} -> {str(after)[:100]!r}")
    if differing:
        print("differing organisation numbers:", ", ".join(differing[:30]) + (" …" if len(differing) > 30 else ""))
    if reordered:
        print("key order differs for:", ", ".join(reordered[:30]) + (" …" if len(reordered) > 30 else ""))
    sys.exit(1 if differing or reordered or only_first or only_second else 0)


if __name__ == "__main__":
    main()
