#!/usr/bin/env bash
# Mirror the local experiments store into the durable benchmark database.
#
# The database host is reached over SSM only: the SQL travels as gzip+base64
# through RunShellScript chunks, so no database port opens and no credentials
# leave the host. The flow is emit -> ship -> apply -> verify -> mark:
# arenabench.mirror emits idempotent SQL plus a manifest of what that SQL
# carries, the host applies it inside the Postgres container, every hash the
# manifest names is confirmed present, and only then are the local rows the
# manifest names stamped as migrated. A failure anywhere stops before the
# stamp, so a failed push can never read as a finished one, and the manifest
# is what keeps the stamp off rows stored while the push was in flight.
set -euo pipefail

INSTANCE_ID="${MIRROR_INSTANCE_ID:-i-023d002d6e44f8f84}"
CONTAINER="${MIRROR_PG_CONTAINER:-oxagen-data-postgres-1}"
PG_USER="${MIRROR_PG_USER:-oxagen}"
PG_DB="${MIRROR_PG_DB:-benchmarks}"
SOURCE="${MIRROR_SOURCE:-$(hostname -s)@local}"
# Base64 is chunked to stay under the SSM command-size limit.
CHUNK_BYTES=60000

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

remote() {
    # Run one command on the host and print its stdout; fail on its failure.
    # The command travels as a JSON parameters file — json.dumps, not shell
    # interpolation, is what keeps quotes inside SQL intact on the wire.
    local command_id
    printf '%s' "$1" | python3 -c \
        'import json, sys; print(json.dumps({"commands": [sys.stdin.read()]}))' \
        > "$WORK/params.json"
    command_id="$(aws ssm send-command \
        --instance-ids "$INSTANCE_ID" \
        --document-name AWS-RunShellScript \
        --parameters "file://$WORK/params.json" \
        --query 'Command.CommandId' --output text)"
    aws ssm wait command-executed \
        --command-id "$command_id" --instance-id "$INSTANCE_ID" || true
    local status
    status="$(aws ssm get-command-invocation \
        --command-id "$command_id" --instance-id "$INSTANCE_ID" \
        --query 'Status' --output text)"
    aws ssm get-command-invocation \
        --command-id "$command_id" --instance-id "$INSTANCE_ID" \
        --query 'StandardOutputContent' --output text
    if [ "$status" != "Success" ]; then
        aws ssm get-command-invocation \
            --command-id "$command_id" --instance-id "$INSTANCE_ID" \
            --query 'StandardErrorContent' --output text >&2
        echo "remote command failed ($status): $1" >&2
        return 1
    fi
}

echo "== emit"
uv run --project "$ROOT" python -m arenabench.mirror emit \
    --source "$SOURCE" --out "$WORK/mirror.sql" \
    --manifest "$WORK/mirror.manifest.json"

# The manifest, not a grep over the SQL, is what the verification counts:
# it holds exactly the durable keys the script inserts, deduplicated the
# same way the durable table's primary key deduplicates them.
manifest_read() {
    python3 - "$WORK/mirror.manifest.json" <<'PY'
import json
import re
import sys

record = json.load(open(sys.argv[1]))
hashes = record["hashes"]
if len(set(hashes)) != len(hashes):
    sys.exit("manifest lists a durable key twice")
if not all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
    sys.exit("manifest lists a key that is not a sha256 digest")
quote = chr(39)
print(len(hashes))
print(",".join(quote + h + quote for h in hashes))
PY
}
{ read -r row_count; read -r sha_list; } < <(manifest_read)

if [ "$row_count" -eq 0 ]; then
    echo "nothing to mirror"
    exit 0
fi

echo "== ship ($row_count rows)"
gzip -9 -c "$WORK/mirror.sql" | base64 | tr -d '\n' > "$WORK/payload.b64"
split -b "$CHUNK_BYTES" "$WORK/payload.b64" "$WORK/chunk."
remote "rm -f /tmp/bench-mirror.b64" > /dev/null
for chunk in "$WORK"/chunk.*; do
    remote "printf %s '$(cat "$chunk")' >> /tmp/bench-mirror.b64" > /dev/null
done

echo "== apply"
# Written as a script rather than an && / || chain: with the chain, a failed
# reconstruction fell through to the `||` arm and created the database anyway,
# reporting success for a push that had shipped nothing.
remote "set -e
base64 -d /tmp/bench-mirror.b64 | gunzip > /tmp/bench-mirror.sql
if ! docker exec $CONTAINER psql -U $PG_USER -tAc \
    \"SELECT 1 FROM pg_database WHERE datname = '$PG_DB'\" | grep -q 1; then
    docker exec $CONTAINER psql -U $PG_USER -c \"CREATE DATABASE \\\"$PG_DB\\\"\"
fi" > /dev/null
remote "docker exec -i $CONTAINER psql -U $PG_USER -d $PG_DB \
--set ON_ERROR_STOP=1 -q -f - < /tmp/bench-mirror.sql \
&& rm -f /tmp/bench-mirror.b64 /tmp/bench-mirror.sql" > /dev/null

echo "== verify"
landed="$(remote "docker exec $CONTAINER psql -U $PG_USER -d $PG_DB -tAc \
\"SELECT COUNT(*) FROM experiment_results WHERE doc_sha256 IN ($sha_list)\"" \
    | tr -d '[:space:]')"
if [ "$landed" != "$row_count" ]; then
    echo "verification failed: emitted $row_count rows, found $landed" >&2
    echo "local rows were NOT marked migrated" >&2
    exit 1
fi

echo "== mark"
uv run --project "$ROOT" python -m arenabench.mirror mark \
    --manifest "$WORK/mirror.manifest.json"
echo "mirrored $row_count rows to $PG_DB on $INSTANCE_ID as $SOURCE"
