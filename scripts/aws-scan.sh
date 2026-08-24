#!/usr/bin/env bash
#
# aws-scan.sh — what is costing money in this account right now.
#
#   Usage:  scripts/aws-scan.sh [--region R] [--quiet]
#
#     --quiet   print only the summary lines (for a cron or a status bar)
# --help text ends here.
#
# READ-ONLY. Every call here is a Describe/List; nothing is created, stopped
# or deleted. `scripts/aws-pause.sh` is the half that acts, and it reads the
# same sources so the two cannot disagree about what is running.
#
# WHY THIS EXISTS
#
# The substrate is scale-to-zero by design — Batch compute environments sit at
# `minvCpus: 0` and bill nothing idle — which is exactly what makes a stuck
# job expensive: it looks like nothing is happening. A wedged trial holds a
# whole m6i.xlarge for as long as the job definition's three-hour timeout
# allows, and a match killed halfway can leave its siblings queued behind it.
# Neither shows up anywhere a person looks unless they go looking.
#
# So this prints, in one screen: what compute is running, what is queued
# behind it, what non-Batch machines are up (the benchmark rig is the usual
# offender), and what is billing by the hour whether or not anything is using
# it — NAT gateways, idle load balancers, unattached elastic IPs. Storage is
# reported separately and in dollars per month, because it is a different
# question with a different answer.
#
# ON THE DOLLAR FIGURES
#
# They are an ESTIMATE from a small hard-coded table (`price_per_hour`),
# on-demand us-east-1, checked 2026-08-24. They are here to answer "is this
# worth caring about" and nothing else — never to reconcile a bill. An
# instance type the table does not know prints `?` and is counted into a
# separate "unpriced" line rather than as zero: a total that silently omits
# the expensive thing is worse than no total at all.
# shellcheck disable=SC2016
# ^ SC2016 warns that expressions do not expand in single quotes. Every hit in
#   this file is an `aws --query` argument, where the backticks are JMESPath
#   *literal* syntax and the single quotes are exactly what keeps the shell out
#   of them. The lint is wrong here, not the code.
set -uo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
QUIET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    -h|--help)
      sed -n '2,/^# --help text ends here\.$/p' "$0" | sed 's/^#\{1,2\} \{0,1\}//;$d'
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v aws >/dev/null 2>&1 || { echo "aws CLI not on PATH" >&2; exit 2; }

bold=""; dim=""; red=""; green=""; yellow=""; reset=""
if [ -t 1 ]; then
  bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'
  yellow=$'\033[33m'; reset=$'\033[0m'
fi

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
head2() { [ "$QUIET" -eq 1 ] || printf '\n%s%s%s\n' "$bold" "$1" "$reset"; }

aws_q() { aws --region "$REGION" "$@" 2>/dev/null; }

# On-demand $/hour, us-east-1, checked 2026-08-24. Only the families this
# account's compute environments can actually launch, plus the rig's. An
# unknown type is reported as unpriced rather than guessed at.
price_per_hour() {
  case "$1" in
    t3.micro)      echo "0.0104" ;;
    t3.small)      echo "0.0208" ;;
    t3.medium)     echo "0.0416" ;;
    t4g.micro)     echo "0.0084" ;;
    t4g.small)     echo "0.0168" ;;
    t4g.medium)    echo "0.0336" ;;
    t4g.large)     echo "0.0672" ;;
    m6i.large)     echo "0.096" ;;
    m6i.xlarge)    echo "0.192" ;;
    m6i.2xlarge)   echo "0.384" ;;
    m6i.4xlarge)   echo "0.768" ;;
    c6i.xlarge)    echo "0.170" ;;
    c7i.xlarge)    echo "0.178" ;;
    g5.xlarge)     echo "1.006" ;;
    g5.2xlarge)    echo "1.212" ;;
    g6.xlarge)     echo "0.805" ;;
    g6e.xlarge)    echo "1.861" ;;
    g6e.2xlarge)   echo "2.242" ;;
    *) echo "" ;;
  esac
}

TOTAL_HOURLY="0"
UNPRICED=0
FINDINGS=0

add_hourly() { TOTAL_HOURLY="$(awk -v a="$TOTAL_HOURLY" -v b="$1" 'BEGIN{printf "%.4f", a+b}')"; }

# ── Batch ────────────────────────────────────────────────────────────────────
#
# Queued states are listed as well as RUNNING, because "nothing is running" is
# not the same as "nothing is about to". A queue with 40 RUNNABLE jobs is a
# bill that has not started yet, and it is the one state a person can still
# cancel for free.

head2 "AWS Batch"
QUEUES="$(aws_q batch describe-job-queues --query 'jobQueues[].jobQueueName' --output text)"
if [ -z "$QUEUES" ]; then
  say "  ${dim}no job queues in this region${reset}"
else
  for q in $QUEUES; do
    line=""
    for state in RUNNING STARTING RUNNABLE PENDING SUBMITTED; do
      n="$(aws_q batch list-jobs --job-queue "$q" --job-status "$state" \
            --query 'length(jobSummaryList)' --output text)"
      [ -z "$n" ] && n=0
      [ "$n" = "0" ] || line="${line}${state}=${n}  "
    done
    if [ -n "$line" ]; then
      FINDINGS=$((FINDINGS + 1))
      say "  ${yellow}${q}${reset}  ${line}"
      # Name the running jobs: a person about to pause needs to know whether
      # they are killing a live measurement or a leftover.
      aws_q batch list-jobs --job-queue "$q" --job-status RUNNING \
        --query 'jobSummaryList[].[jobId,jobName,startedAt]' --output text \
        | while read -r jid jname started; do
            [ -z "$jid" ] && continue
            if [ -n "$started" ] && [ "$started" != "None" ]; then
              mins=$(( ( $(date +%s) - started / 1000 ) / 60 ))
              say "    running ${mins}m  ${jid}  ${dim}${jname}${reset}"
            else
              say "    running       ${jid}  ${dim}${jname}${reset}"
            fi
          done
    else
      say "  ${green}${q}${reset}  ${dim}idle${reset}"
    fi
  done
fi

# Compute environments: `desiredvCpus` above zero means instances are up (or
# coming up) regardless of whether any job is using them.
CE_ROWS="$(aws_q batch describe-compute-environments \
  --query 'computeEnvironments[].[computeEnvironmentName,state,status,computeResources.desiredvCpus,computeResources.maxvCpus]' \
  --output text)"
if [ -n "$CE_ROWS" ]; then
  printf '%s\n' "$CE_ROWS" | while read -r name state status desired maxv; do
    [ -z "$name" ] && continue
    if [ "${desired:-0}" != "0" ] && [ "${desired:-0}" != "None" ]; then
      say "  ${yellow}${name}${reset}  ${state}/${status}  desired=${desired} vCPU (max ${maxv})"
    else
      say "  ${dim}${name}  ${state}/${status}  desired=0${reset}"
    fi
  done
fi

# ── EC2 ──────────────────────────────────────────────────────────────────────
#
# Batch-managed instances are labelled as such and NOT counted as strays: they
# are the consequence of the jobs above, and killing them directly fights the
# scheduler (Batch replaces them). The rows that matter here are the ones
# nothing is managing — a benchmark rig started for an afternoon and still up.

head2 "EC2 (running instances)"
EC2_ROWS="$(aws_q ec2 describe-instances \
  --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,LaunchTime,Tags[?Key==`Name`]|[0].Value,Tags[?Key==`aws:batch:compute-environment`]|[0].Value]' \
  --output text)"
if [ -z "$EC2_ROWS" ]; then
  say "  ${green}none running${reset}"
else
  while read -r iid itype launched name batch_ce; do
    [ -z "$iid" ] && continue
    price="$(price_per_hour "$itype")"
    if [ -n "$price" ]; then
      cost="\$${price}/h"
      add_hourly "$price"
    else
      cost="${red}unpriced${reset}"
      UNPRICED=$((UNPRICED + 1))
    fi
    if [ -n "$batch_ce" ] && [ "$batch_ce" != "None" ]; then
      tag="${dim}batch-managed${reset}"
    else
      tag="${red}not batch-managed — nothing will stop this for you${reset}"
      FINDINGS=$((FINDINGS + 1))
    fi
    [ "$name" = "None" ] && name=""
    say "  ${iid}  ${itype}  ${cost}  ${dim}up since ${launched}${reset}  ${name} ${tag}"
  done <<EOF
$EC2_ROWS
EOF
fi

# ── CodeBuild ────────────────────────────────────────────────────────────────

head2 "CodeBuild (builds in flight)"
BUILD_IDS="$(aws_q codebuild list-builds --sort-order DESCENDING \
  --query 'ids[0:20]' --output text)"
INFLIGHT=""
if [ -n "$BUILD_IDS" ]; then
  # shellcheck disable=SC2086  # deliberate word-splitting: batch-get takes a list
  INFLIGHT="$(aws_q codebuild batch-get-builds --ids $BUILD_IDS \
    --query 'builds[?buildStatus==`IN_PROGRESS`].[id,projectName,startTime]' \
    --output text)"
fi
if [ -z "$INFLIGHT" ]; then
  say "  ${green}none in progress${reset}"
else
  FINDINGS=$((FINDINGS + 1))
  printf '%s\n' "$INFLIGHT" | while read -r bid proj started; do
    [ -z "$bid" ] && continue
    say "  ${yellow}${bid}${reset}  ${proj}  ${dim}since ${started}${reset}"
  done
fi

# ── Billed-by-the-hour whether used or not ───────────────────────────────────
#
# The quiet ones. None of these are created by this project's templates, which
# is exactly why they are worth printing: something else in the account made
# them, and nobody is watching that thing's bill either.

head2 "Hourly whether idle or not"
NATS="$(aws_q ec2 describe-nat-gateways \
  --filter Name=state,Values=available \
  --query 'NatGateways[].NatGatewayId' --output text)"
if [ -n "$NATS" ]; then
  for n in $NATS; do
    FINDINGS=$((FINDINGS + 1))
    add_hourly "0.045"
    say "  ${red}NAT gateway${reset} ${n}  \$0.045/h  ${dim}+ data processing${reset}"
  done
else
  say "  ${green}no NAT gateways${reset}"
fi

EIPS="$(aws_q ec2 describe-addresses \
  --query 'Addresses[?AssociationId==null].PublicIp' --output text)"
if [ -n "$EIPS" ]; then
  for ip in $EIPS; do
    FINDINGS=$((FINDINGS + 1))
    add_hourly "0.005"
    say "  ${yellow}unattached elastic IP${reset} ${ip}  \$0.005/h"
  done
fi

LBS="$(aws_q elbv2 describe-load-balancers \
  --query 'LoadBalancers[].LoadBalancerName' --output text)"
if [ -n "$LBS" ]; then
  for lb in $LBS; do
    FINDINGS=$((FINDINGS + 1))
    add_hourly "0.0225"
    say "  ${yellow}load balancer${reset} ${lb}  ~\$0.0225/h + LCU"
  done
fi

RDS="$(aws_q rds describe-db-instances \
  --query 'DBInstances[?DBInstanceStatus==`available`].[DBInstanceIdentifier,DBInstanceClass]' \
  --output text)"
if [ -n "$RDS" ]; then
  printf '%s\n' "$RDS" | while read -r db cls; do
    [ -z "$db" ] && continue
    say "  ${yellow}RDS${reset} ${db} (${cls})  ${dim}unpriced here${reset}"
  done
fi

# ── Storage ──────────────────────────────────────────────────────────────────
#
# Reported per MONTH, and deliberately not folded into the hourly total: it is
# a different decision. Nobody pauses a bucket; they decide whether to prune
# it. `arenabench cloud fetch` pulls what matters onto a disk anyway.

head2 "Storage (\$/month, not pausable)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
BUCKET="arenabench-artifacts-${ACCOUNT}"
BYTES="$(aws_q cloudwatch get-metric-statistics \
  --namespace AWS/S3 --metric-name BucketSizeBytes \
  --dimensions Name=BucketName,Value="$BUCKET" Name=StorageType,Value=StandardStorage \
  --start-time "$(date -u -v-3d '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '3 days ago' '+%Y-%m-%dT%H:%M:%SZ')" \
  --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --period 86400 --statistics Maximum \
  --query 'sort_by(Datapoints,&Timestamp)[-1].Maximum' --output text)"
if [ -n "$BYTES" ] && [ "$BYTES" != "None" ]; then
  say "  s3://${BUCKET}  $(awk -v b="$BYTES" 'BEGIN{printf "%.1f GiB  ~$%.2f/mo", b/1073741824, (b/1073741824)*0.023}')"
else
  say "  ${dim}s3://${BUCKET} — no CloudWatch datapoint yet (metrics lag ~1 day)${reset}"
fi

ECR_BYTES="$(aws_q ecr describe-images --repository-name arenabench/runner \
  --query 'sum(imageDetails[].imageSizeInBytes)' --output text)"
if [ -n "$ECR_BYTES" ] && [ "$ECR_BYTES" != "None" ]; then
  say "  ecr/arenabench/runner  $(awk -v b="$ECR_BYTES" 'BEGIN{printf "%.1f GiB  ~$%.2f/mo", b/1073741824, (b/1073741824)*0.10}')"
fi

# ── Verdict ──────────────────────────────────────────────────────────────────

printf '\n'
if [ "$FINDINGS" -eq 0 ]; then
  printf '%s✔ nothing is running%s  — compute is scaled to zero; only storage bills.\n' \
    "$green" "$reset"
else
  printf '%s%d thing(s) running%s  ~%s$%s/hour%s  (~$%s/day if left up)\n' \
    "$bold" "$FINDINGS" "$reset" "$bold" "$TOTAL_HOURLY" "$reset" \
    "$(awk -v h="$TOTAL_HOURLY" 'BEGIN{printf "%.2f", h*24}')"
  if [ "$UNPRICED" -gt 0 ]; then
    printf '%s  %d instance(s) had no price in this script'"'"'s table — the total above is LOW.%s\n' \
      "$red" "$UNPRICED" "$reset"
  fi
  printf '  Pause it with: %smake aws-pause%s   (dry run; add APPLY=1 to act)\n' \
    "$bold" "$reset"
fi
printf '%sEstimates only — on-demand us-east-1, table checked 2026-08-24. Never a bill.%s\n' \
  "$dim" "$reset"
