#!/usr/bin/env bash
#
# Guard: the agent-config role names have one spelling, across every producer
# in this repository.
#
# This is the ArenaBench half of a guard that split at ejection. Before the
# split, the stella monorepo's scripts/check-role-names.sh held four languages
# to one spelling, reading the truth from the Rust settings surface
# (`ENGINE_AGENT_NAMES` + `RETIRED_ENGINE_AGENT_NAMES` in
# crates/stella-cli/src/settings/unknown.rs) and checking arenabench's Python
# producers against it. The stella half still exists there and now covers the
# producers that stayed (bench/harbor_adapter, the Observatory filter, the TUI
# enum): https://github.com/macanderson/stella, scripts/check-role-names.sh.
#
# This half checks the producers that left with ArenaBench. The normative home
# here is the ROLES tuple in arenabench/model.py — the canonical spelling this
# package writes into a seat's `pipeline_<role>_model` keys. It must stay in
# lockstep with the union the stella guard reads from its Rust home; a rename
# on either side is a cross-repository change, and each half fails on its own
# producers rather than pretending the other repository does not exist.
#
# The failure this exists to catch (stella#1394): a role rename that every
# compiler and test misses, so a seat runs with the renamed role silently
# inheriting the worker baseline while the scoreboard reports it as
# configured — a wrong measurement, not a crash.
#
# `judge` is a deliberately retired spelling. It survives only where it is
# read and translated (`_ROLE_ALIASES` in arenabench's config loader, which
# upgrades an old match file on the way in). That is compatibility, not drift,
# so it is named below rather than the guard pretending it does not exist.
#
# Uses portable POSIX tools so it runs on a bare CI runner.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

fail=0

# The verdict is decided before anything is written. Failure lines are
# buffered while the checks run and emitted in one final write: a guard that
# prints as it scans dies mid-report when its reader exits early, and under
# `set -euo pipefail` whatever partial state it had reached becomes the exit
# status.
report=""
note() { report="${report}check-role-names: $1"$'\n'; }

# Emission is best-effort: the verdict is already decided, so a reader that
# closed the pipe (`| head -1`, `| true`) must be able to change neither the
# report nor the exit code. SIGPIPE is ignored so a failed write surfaces as a
# discarded error instead of killing the script.
emit() {
  trap '' PIPE
  printf '%s' "$report" >&2 || true
}

# Retired spellings. A producer may mention one of these only as an alias —
# never as a member of its own role set.
retired_spellings="judge"

# ── The truth ────────────────────────────────────────────────────────────────

roles_home="arenabench/model.py"

if [ ! -f "$roles_home" ]; then
  note "FAIL — $roles_home does not exist; its ROLES tuple is the normative home."
  emit
  exit 1
fi

# The canonical tuple:
#   ROLES: tuple[str, ...] = ("default", "worker", ...)
roles="$(
  grep -E '^ROLES: *tuple' "$roles_home" |
    tr ',' '\n' | sed -n 's/.*["'"'"']\([a-z_][a-z_]*\)["'"'"'].*/\1/p' | LC_ALL=C sort -u
)"

if [ -z "$roles" ]; then
  note "FAIL — could not read any role from the ROLES tuple in $roles_home."
  note "     If the tuple moved or changed shape, repoint this guard; do not"
  note "     delete it. It is the only thing holding this package's producers"
  note "     to one spelling — and to the stella repo's half of this guard."
  emit
  exit 1
fi

roles_flat="$(printf '%s\n' "$roles" | tr '\n' ' ')"

is_role() {
  case " $roles_flat " in
  *" $1 "*) return 0 ;;
  *) return 1 ;;
  esac
}

# Extract every double- or single-quoted lowercase token from stdin, one per
# line, sorted and deduplicated. Used to read a literal list out of Python
# without teaching this script the language.
quoted_tokens() {
  tr ',' '\n' | sed -n 's/.*["'"'"']\([a-z_][a-z_]*\)["'"'"'].*/\1/p' | LC_ALL=C sort -u
}

# Compare a producer's role set against the canonical one.
#   $1 producer path (for the message), $2 what it is, $3 the extracted set
expect_exact_set() {
  path="$1"
  what="$2"
  found="$3"

  if [ -z "$found" ]; then
    note "FAIL — $path: could not extract $what."
    note "     The producer moved or changed shape. Repoint the extraction in"
    note "     this guard rather than dropping the producer from it."
    fail=1
    return
  fi

  missing="$(comm -23 <(printf '%s\n' "$roles") <(printf '%s\n' "$found") | tr '\n' ' ')"
  extra="$(comm -13 <(printf '%s\n' "$roles") <(printf '%s\n' "$found") | tr '\n' ' ')"

  if [ -n "${missing// /}" ]; then
    note "FAIL — $path ($what) is missing role(s): $missing"
    fail=1
  fi
  if [ -n "${extra// /}" ]; then
    note "FAIL — $path ($what) has role(s) the canonical tuple does not know: $extra"
    fail=1
  fi
}

# Every role named in a `pipeline_<role>_model` key anywhere in a file must be
# a real role. A subset check, not an exact one: `default` has no flat key by
# design (`default_model` is its key), so the set is legitimately smaller.
#
# Redirected rather than piped: a `while read` on the right of a pipe runs in
# a subshell, where `fail=1` and the buffered note would be set and then
# thrown away.
expect_flat_keys_known() {
  path="$1"
  [ -f "$path" ] || return 0
  while IFS= read -r r; do
    [ -n "$r" ] || continue
    # Not a role name — these are the pipeline's own numeric settings.
    case "$r" in
    max_revisions | candidates) continue ;;
    esac
    if ! is_role "$r"; then
      note "FAIL — $path writes pipeline_${r}_model, and '$r' is not a role."
      fail=1
    fi
  done < <(sed -n 's/.*pipeline_\([a-z_][a-z_]*\)_model.*/\1/p' "$path" | LC_ALL=C sort -u)
}

# ── The producers ────────────────────────────────────────────────────────────
#
# Each entry states where the set lives and how it is spelled. Finding these
# again when one moves is half the work this guard does; a producer that
# vanishes should be deleted from here in the same PR, never silently skipped.

# 1. The per-role loop, which drives what a seat actually writes. The
#    canonical tuple in arenabench/model.py is the truth read above, so the
#    loop is checked against it rather than beside it.
p="arenabench/harbor_agent.py"
if [ -f "$p" ]; then
  expect_exact_set "$p" "per-role loop" \
    "$(grep -E 'for role in \(' "$p" | quoted_tokens)"
  expect_flat_keys_known "$p"
else
  note "FAIL — $p is gone; update this guard's producer list."
  fail=1
fi

# 2. Every other flat `pipeline_<role>_model` writer in the package. Cheap to
#    check and exactly the shape that drifts: a stale spelling here writes a
#    key the engine ignores, and the seat silently inherits the worker
#    baseline.
expect_flat_keys_known "arenabench/model.py"
expect_flat_keys_known "arenabench/config.py"

# ── Retired spellings ────────────────────────────────────────────────────────
#
# A retired name may appear only where it is being *translated*. Anywhere else
# it is the drift this guard exists to catch.

for old in $retired_spellings; do
  if is_role "$old"; then
    note "FAIL — '$old' is a retired spelling and must not appear in the"
    note "     ROLES tuple in $roles_home. It survives only as an alias in"
    note "     the config loader, which translates it on the way in."
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  note ""
  note "The role names live in $roles_home (ROLES), in lockstep with the"
  note "stella repo's half of this guard. Renaming one is a cross-language,"
  note "cross-repository change: no compiler will find the Python literals,"
  note "and neither will your tests. Update every producer named above in the"
  note "same PR, and the stella side in its own."
  emit
  exit 1
fi

emit
printf 'check-role-names: OK — %d role(s) [%s] consistent across every producer.\n' \
  "$(printf '%s\n' "$roles" | wc -l | tr -d ' ')" "$(printf '%s' "$roles_flat" | sed 's/ $//')" || true
