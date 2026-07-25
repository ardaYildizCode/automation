"""Batch state, persisted as JSON and committed back to the repo.

GitHub Actions runners are ephemeral, so the pipeline's memory lives in
`state/batches.json`. Committing it (rather than using an external DB) keeps
the whole system dependency-free and gives a full audit trail of every batch
in git history.

Writes are atomic (temp file + rename) so an interrupted run cannot leave a
truncated state file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import REPO_ROOT

STATE_PATH = Path(os.environ.get("STATE_PATH") or REPO_ROOT / "state" / "batches.json")
SCHEMA_VERSION = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class BatchStatus:
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    MEASURED = "measured"
    GRADUATED = "graduated"
    PROMOTED = "promoted"
    FAILED = "failed"


@dataclass
class Variant:
    key: str
    label: str
    recipe: str = ""
    render_name: str = ""
    duration: float = 0.0
    ig_media_id: str = ""
    ig_container_id: str = ""
    permalink: str = ""
    status: str = "pending"
    error: str = ""
    insights: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    rank: int = 0

    @property
    def published(self) -> bool:
        return self.status == "published" and bool(self.ig_media_id)


@dataclass
class Batch:
    id: str
    source_path: str
    source_name: str
    source_hash: str
    product: str
    created_at: str
    status: str = BatchStatus.PUBLISHING
    variants: list[Variant] = field(default_factory=list)
    published_at: str = ""
    measured_at: str = ""
    graduated_at: str = ""
    winner_key: str = ""
    notes: list[str] = field(default_factory=list)
    ad: dict[str, Any] = field(default_factory=dict)

    def variant(self, key: str) -> Variant | None:
        return next((v for v in self.variants if v.key == key), None)

    @property
    def winner(self) -> Variant | None:
        return self.variant(self.winner_key) if self.winner_key else None

    @property
    def published_variants(self) -> list[Variant]:
        return [v for v in self.variants if v.published]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Batch":
        variants = [Variant(**v) for v in data.pop("variants", [])]
        known = {f for f in cls.__dataclass_fields__ if f != "variants"}
        return cls(variants=variants, **{k: v for k, v in data.items() if k in known})


class Store:
    """Load/mutate/save the batch ledger."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self.batches: list[Batch] = []
        self.seen_hashes: set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        self.batches = [Batch.from_dict(b) for b in raw.get("batches", [])]
        self.seen_hashes = set(raw.get("seen_hashes", []))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utcnow(),
            "seen_hashes": sorted(self.seen_hashes),
            "batches": [b.to_dict() for b in self.batches],
        }
        blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        # Atomic replace: a killed runner must not leave half a file.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(blob)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- queries --------------------------------------------------------

    def get(self, batch_id: str) -> Batch | None:
        return next((b for b in self.batches if b.id == batch_id), None)

    def by_status(self, *statuses: str) -> Iterator[Batch]:
        wanted = set(statuses)
        return (b for b in self.batches if b.status in wanted)

    def latest(self, *statuses: str) -> Batch | None:
        candidates = sorted(
            self.by_status(*statuses) if statuses else self.batches,
            key=lambda b: b.created_at,
        )
        return candidates[-1] if candidates else None

    def already_seen(self, source_hash: str) -> bool:
        return source_hash in self.seen_hashes

    def batches_created_since(self, since: datetime) -> list[Batch]:
        return [b for b in self.batches if parse_ts(b.created_at) >= since]

    # -- mutations ------------------------------------------------------

    def add(self, batch: Batch) -> None:
        self.batches.append(batch)
        self.seen_hashes.add(batch.source_hash)

    def mark_seen(self, source_hash: str) -> None:
        self.seen_hashes.add(source_hash)
