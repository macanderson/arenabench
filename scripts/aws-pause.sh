#!/usr/bin/env bash
#
# aws-pause.sh — stop what `aws-scan.sh` found running.
#
#   Usage:  scripts/aws-pause.sh [--apply] [--jobs] [--builds] [--ec2]
#                                [--hard] [--region R]
#
#     --apply    actually do it. WITHOUT THIS THE SCRIPT CHANGES NOTHING.
#     --jobs     Batch jobs only          (default: jobs + builds)
#     --builds   CodeBuild builds only
#     --ec2      also stop non-Batch EC2 instances — NOT a default, see below
#     --hard     also DISABLE the compute environments, so nothing new can
#                start until they are re-enabled. Prints the undo command.
# --help text ends here.
#
# DRY RUN IS THE DEFAULT, and that is not politeness. Terminating a Batch job
# destroys a trial that has already been paid for: an agent may be twenty
# minutes and two dollars into a task, and the artifacts of a killed trial are
# a partial upload, not a result. So the default run prints exactly what it
# WOULD end, with how long each has been going, and stops. `--apply` is a
# second, deliberate keystroke.
#
# WHAT IT DOES NOT TOUCH, AND WHY
#
#   Batch-managed EC2   Terminating an instance out from under the scheduler
#                       is not a pause — Batch notices, replaces it, and the
#                       bill continues on a fresh instance. Ending the JOBS is
#                       the pause; the instances drain on their own within
#                       minutes because every compute environment sits at
#                       `minvCpus: 0`.
#   S3, ECR, DynamoDB   Storage is not pausable, only prunable. `aws-scan.sh`
#                       reports it in dollars per month for exactly that
#                       reason. Deleting run artifacts is a decision about
#                       evidence, and it does not belong behind a verb called
#                       "pause".
#   Lambda / API GW     The arenabench.org control plane bills per request.
#                       Idle it is free, and pausing it only breaks the login.
#
# `--ec2` is opt-in because the instances it finds are usually somebody's
# deliberate long-running box (the benchmark rig, a database host), not a
# leak. It STOPS rather than terminates, so an EBS-backed instance keeps its
# disk and comes back with `aws ec2 start-instances`. An instance-store root
# does not survive a stop at all, so those are refused by name rather than
# quietly skipped.
# shellcheck disable=SC2016
# ^ SC2016 warns that expressions do not expand in single quotes. Every hit in
#   this file is an `aws --query` argument, where the backticks are JMESPath
#   *literal* syntax and the single quotes are exactly what keeps the shell out
#   of them. The lint is wrong here, not the code.
set -uo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
APPLY=0
DO_JOBS=0
DO_BUILDS=0
DO_EC2=0
HARD=0
EXPLICIT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --jobs) DO_JOBS=1; EXPLICIT=1; shift ;;
    --builds) DO_BUILDS=1; EXPLICIT=1; shift ;;
    --ec2) DO_EC2=1; EXPLICIT=1; shift ;;
    --hard) HARD=1; shift ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    -h|--help)
      sed -n '2,/^# --help text ends here\.$/p' "$0" | sed 's/^#\{1,2\} \{0,1\}//;$d'
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# No selector given means the safe pair: the two things that are always a leak
# when they are stuck, and never someone's deliberate long-running box.
if [ "$EXPLICIT" -eq 0 ]; then DO_JOBS=1; DO_BUILDS=1; fi

command -v aws >/dev/null 2>&1 || { echo "aws CLI not on PATH" >&2; exit 2; }

bold=""; dim=""; red=""; green=""; yellow=""; reset=""
if [ -t 1 ]; then
  bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'
  yellow=$'\033[33m'; reset=$'\033[0m'
fi

aws_q() { aws --region "$REGION" "$@" 2>/dev/null; }

# A file, not a variable: the loops below run in subshells (they are fed by
# pipelines and here-documents), so a counter incremented inside one does not
# survive it. The same shape `scripts/arena-kill.sh` uses.
PLAN_FILE="$(mktemp)"
trap 'rm -f "$PLAN_FILE"' EXIT
plan() { printf '%s\n' "$1" >> "$PLAN_FILE"; }
planned() { wc -l < "$PLAN_FILE" | tr -d ' '; }

if [ "$APPLY" -eq 1 ]; then
  printf '%s%s%s\n\n' "$bold" "pausing (--apply)" "$reset"
else
  printf '%s%s%s\n\n' "$bold" "DRY RUN — nothing will change. Add --apply to act." "$reset"
fi

# ── Batch jobs ───────────────────────────────────────────────────────────────
#
# Every non-terminal state, not just RUNNING: a RUNNABLE job is a bill that has
# not started, and that is the cheapest possible moment to cancel it.
# `terminate-job` covers both — AWS cancels a job that has not started yet and
# terminates one that has.

if [ "$DO_JOBS" -eq 1 ]; then
  printf '%sBatch jobs%s\n' "$bold" "$reset"
  before="$(planned)"
  QUEUES="$(aws_q batch describe-job-queues --query 'jobQueues[].jobQueueName' --output text)"
  for q in $QUEUES; do
    for state in RUNNING STARTING RUNNABLE PENDING SUBMITTED; do
      rows="$(aws_q batch list-jobs --job-queue "$q" --job-status "$state" \
        --query 'jobSummaryList[].[jobId,jobName,startedAt]' --output text)"
      [ -z "$rows" ] && continue
      while read -r jid jname started; do
        [ -z "$jid" ] && continue
        age=""
        if [ -n "$started" ] && [ "$started" != "None" ]; then
          age="$(( ( $(date +%s) - started / 1000 ) / 60 ))m in"
        fi
        plan "batch job $jid ($q, $state) $jname"
        printf '  %s%s%s  %s  %s  %s%s%s\n' \
          "$yellow" "$state" "$reset" "$jid" "$age" "$dim" "$jname" "$reset"
        if [ "$APPLY" -eq 1 ]; then
          if aws_q batch terminate-job --job-id "$jid" \
               --reason "paused by scripts/aws-pause.sh"; then
            printf '    %s✔ terminated%s\n' "$green" "$reset"
          else
            printf '    %s✗ could not terminate%s\n' "$red" "$reset"
          fi
        fi
      done <<EOF
$rows
EOF
    done
  done
  [ "$(planned)" = "$before" ] &&
    printf '  %sno jobs in any non-terminal state%s\n' "$green" "$reset"
fi

# ── CodeBuild ────────────────────────────────────────────────────────────────

if [ "$DO_BUILDS" -eq 1 ]; then
  printf '\n%sCodeBuild%s\n' "$bold" "$reset"
  IDS="$(aws_q codebuild list-builds --sort-order DESCENDING --query 'ids[0:20]' --output text)"
  INFLIGHT=""
  if [ -n "$IDS" ]; then
    # shellcheck disable=SC2086  # deliberate word-splitting: batch-get takes a list
    INFLIGHT="$(aws_q codebuild batch-get-builds --ids $IDS \
      --query 'builds[?buildStatus==`IN_PROGRESS`].[id,projectName]' --output text)"
  fi
  if [ -z "$INFLIGHT" ]; then
    printf '  %snothing in progress%s\n' "$green" "$reset"
  else
    while read -r bid proj; do
      [ -z "$bid" ] && continue
      plan "codebuild $bid ($proj)"
      printf '  %sIN_PROGRESS%s  %s  %s%s%s\n' "$yellow" "$reset" "$bid" "$dim" "$proj" "$reset"
      if [ "$APPLY" -eq 1 ]; then
        if aws_q codebuild stop-build --id "$bid" >/dev/null; then
          printf '    %s✔ stopped%s\n' "$green" "$reset"
        else
          printf '    %s✗ could not stop%s\n' "$red" "$reset"
        fi
      fi
    done <<EOF
$INFLIGHT
EOF
  fi
fi

# ── EC2 (opt-in) ─────────────────────────────────────────────────────────────

if [ "$DO_EC2" -eq 1 ]; then
  printf '\n%sEC2 (non-Batch; stopped, not terminated)%s\n' "$bold" "$reset"
  ROWS="$(aws_q ec2 describe-instances \
    --filters Name=instance-state-name,Values=running \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType,RootDeviceType,Tags[?Key==`Name`]|[0].Value,Tags[?Key==`aws:batch:compute-environment`]|[0].Value]' \
    --output text)"
  if [ -z "$ROWS" ]; then
    printf '  %snone running%s\n' "$green" "$reset"
  else
    while read -r iid itype rootdev name batch_ce; do
      [ -z "$iid" ] && continue
      if [ -n "$batch_ce" ] && [ "$batch_ce" != "None" ]; then
        printf '  %sskip%s     %s  %sbatch-managed — end its JOB instead%s\n' \
          "$dim" "$reset" "$iid" "$dim" "$reset"
        continue
      fi
      if [ "$rootdev" != "ebs" ]; then
        printf '  %sREFUSED%s  %s (%s)  %sinstance-store root: a stop DESTROYS it%s\n' \
          "$red" "$reset" "$iid" "$itype" "$dim" "$reset"
        continue
      fi
      [ "$name" = "None" ] && name=""
      plan "ec2 stop $iid ($itype) $name"
      printf '  %sstop%s     %s  %s  %s%s%s\n' \
        "$yellow" "$reset" "$iid" "$itype" "$dim" "$name" "$reset"
      if [ "$APPLY" -eq 1 ]; then
        if aws_q ec2 stop-instances --instance-ids "$iid" >/dev/null; then
          printf '    %s✔ stopping%s — restart with: aws ec2 start-instances --instance-ids %s\n' \
            "$green" "$reset" "$iid"
        else
          printf '    %s✗ could not stop%s\n' "$red" "$reset"
        fi
      fi
    done <<EOF
$ROWS
EOF
  fi
fi

# ── Compute environments (--hard) ────────────────────────────────────────────
#
# A bigger hammer than it looks: a disabled environment accepts no new jobs, so
# the next `arenabench cloud run` submits into a queue that will never place
# them and simply sits there looking queued. That is a fine state to be in
# overnight and a confusing one to walk into cold, so the undo is printed every
# time rather than left in this comment.

if [ "$HARD" -eq 1 ]; then
  printf '\n%sCompute environments (--hard)%s\n' "$bold" "$reset"
  CES="$(aws_q batch describe-compute-environments \
    --query 'computeEnvironments[?state==`ENABLED`].computeEnvironmentName' --output text)"
  if [ -z "$CES" ]; then
    printf '  %salready disabled%s\n' "$green" "$reset"
  else
    for ce in $CES; do
      plan "disable compute environment $ce"
      printf '  %sdisable%s  %s\n' "$yellow" "$reset" "$ce"
      if [ "$APPLY" -eq 1 ]; then
        if aws_q batch update-compute-environment \
             --compute-environment "$ce" --state DISABLED >/dev/null; then
          printf '    %s✔ disabled%s — re-enable with:\n' "$green" "$reset"
          printf '      aws batch update-compute-environment --compute-environment %s --state ENABLED\n' "$ce"
        else
          printf '    %s✗ could not disable%s\n' "$red" "$reset"
        fi
      fi
    done
  fi
fi

# ── Verdict ──────────────────────────────────────────────────────────────────

COUNT="$(planned)"
printf '\n'
if [ "$COUNT" = "0" ]; then
  printf '%s✔ nothing to pause%s\n' "$green" "$reset"
  exit 0
fi

if [ "$APPLY" -eq 1 ]; then
  printf '%s✔ paused %s thing(s)%s — re-scan with: make aws-scan\n' "$green" "$COUNT" "$reset"
  exit 0
fi

printf '%s%s thing(s) would be ended.%s\n' "$bold" "$COUNT" "$reset"
printf 'A running trial is paid work — killing it leaves a partial upload, not a result.\n'
printf 'Run it for real with:  %smake aws-pause APPLY=1%s\n' "$bold" "$reset"
