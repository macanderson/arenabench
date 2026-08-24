# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 the ArenaBench authors
"""``harbor_agent._ENGINE_ROOT_FIELDS`` against the Rust authority (#3879).

``_ENGINE_ROOT_FIELDS`` is a hand-maintained copy of
``crates/stella-cli/src/settings/unknown.rs::ENGINE_ROOT_FIELDS`` — the
trusted-launcher seam's fail-closed root-key vocabulary for
``STELLA_ENGINE_CONFIG_JSON``. A hand copy drifts in both directions: too
narrow refuses a posture the launcher would actually accept (a live launch
failure), too wide accepts a posture the launcher will refuse (a test that
lies about what would happen at the seam). ``bench/harbor_adapter`` solved
this the same way already — parse the Rust source rather than duplicate it
(``tests/test_posture.py::_engine_root_fields``, #2033) — and this file is
that same reader, pointed at arenabench's own copy.

The authority is now in **another repository**
(https://github.com/macanderson/stella), so this file resolves a Stella
checkout the way the package itself does — :func:`arenabench.sut.stella_repo`,
which honours ``ARENABENCH_STELLA_REPO`` and falls back to the adapter path a
Stella seat already needs — and **skips** when there is none. That is the
honest posture for a cross-repo parity check: it cannot run in a bare CI
checkout, and a vendored copy of the Rust file would be a third hand-copy of
the very thing this test exists to catch drifting. It runs on any machine
that can seat Stella at all, which is every machine that can run the match
this vocabulary governs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from arenabench.harbor_agent import _ENGINE_ROOT_FIELDS
from arenabench.sut import repo_problem, stella_repo

_UNKNOWN_REL = Path("crates") / "stella-cli" / "src" / "settings" / "unknown.rs"


def _unknown_rs() -> Path:
    """The launcher vocabulary's source file, or skip saying why not."""
    repo = stella_repo()
    if repo is None:
        pytest.skip(
            f"no Stella checkout to read {_UNKNOWN_REL} from: "
            f"{repo_problem()} This parity check reads the authority in "
            "https://github.com/macanderson/stella; set "
            "ARENABENCH_STELLA_REPO to a checkout to run it."
        )
    path = repo / _UNKNOWN_REL
    if not path.is_file():
        pytest.skip(
            f"{path} does not exist — the Stella checkout at {repo} predates "
            "or postdates the module this check reads. If the crate moved, "
            "update `_UNKNOWN_REL`."
        )
    return path


def _parse_rust_str_slice(source: str, const_name: str, path: Path) -> frozenset[str]:
    """Every string literal in a ``const NAME: &[&str] = &[ ... ];`` table."""
    match = re.search(rf"const {const_name}: &\[&str\] =\s*&\[(.*?)\];", source, re.DOTALL)
    assert match is not None, (
        f"could not find `{const_name}` in {path} — the constant moved "
        "or was renamed; this file's readers must be repointed"
    )
    return frozenset(re.findall(r'"([^"]+)"', match.group(1)))


def _engine_root_fields() -> frozenset[str]:
    """``ENGINE_ROOT_FIELDS`` out of ``unknown.rs``, unioned with
    ``RETIRED_ENGINE_ROOT``.

    The union, not just ``ENGINE_ROOT_FIELDS`` alone, because the launcher
    seam recognizes both tables — a posture naming a retired-but-still-read
    key (the four non-worker ``pipeline_*_model`` keys #3908 retired) passes
    the seam, and an assertion that only checked the first table would be
    false about the code it names.
    """
    path = _unknown_rs()
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssertionError(
            f"cannot read the launcher vocabulary at {path}: {exc.strerror}. "
            "The Stella checkout was found but this file could not be read."
        ) from exc
    fields = _parse_rust_str_slice(source, "ENGINE_ROOT_FIELDS", path)
    assert fields, "ENGINE_ROOT_FIELDS parsed to zero entries"
    retired = _parse_rust_str_slice(source, "RETIRED_ENGINE_ROOT", path)
    return fields | retired


def test_the_local_copy_does_not_accept_more_than_the_launcher_does() -> None:
    """A posture the local guard would pass but the launcher seam refuses is
    a benchmark run that dies after credentials are already spent.

    ``responsibilities`` is the one deliberate exception: it is not in the
    Rust vocabulary at all (#3879), and stays in ``_ENGINE_ROOT_FIELDS`` as a
    tracked, pre-existing gap (#3871) rather than a silent one — every arena
    posture that declares a responsibility override is refused at launch
    today, and fixing that is a maintainer decision about whether to stop
    emitting the key or give it a home in the Rust engine config, not a
    mechanical sync.
    """
    known_gap = {"responsibilities"}
    assert _ENGINE_ROOT_FIELDS - known_gap <= _engine_root_fields(), (
        "harbor_agent._ENGINE_ROOT_FIELDS accepts a root key the trusted "
        "launcher seam would refuse — re-sync it against "
        "crates/stella-cli/src/settings/unknown.rs::ENGINE_ROOT_FIELDS / "
        "RETIRED_ENGINE_ROOT"
    )


def test_the_local_copy_is_not_missing_a_key_the_launcher_accepts() -> None:
    """The other direction of drift: a key the launcher would accept but the
    local guard refuses first is a capability gap, not a broken run — still
    worth catching, so a future posture builder does not waste time chasing
    a refusal that `_ENGINE_ROOT_FIELDS` invented.
    """
    missing = _engine_root_fields() - _ENGINE_ROOT_FIELDS
    assert not missing, (
        f"the trusted launcher accepts root keys _ENGINE_ROOT_FIELDS does "
        f"not: {sorted(missing)} — add them so a posture that wants one is "
        "not refused by this file before it ever reaches the seam"
    )
