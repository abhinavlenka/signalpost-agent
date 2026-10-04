"""Which parts of an envelope describe the company, and which describe one particular run.

A company record must be the same whenever the same source content is read: on a first run, on a
rerun from an empty state directory, and on a refresh over the previous run's state. Everything that
belongs to a single execution is kept in three places only, all of them part of the output contract:

- ``run``: run id, start and end time, and what the run was compared against;
- ``operations``: request count, runtime and cost;
- ``evidence[].retrieved_at``: when each source was fetched.

``semantic_record`` removes exactly those three and nothing else, so two envelopes describe the same
facts if and only if their semantic records are equal.

Inside those three places only the contract's own key names are used (``RUN_SCOPED_KEY_NAMES``), so
a comparison that drops volatile values by key name, wherever they appear, reaches the same verdict
as one that drops the three places whole. Key order is fixed as well: ``same_serialisation`` checks
that two equal records are also written out identically.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

RUN_SCOPED_KEYS = ("run", "operations")
RUN_SCOPED_EVIDENCE_KEYS = ("retrieved_at",)
# The only key names, anywhere in an envelope, whose values may differ between two runs over the same sources.
RUN_SCOPED_KEY_NAMES = {"run_id", "started_at", "completed_at", "retrieved_at", "requests", "runtime_ms"}


def semantic_record(envelope: dict[str, Any]) -> dict[str, Any]:
    record = copy.deepcopy(envelope)
    for key in RUN_SCOPED_KEYS:
        record.pop(key, None)
    for item in record.get("evidence") or []:
        for key in RUN_SCOPED_EVIDENCE_KEYS:
            item.pop(key, None)
    return record


def same_serialisation(before: Any, after: Any) -> bool:
    """True when two records are written out identically, key order included."""
    dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    return dump(before) == dump(after)


def load_records(path: str | Path) -> dict[str, dict[str, Any]]:
    """Semantic records keyed by organisation number, from an envelopes.jsonl file or its directory."""
    source = Path(path)
    if source.is_dir():
        source = source / "envelopes.jsonl"
    records: dict[str, dict[str, Any]] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip():
            envelope = json.loads(line)
            records[str(envelope.get("organisation_number"))] = semantic_record(envelope)
    return records


def differences(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Every leaf that differs between two records, as (path, before, after)."""
    if isinstance(before, dict) and isinstance(after, dict):
        found: list[tuple[str, Any, Any]] = []
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                found.append((f"{path}.{key}", before.get(key, "<absent>"), after.get(key, "<absent>")))
            else:
                found.extend(differences(before[key], after[key], f"{path}.{key}"))
        return found
    if isinstance(before, list) and isinstance(after, list):
        found = [(f"{path}[length]", len(before), len(after))] if len(before) != len(after) else []
        for left, right in zip(before, after):
            found.extend(differences(left, right, f"{path}[]"))
        return found
    return [] if before == after else [(path, before, after)]
