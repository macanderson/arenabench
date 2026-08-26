# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 the ArenaBench authors
"""Mirror the local experiments store into the durable benchmark database.

The Stella repository's ``bench/telemetry_store/schema.sql`` names the
arrangement this module implements: the local SQLite copy is the working
set and the Postgres copy is the durable one. This module turns the working
set into SQL the durable side can apply — it deliberately emits SQL text
instead of holding a database connection, because the durable copy is
reached over SSM (no open port, no credentials on the workstation) and the
only thing that channel carries is text.

The emitted SQL is idempotent end to end. The schema is all
``IF NOT EXISTS``; every row's primary key is the SHA-256 of its canonical
document bytes, inserted with ``ON CONFLICT DO NOTHING``, so re-running a
mirror — or two machines mirroring the same document — converges instead
of duplicating. The DDL and the inserts are valid on both PostgreSQL and
SQLite, which is what lets the tests here prove the SQL by executing it
rather than by matching strings.

Because the durable key is the content hash, identical documents are one
durable row however many local rows hold them. :func:`mirror_rows` folds
them at emit time rather than leaving the duplication for the database to
absorb, so the number of statements emitted is the number of rows the
durable copy should end up with — which is what makes the shell script's
verification a like-for-like count instead of one that rejects a
successful mirror.

Every mirrored row carries ``migrated = 1`` and a ``migration_source`` of
the form ``machine@tier`` (``Mac@local``), so the durable copy names the
working set each row came from. :func:`manifest` records which local rows
each durable row covers, and the local rows are stamped with the same pair
by :func:`arenabench.experiments.mark_migrated` — from that manifest, and
only after the durable side has verifiably accepted them.

Usage::

    python3 -m arenabench.mirror emit --source Mac@local \\
        --out mirror.sql --manifest mirror.manifest.json
    python3 -m arenabench.mirror mark --manifest mirror.manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .experiments import mark_migrated, stored_rows

__all__ = [
    "PRODUCTION_SCHEMA",
    "MirrorError",
    "manifest",
    "mirror_rows",
    "mirror_sql",
]

#: The durable copy of ``experiment_results``. Same philosophy as the
#: local store — the document is the record, the columns beside it are
#: identity and provenance — with two differences the durable tier forces:
#: the key is the content hash (machine-independent, so any number of
#: working sets can mirror into one table), and the listing headers
#: (title, status, schema, calculation version) are extracted so the
#: durable side can answer a gallery query without parsing documents.
#: ``doc_schema`` rather than ``schema`` because the bare word collides
#: with the SQL namespace concept on PostgreSQL. ``results`` is TEXT, not
#: JSONB, and that is the point: PostgreSQL's jsonb normalizes key order
#: and whitespace, so a JSONB column could never re-derive ``doc_sha256``
#: from what it stores. TEXT keeps the stored bytes the bytes the hash
#: names; a reader that wants json operators casts at query time.
PRODUCTION_SCHEMA = """\
CREATE TABLE IF NOT EXISTS experiment_results (
    doc_sha256          TEXT PRIMARY KEY,
    experiment_id       TEXT,
    title               TEXT,
    status              TEXT,
    doc_schema          TEXT,
    calculation_version TEXT,
    created_at          TEXT,
    migrated            INTEGER NOT NULL DEFAULT 0,
    migration_source    TEXT,
    mirrored_at         TEXT NOT NULL,
    results             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS experiment_results_experiment_idx
    ON experiment_results(experiment_id);
"""

#: The durable header columns, in the order :func:`mirror_sql` emits them,
#: paired with the document path each is read from. Every one is TEXT, so
#: every one goes through :func:`_header_text` before it reaches SQL.
_HEADER_FIELDS = (
    "experiment_id",
    "title",
    "status",
    "doc_schema",
    "calculation_version",
)


class MirrorError(ValueError):
    """A local document the durable table cannot hold as emitted.

    Raised rather than letting a malformed value reach the SQL builder,
    where the failure would arrive as an :class:`AttributeError` naming
    ``str.replace`` and nothing about which document is wrong.
    """


def _canonical(document: Mapping[str, Any]) -> str:
    """The document's canonical bytes: sorted keys, no ASCII escaping.

    The same serialization :func:`arenabench.experiments.store_results`
    writes, so the hash of a mirrored row equals the hash of the local row
    it came from.
    """
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def _header_text(value: Any, field: str, row_id: int) -> str | None:
    """One durable header column's value as TEXT, or ``None``.

    An experiment document is arbitrary JSON, so a header a caller filled
    in with a number, a boolean, or a list is a document this store will
    happily hold — but the durable columns are TEXT, and a list has no
    honest TEXT rendering that a reader could distinguish from a title
    someone typed. Scalars are rendered as their canonical JSON text
    (``7``, ``true``); containers are refused by name, with the row that
    carries them, so the fix is a document edit rather than a stack trace.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return json.dumps(value)
    raise MirrorError(
        f"local row {row_id}: {field} must be a JSON scalar for the durable "
        f"header, got {type(value).__name__}"
    )


def mirror_rows(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Every distinct local experiment document, shaped for the durable table.

    One dict per *durable* row: the content hash, the extracted listing
    headers, the earliest local ``created_at``, the canonical text, and
    ``local_ids`` — every local row whose document hashes to that key.
    Documents are folded by hash because the durable key is the hash: two
    local rows holding byte-identical documents are one durable row, and
    emitting two inserts for them would make the mirror's own verification
    count fewer landed rows than statements sent and refuse a mirror that
    in fact succeeded.

    The first occurrence wins for every column, so the durable row carries
    the earliest time the working set saw that document rather than the
    time of whichever copy happened to be scanned last.

    Reading through :func:`~arenabench.experiments.stored_rows` keeps this
    the only module that knows the durable shape while the experiments
    module stays the only one that knows the local one.
    """
    rows: list[dict[str, Any]] = []
    by_hash: dict[str, dict[str, Any]] = {}
    for stored in stored_rows(db_path):
        document = stored["document"]
        text = _canonical(document)
        digest = hashlib.sha256(text.encode()).hexdigest()
        already = by_hash.get(digest)
        if already is not None:
            already["local_ids"].append(stored["id"])
            continue
        experiment = document.get("experiment")
        header = experiment if isinstance(experiment, Mapping) else {}
        row_id = stored["id"]
        row = {
            "doc_sha256": digest,
            "experiment_id": _header_text(header.get("id"), "experiment_id", row_id),
            "title": _header_text(header.get("title"), "title", row_id),
            "status": _header_text(header.get("status"), "status", row_id),
            "doc_schema": _header_text(document.get("schema"), "doc_schema", row_id),
            "calculation_version": _header_text(
                document.get("calculation_version"), "calculation_version", row_id
            ),
            "created_at": stored["created_at"],
            "results": text,
            "local_ids": [row_id],
        }
        by_hash[digest] = row
        rows.append(row)
    return rows


def _literal(value: str | None) -> str:
    """``value`` as a SQL string literal, ``NULL`` when absent.

    Single quotes are doubled and nothing else is escaped — the portable
    quoting shared by PostgreSQL (with ``standard_conforming_strings``,
    the default since 9.1) and SQLite. Deliberately strict about its
    argument: every caller has already put the value through
    :func:`_header_text` or built it from a canonical serialization, so a
    non-string here means a row was hand-assembled against the contract.
    """
    if value is None:
        return "NULL"
    if not isinstance(value, str):
        raise MirrorError(
            f"SQL literals are text or NULL, got {type(value).__name__}: {value!r}"
        )
    return "'" + value.replace("'", "''") + "'"


def mirror_sql(
    rows: Sequence[Mapping[str, Any]],
    source: str,
    mirrored_at: str,
) -> str:
    """One idempotent script: schema, then every row as a keyed insert.

    ``source`` is the ``machine@tier`` label stamped into
    ``migration_source``; ``mirrored_at`` is passed in rather than read
    from the clock so the same rows always produce the same script.
    """
    statements = [PRODUCTION_SCHEMA]
    for row in rows:
        values = ", ".join(
            (
                _literal(row["doc_sha256"]),
                *(_literal(row[field]) for field in _HEADER_FIELDS),
                _literal(row.get("created_at")),
                "1",
                _literal(source),
                _literal(mirrored_at),
                _literal(row["results"]),
            )
        )
        statements.append(
            "INSERT INTO experiment_results "
            "(doc_sha256, experiment_id, title, status, doc_schema, "
            "calculation_version, created_at, migrated, migration_source, "
            "mirrored_at, results)\n"
            f"VALUES ({values})\n"
            "ON CONFLICT (doc_sha256) DO NOTHING;"
        )
    return "\n".join(statements) + "\n"


def manifest(
    rows: Sequence[Mapping[str, Any]],
    source: str,
    mirrored_at: str,
) -> dict[str, Any]:
    """What the emit step sent, for the verify and mark steps to read.

    ``hashes`` is the set of durable keys the script inserts — distinct by
    construction, so counting them against the durable table is a
    like-for-like comparison. ``local_ids`` is the set of local rows those
    keys account for, and is the only thing
    :func:`~arenabench.experiments.mark_migrated` is allowed to stamp: a
    row stored after this manifest was written was never sent and must not
    be credited as durable. ``source`` travels here too, so the mark step
    cannot label rows differently from the script that carried them.
    """
    return {
        "source": source,
        "mirrored_at": mirrored_at,
        "hashes": [row["doc_sha256"] for row in rows],
        "local_ids": [row_id for row in rows for row_id in row["local_ids"]],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    emit = sub.add_parser("emit", help="write the mirror SQL for every local row")
    emit.add_argument("--db", type=Path, default=None, help="local experiments.db")
    emit.add_argument("--source", required=True, help="machine@tier provenance label")
    emit.add_argument("--out", type=Path, required=True, help="where to write the SQL")
    emit.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="where to write the record of what was emitted",
    )

    mark = sub.add_parser("mark", help="stamp the emitted local rows as migrated")
    mark.add_argument("--db", type=Path, default=None, help="local experiments.db")
    mark.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="the manifest written by emit, naming the rows that were sent",
    )

    args = parser.parse_args(argv)
    if args.command == "emit":
        rows = mirror_rows(args.db)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        args.out.write_text(mirror_sql(rows, args.source, stamp))
        record = manifest(rows, args.source, stamp)
        args.manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(
            f"{len(rows)} rows ({len(record['local_ids'])} local) -> {args.out}"
        )
        return 0
    record = json.loads(args.manifest.read_text())
    count = mark_migrated(record["source"], record["local_ids"], args.db)
    print(f"{count} rows marked migrated ({record['source']})")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI
    sys.exit(main())
