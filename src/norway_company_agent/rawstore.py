"""Content-addressed store of the raw responses that claims were extracted from.

Every fetched body that becomes evidence is written once as ``raw/<aa>/<sha256>.gz`` under the state
directory, so a claim's ``content_sha256`` can be checked against the bytes it came from. The store is
optional: without one (library use, tests) nothing is written and evidence carries no snapshot path.
"""
from __future__ import annotations

import gzip
import os
import threading
from pathlib import Path

_root: Path | None = None
_lock = threading.Lock()


def set_raw_store(state_dir: str | Path | None) -> None:
    global _root
    _root = Path(state_dir) if state_dir else None


def _relative(sha256: str) -> str:
    return f"raw/{sha256[:2]}/{sha256}.gz"


def save_raw(sha256: str | None, raw: bytes) -> None:
    """Best effort: a full disk or a permission error must never fail a fetch."""
    if _root is None or not sha256 or not raw:
        return
    target = _root / _relative(sha256)
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_bytes(gzip.compress(raw, compresslevel=6))
        temporary.replace(target)
    except OSError:
        pass


def snapshot_path(sha256: str | None) -> str | None:
    """Path of the stored snapshot relative to the state directory, or None when it was not kept."""
    if _root is None or not sha256:
        return None
    return _relative(sha256) if (_root / _relative(sha256)).exists() else None
