"""Write `vault-manifest.json` — discovery hook for a future aggregator.

Per RFC 003 Fork 02, the v0 vault is per-repo. A hypothetical future
`~/.nightly/global-vault/` aggregator would discover vaults by scanning
known locations; the manifest gives it enough metadata to do that without
re-parsing the corpus.

Schema is intentionally minimal — name, version, counts. Cross-repo
aggregation is its own RFC; this file is just a forward-compatible hook.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .model import NODE_KINDS, Node

__all__ = ["MANIFEST_SCHEMA_VERSION", "write_manifest"]


MANIFEST_SCHEMA_VERSION = 1


def write_manifest(
    vault_root: Path,
    *,
    nodes: Iterable[Node],
    run_count: int,
) -> Path:
    """Write `vault-manifest.json` under `vault_root`. Overwrites unconditionally.

    `nodes` is iterable so callers can stream — the manifest only reads
    `node.kind` for the kind-tally, nothing else.
    """
    counts: Counter[str] = Counter()
    for node in nodes:
        counts[node.kind] += 1

    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "vault_path": str(vault_root),
        "last_built": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_count": run_count,
        "node_count_by_kind": {kind: counts.get(kind, 0) for kind in NODE_KINDS},
    }

    vault_root.mkdir(parents=True, exist_ok=True)
    target = vault_root / "vault-manifest.json"
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target
