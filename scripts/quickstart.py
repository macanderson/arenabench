#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 the ArenaBench authors
"""Two opinionated match starters: ``h2h`` and ``frontierbench``.

Both seat the same pairing — **Stella as arm 1, Claude Code as arm 2**, both
on ``claude-sonnet-5`` at ``effort = low`` — because that is the comparison
this project exists to make, and because holding the worker model and the
effort constant across both arms is what makes the agent architecture the
variable under test rather than one more confound.

They differ in how the task list is chosen, which is the only interesting
decision left:

``h2h``            a reproducible sample of the whole dataset. The honest
                   default: every difficulty in the proportion the benchmark
                   actually ships.
``frontierbench``  a reproducible sample of the **easy and medium** tasks
                   only, unless ``--include-hard`` says otherwise. Faster and
                   cheaper to iterate on, and — stated everywhere it can be —
                   a solve rate over a smaller, easier population, which is
                   not a solve rate over the benchmark.

Credentials never enter a match file. Both arms declare only the NAME of the
variable they need (``ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``), and
``--submit`` stages the values into SSM SecureStrings under ``/arenabench/``,
where the Batch runner's entrypoint exports them into the trial container.
That is the same path every other cloud match uses; nothing here invents a
second one.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arenabench.registry import DEFAULT_REGISTRY, Task, sample_tasks

#: The worker both arms run. One model, one effort, both seats — see module
#: docstring. `low` is the user-facing choice these starters are pinned to;
#: `arenabench.model.EFFORTS` is the full vocabulary.
WORKER_MODEL = "claude-sonnet-5"
WORKER_EFFORT = "low"
DATASET = "terminal-bench-2.1"

#: SSM parameter names, matching the ones already provisioned under
#: `/arenabench/`. The runner's entrypoint upper-cases every name it exports,
#: so `claude_code_oauth_token` arrives in the container as
#: `CLAUDE_CODE_OAUTH_TOKEN`. Writing to the EXISTING spellings rather than
#: inventing new ones keeps one parameter per credential instead of two that
#: can disagree.
SSM_ANTHROPIC = "/arenabench/ANTHROPIC_API_KEY"
SSM_OAUTH = "/arenabench/claude_code_oauth_token"

EASY_MEDIUM = ("easy", "medium")


# ── Credentials ──────────────────────────────────────────────────────────────


def read_secret(
    literal: str | None, path: str | None, env_name: str, label: str
) -> str | None:
    """A secret from ``--x``, ``--x-file`` or the environment, in that order.

    Returns ``None`` when none of the three is set — the caller decides
    whether that is fatal, because a local run needs the value in its own
    environment while a cloud run needs it in SSM.
    """
    if literal:
        # argv is world-readable on a shared box (`ps -ef`) and lands in shell
        # history. Said once, here, rather than in three help strings.
        print(
            f"note: {label} was passed on the command line, which `ps` shows to "
            f"every user on this machine and your shell writes to history. "
            f"--{label.lower().replace('_', '-')}-file or the {env_name} "
            f"environment variable avoid both.",
            file=sys.stderr,
        )
        return literal.strip()
    if path:
        text = Path(path).expanduser().read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit(f"error: {path} is empty")
        return text
    from_env = os.environ.get(env_name, "").strip()
    return from_env or None


def stage_ssm(name: str, value: str, region: str) -> None:
    """Put one SecureString, printing what it did and never what it wrote.

    The value reaches the AWS CLI through ``--value file://…`` and a mode-0600
    temporary file, never through argv. Passing it directly would put the
    secret in the `ps` output of every user on the machine — the exact hazard
    :func:`read_secret` warns about, and it would be incoherent to warn about
    it and then do it here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        holder = Path(tmp) / "value"
        holder.touch(mode=0o600)
        holder.write_text(value, encoding="utf-8")
        subprocess.run(
            [
                "aws", "ssm", "put-parameter",
                "--region", region,
                "--name", name,
                "--type", "SecureString",
                "--value", f"file://{holder}",
                "--overwrite",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    print(f"  staged {name}  ({len(value)} chars)")


# ── Task selection ───────────────────────────────────────────────────────────


def pick_tasks(count: int, seed: int, easy_medium_only: bool) -> tuple[list[Task], int]:
    """``count`` tasks and the seed that drew them.

    An unseeded draw still gets a seed and reports it, because the one thing
    needed to re-run a slice is the one thing an unseeded draw throws away.
    """
    tasks = DEFAULT_REGISTRY.tasks(DATASET)
    if not tasks:
        raise SystemExit(
            f"error: no tasks for {DATASET} on this disk.\n"
            f"       materialise it first:  arenabench export {DATASET}"
        )
    pool = tasks
    if easy_medium_only:
        pool = [t for t in tasks if t.difficulty in EASY_MEDIUM]
        if not pool:
            raise SystemExit(
                f"error: no easy/medium tasks in {DATASET} — the dataset "
                "carries no difficulty metadata, so this filter cannot be "
                "honoured. Re-run with --include-hard."
            )
    if count >= len(pool):
        return list(pool), seed
    return sample_tasks(pool, count, seed), seed


def difficulty_breakdown(tasks: list[Task]) -> str:
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.difficulty or "unlabelled"] = (
            counts.get(task.difficulty or "unlabelled", 0) + 1
        )
    return ", ".join(f"{n} {d}" for d, n in sorted(counts.items()))


# ── The match file ───────────────────────────────────────────────────────────


def render_toml(
    *,
    name: str,
    tasks: list[Task],
    attempts: int,
    concurrency: int,
    seed: int,
    pool_note: str,
) -> str:
    task_lines = "\n".join(f'  "{t.name}",' for t in tasks)
    return f"""# Generated by scripts/quickstart.py — edit freely, it is a normal match file.
#
# {name}
#
# BOTH ARMS RUN {WORKER_MODEL} AT effort = {WORKER_EFFORT}. That is the point:
# the worker model and the effort are held constant so the thing being
# compared is the agent architecture around them, not one seat's budget. A
# change to either belongs on both arms or on neither.
#
# TASK POOL
#
# {pool_note}
# Drawn with seed {seed} — pass `--seed {seed}` to reproduce this exact slice.
# Difficulty of the drawn set: {difficulty_breakdown(tasks)}.
#
# CREDENTIALS
#
# Names only, never values. Locally these come from the environment; in the
# cloud the Batch entrypoint exports them from SSM `/arenabench/*`.
[match]
name = "{name}"
dataset = "{DATASET}"
tasks = [
{task_lines}
]
attempts = {attempts}
concurrency = {concurrency}

# ── Arm 1 ────────────────────────────────────────────────────────────────────
[[contestant]]
id = "stella"
name = "Stella (Sonnet 5, low)"
agent = "stella"
color = "#EFC53F"

  [contestant.engine]
  api = "anthropic"
  model = "{WORKER_MODEL}"
  reasoning = true
  effort = "{WORKER_EFFORT}"

  [contestant.env]
  required = ["ANTHROPIC_API_KEY"]

# ── Arm 2 ────────────────────────────────────────────────────────────────────
[[contestant]]
id = "claude-code"
name = "Claude Code (Sonnet 5, low)"
agent = "claude-code"
color = "#FF6B9D"

  [contestant.engine]
  api = "anthropic"
  model = "{WORKER_MODEL}"
  reasoning = true
  effort = "{WORKER_EFFORT}"
  # No base_url: this seat runs at Anthropic on the subscription token rather
  # than a metered key.

  [contestant.env]
  required = ["CLAUDE_CODE_OAUTH_TOKEN"]
"""


# ── Driver ───────────────────────────────────────────────────────────────────


def run(args: argparse.Namespace, *, easy_medium_only: bool, label: str) -> int:
    seed = args.seed if args.seed is not None else random.randrange(1, 2**31)
    tasks, seed = pick_tasks(args.tasks, seed, easy_medium_only)

    if easy_medium_only:
        pool_note = (
            "EASY AND MEDIUM ONLY — hard tasks were excluded by default.\n"
            "# This is a smaller and easier population than the benchmark, so a\n"
            "# solve rate from this run is NOT comparable to a full-panel number\n"
            "# and must not be quoted as one. `--include-hard` widens it."
        )
    else:
        pool_note = (
            "The whole dataset, every difficulty, in the proportion it ships."
        )

    name = args.name or f"{label}: {len(tasks)} tasks, sonnet 5 low, stella vs claude code"
    toml = render_toml(
        name=name,
        tasks=tasks,
        attempts=args.attempts,
        concurrency=args.concurrency,
        seed=seed,
        pool_note=pool_note,
    )

    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(toml, encoding="utf-8")

    print(f"match     : {name}")
    print(f"tasks     : {len(tasks)}  ({difficulty_breakdown(tasks)})")
    print(f"seed      : {seed}   (--seed {seed} reproduces this slice)")
    print(f"trials    : {len(tasks) * args.attempts * 2}"
          f"  [{args.attempts} attempt(s) x {len(tasks)} task(s) x 2 seats]")
    print(f"written   : {out}")
    if easy_medium_only:
        print("pool      : easy+medium only — NOT a full-benchmark solve rate")

    if args.dry_run:
        print("\ndry run — nothing submitted. Run it with:")
        print(f"  arenabench cloud run {out} --ref main")
        return 0

    # Credentials. A local run reads them from this shell's environment; a
    # cloud run needs them in SSM, where the trial container can reach them.
    anthropic = read_secret(
        args.anthropic_key, args.anthropic_key_file, "ANTHROPIC_API_KEY", "ANTHROPIC_KEY"
    )
    oauth = read_secret(
        args.oauth_token, args.oauth_token_file, "CLAUDE_CODE_OAUTH_TOKEN", "OAUTH_TOKEN"
    )

    if args.local:
        missing = [
            n
            for n, v in (("ANTHROPIC_API_KEY", anthropic), ("CLAUDE_CODE_OAUTH_TOKEN", oauth))
            if not v
        ]
        if missing:
            raise SystemExit(
                "error: a local run needs both credentials in this shell:\n"
                + "".join(f"       {n}\n" for n in missing)
            )
        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = anthropic or ""
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth or ""
        cmd = [sys.executable, "-m", "arenabench", "run", str(out), "--progress"]
        print("\nlaunching locally:", " ".join(cmd))
        return subprocess.run(cmd, env=env, check=False).returncode

    if anthropic or oauth:
        print("\nstaging credentials into SSM:")
        if anthropic:
            stage_ssm(SSM_ANTHROPIC, anthropic, args.region)
        if oauth:
            stage_ssm(SSM_OAUTH, oauth, args.region)
    else:
        print(
            "\nno credentials passed — assuming /arenabench/* in SSM is already "
            "current.\n(If a seat dies with 'required credentials are not set', "
            "that assumption was wrong.)"
        )

    cmd = [
        sys.executable, "-m", "arenabench", "cloud", "run", str(out),
        "--ref", args.ref, "--region", args.region,
    ]
    if args.no_gate:
        cmd.append("--no-gate")
    if args.no_wait:
        cmd.append("--no-wait")
    print("\nsubmitting:", " ".join(cmd))
    return subprocess.run(cmd, check=False).returncode


def add_common(parser: argparse.ArgumentParser, *, default_tasks: int) -> None:
    parser.add_argument(
        "--tasks", type=int, default=default_tasks,
        help=f"how many tasks to draw (default: {default_tasks})",
    )
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="task slots per runner for a LOCAL run; one slot is two "
             "contestant containers racing. Irrelevant to a cloud submit, "
             "which fans out one job per trial.",
    )
    parser.add_argument("--seed", type=int, default=None,
                        help="reproduce an earlier draw")
    parser.add_argument("--name", default=None, help="match name")
    parser.add_argument("-o", "--output", default=None, help="where to write the toml")
    parser.add_argument("--anthropic-key", default=None,
                        help="Stella's Anthropic API key (visible in `ps` — "
                             "prefer --anthropic-key-file or $ANTHROPIC_API_KEY)")
    parser.add_argument("--anthropic-key-file", default=None,
                        help="file holding Stella's Anthropic API key")
    parser.add_argument("--oauth-token", default=None,
                        help="Claude Code's OAuth token (visible in `ps` — "
                             "prefer --oauth-token-file or $CLAUDE_CODE_OAUTH_TOKEN)")
    parser.add_argument("--oauth-token-file", default=None,
                        help="file holding Claude Code's OAuth token")
    parser.add_argument("--ref", default="main", help="git ref of the Stella SUT")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--local", action="store_true",
                        help="run on this machine instead of AWS Batch")
    parser.add_argument("--dry-run", action="store_true",
                        help="write the toml and stop")
    parser.add_argument("--no-gate", action="store_true",
                        help="waive the banned-behavior gate (printed in the header)")
    parser.add_argument("--no-wait", action="store_true",
                        help="submit and return, do not follow the run")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quickstart",
        description="Stella vs Claude Code, both on sonnet 5 at low effort.",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    h2h = subs.add_parser(
        "h2h", help="the head-to-head over a sample of the whole dataset"
    )
    add_common(h2h, default_tasks=10)

    fb = subs.add_parser(
        "frontierbench",
        help="quick start over easy/medium tasks only (use --include-hard to widen)",
    )
    add_common(fb, default_tasks=10)
    fb.add_argument(
        "--include-hard", action="store_true",
        help="widen the pool to every difficulty. Off by default: this quick "
             "start exists to be fast and cheap, and says so in the match file.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "h2h":
        args.output = args.output or "matches/generated-h2h.toml"
        return run(args, easy_medium_only=False, label="h2h")

    args.output = args.output or "matches/generated-frontierbench.toml"
    return run(args, easy_medium_only=not args.include_hard, label="frontierbench")


if __name__ == "__main__":
    raise SystemExit(main())
