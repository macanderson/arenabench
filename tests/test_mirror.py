# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 the ArenaBench authors
"""The mirror: durable-copy SQL emission, idempotency, and provenance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from arenabench import experiments, mirror

STAMP = "2026-08-23T00:00:00Z"


def _stored(tmp_path: Path, *documents: dict) -> Path:
    db = tmp_path / "experiments.db"
    for document in documents:
        experiments.store_results(document, db)
    return db


def _emit(tmp_path: Path, *documents: dict) -> str:
    db = _stored(tmp_path, *documents)
    return mirror.mirror_sql(mirror.mirror_rows(db), "Mac@local", STAMP)


def _apply(target: sqlite3.Connection, sql: str) -> None:
    target.executescript(sql)


def test_emitted_sql_executes_and_lands_every_row(tmp_path: Path) -> None:
    document = {
        "schema": "arenabench-experiment-document/1",
        "calculation_version": "calc/1",
        "experiment": {"id": "exp-001", "title": "A vs B", "status": "open"},
        "trials": [{"task": "t1"}],
    }
    sql = _emit(tmp_path, document)
    target = sqlite3.connect(":memory:")
    try:
        _apply(target, sql)
        (row,) = target.execute(
            "SELECT experiment_id, title, status, doc_schema, "
            "calculation_version, migrated, migration_source, mirrored_at, "
            "results FROM experiment_results"
        )
    finally:
        target.close()
    assert row[:8] == (
        "exp-001",
        "A vs B",
        "open",
        "arenabench-experiment-document/1",
        "calc/1",
        1,
        "Mac@local",
        STAMP,
    )
    assert json.loads(row[8]) == document


def test_mirroring_twice_does_not_duplicate(tmp_path: Path) -> None:
    sql = _emit(tmp_path, {"experiment": {"id": "exp-001"}})
    target = sqlite3.connect(":memory:")
    try:
        _apply(target, sql)
        _apply(target, sql)
        (count,) = target.execute("SELECT COUNT(*) FROM experiment_results").fetchone()
    finally:
        target.close()
    assert count == 1


def test_duplicate_local_documents_fold_into_one_durable_row(tmp_path: Path) -> None:
    """Two local rows holding one document emit one insert, not two.

    The durable key is the content hash, so the database would absorb the
    second insert with ``DO NOTHING`` either way — but the mirror verifies
    by counting the keys it sent against the keys that landed, and an
    emitted duplicate makes that count come up short and rejects a mirror
    that in fact succeeded.
    """
    document = {"experiment": {"id": "exp-001"}, "calculation_version": "calc/1"}
    db = _stored(tmp_path, document, document)
    rows = mirror.mirror_rows(db)
    assert len(rows) == 1
    assert rows[0]["local_ids"] == [1, 2]

    sql = mirror.mirror_sql(rows, "Mac@local", STAMP)
    assert sql.count("INSERT INTO experiment_results") == 1

    record = mirror.manifest(rows, "Mac@local", STAMP)
    assert record["hashes"] == [rows[0]["doc_sha256"]]
    assert record["local_ids"] == [1, 2]

    target = sqlite3.connect(":memory:")
    try:
        _apply(target, sql)
        (landed,) = target.execute(
            "SELECT COUNT(*) FROM experiment_results WHERE doc_sha256 = ?",
            (rows[0]["doc_sha256"],),
        ).fetchone()
    finally:
        target.close()
    assert landed == len(record["hashes"])


def test_manifest_names_the_source_and_stamp_the_script_carries(
    tmp_path: Path,
) -> None:
    """The mark step reads its label from the manifest, never a second flag.

    A ``--source`` typed once at emit and again at mark is a label the two
    steps can disagree about; the durable copy would then say one machine
    and the working set another.
    """
    db = _stored(tmp_path, {"experiment": {"id": "exp-001"}})
    rows = mirror.mirror_rows(db)
    record = mirror.manifest(rows, "Mac@local", STAMP)
    assert record["source"] == "Mac@local"
    assert record["mirrored_at"] == STAMP


def test_scalar_headers_reach_the_durable_row_as_json_text(tmp_path: Path) -> None:
    """A numeric id or boolean status is TEXT in the durable table.

    An experiment document is arbitrary JSON, so these are documents the
    store legitimately holds; rendering them as canonical JSON text is
    what lets the TEXT columns carry them without guessing.
    """
    document = {"experiment": {"id": 7, "status": True}, "calculation_version": 1.5}
    sql = _emit(tmp_path, document)
    target = sqlite3.connect(":memory:")
    try:
        _apply(target, sql)
        (row,) = target.execute(
            "SELECT experiment_id, status, calculation_version FROM experiment_results"
        )
    finally:
        target.close()
    assert row == ("7", "true", "1.5")


def test_a_container_header_is_refused_by_name(tmp_path: Path) -> None:
    """A list-valued title names the field and the row, not ``str.replace``."""
    db = _stored(tmp_path, {"experiment": {"id": "exp-001", "title": ["a", "b"]}})
    with pytest.raises(mirror.MirrorError, match="title"):
        mirror.mirror_rows(db)


def test_row_key_is_the_canonical_document_hash(tmp_path: Path) -> None:
    document = {"experiment": {"id": "exp-001"}, "b": 2, "a": 1}
    (row,) = mirror.mirror_rows(_stored(tmp_path, document))
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True)
    assert row["doc_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()


def test_quoting_survives_documents_containing_quotes(tmp_path: Path) -> None:
    document = {"experiment": {"id": "exp-001", "title": "it's; DROP TABLE x"}}
    sql = _emit(tmp_path, document)
    target = sqlite3.connect(":memory:")
    try:
        _apply(target, sql)
        (title,) = target.execute("SELECT title FROM experiment_results").fetchone()
    finally:
        target.close()
    assert title == "it's; DROP TABLE x"


def test_local_created_at_travels_to_the_durable_row(tmp_path: Path) -> None:
    sql = _emit(tmp_path, {"experiment": {"id": "exp-001"}})
    target = sqlite3.connect(":memory:")
    try:
        _apply(target, sql)
        (created_at,) = target.execute(
            "SELECT created_at FROM experiment_results"
        ).fetchone()
    finally:
        target.close()
    assert created_at is not None
