# SPDX-License-Identifier: Apache-2.0
"""The arenabench.org control plane — one Lambda behind API Gateway.

This replaces the Vercel Next.js app that used to live in ``web/`` (retired
per the SaaS spec §9.4: the control plane is 100% AWS). Same surface as that
app — built binary refs, their latest manifests, the most recent runner jobs,
and two actions (rebuild the SUT from a ref, submit a smoke trial) — plus the
login wall that app never grew:

* **Magic-link only.** There is no password and no signup. POST /login takes
  an email address; if it is on the allowlist a one-time link signed with the
  auth secret is mailed to it via SES. Following the link within 15 minutes
  sets a 30-day session cookie. Every other path requires that cookie.
* **The allowlist is configuration, not code.** ``ALLOWED_EMAILS`` (comma
  separated, case-insensitive) comes from the CloudFormation parameter
  ``AllowedEmails`` on the ``arenabench-web`` stack — edit the parameter and
  redeploy to admit or remove an address. Enumeration is not offered: the
  login page answers identically whether or not the address is allowed.
* **Fail closed.** The auth secret lives in SSM
  (``/arenabench/web/auth-secret``, SecureString). If it cannot be read,
  every request is a 503 — unlike the old ``ACCESS_KEY`` middleware, which
  silently served the dashboard to anyone when its env var was unset.

Sessions and links are stateless HMAC tokens (``email.expiry.signature``), so
there is no session table to operate. Revocation is rotating the secret.

Deployed by ``.github/workflows/infra.yml``; template ``infra/web.yaml``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import time
import urllib.parse

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("ARTIFACTS_BUCKET", "")
TABLE = os.environ.get("ARENABENCH_TABLE", "arenabench")
DOMAIN = os.environ.get("APP_DOMAIN", "arenabench.org")
MAIL_FROM = os.environ.get("MAIL_FROM", "")
AUTH_SECRET_PARAM = os.environ.get(
    "AUTH_SECRET_PARAM", "/arenabench/web/auth-secret"
)
SESSION_TTL = 30 * 24 * 3600
LINK_TTL = 15 * 60

_s3 = boto3.client("s3", region_name=REGION)
_ddb = boto3.client("dynamodb", region_name=REGION)
_batch = boto3.client("batch", region_name=REGION)
_codebuild = boto3.client("codebuild", region_name=REGION)
_ses = boto3.client("sesv2", region_name=REGION)
_ssm = boto3.client("ssm", region_name=REGION)

_secret_cache: bytes | None = None


def _secret() -> bytes:
    """The signing secret, fetched once per container. Raises on failure —
    the handler turns that into a 503 rather than serving unauthenticated."""
    global _secret_cache
    if _secret_cache is None:
        value = _ssm.get_parameter(Name=AUTH_SECRET_PARAM, WithDecryption=True)
        _secret_cache = value["Parameter"]["Value"].strip().encode()
    return _secret_cache


def allowed_emails() -> set[str]:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# ── Tokens ───────────────────────────────────────────────────────────────────
#
# One shape for both the emailed link and the session cookie:
# base64url(email) . expiry-epoch . hmac(purpose|email|expiry). The purpose
# string keeps a login link from doubling as a session cookie.


def _sign(purpose: str, email: str, exp: int) -> str:
    msg = f"{purpose}|{email}|{exp}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_token(purpose: str, email: str, ttl: int) -> str:
    exp = int(time.time()) + ttl
    b64 = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{b64}.{exp}.{_sign(purpose, email, exp)}"


def read_token(purpose: str, token: str) -> str | None:
    """The email a valid, unexpired token carries, else None."""
    try:
        b64, exp_s, sig = token.split(".")
        email = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        exp = int(exp_s)
    except (ValueError, UnicodeDecodeError):
        return None
    if time.time() > exp:
        return None
    if not hmac.compare_digest(sig, _sign(purpose, email, exp)):
        return None
    # Re-checked on every request, so removing an address from the allowlist
    # ends its access at the next click, not at cookie expiry.
    if email.lower() not in allowed_emails():
        return None
    return email


def session_email(event: dict) -> str | None:
    for cookie in event.get("cookies") or []:
        if cookie.startswith("ab_s="):
            return read_token("session", cookie[len("ab_s=") :])
    return None


def csrf_token(email: str) -> str:
    """Per-session anti-CSRF value embedded in every action form. Derived,
    not stored: valid for the same 30 days the session is."""
    return hmac.new(
        _secret(), f"csrf|{email}".encode(), hashlib.sha256
    ).hexdigest()[:32]


# ── Data reads (parity with the retired web/lib/aws.js) ──────────────────────


def list_binary_refs() -> list[str]:
    out = _s3.list_objects_v2(Bucket=BUCKET, Prefix="binaries/", Delimiter="/")
    return [
        p["Prefix"].removeprefix("binaries/").rstrip("/")
        for p in out.get("CommonPrefixes", [])
    ]


def latest_manifest(ref: str) -> dict | None:
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=f"binaries/{ref}/latest.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def recent_jobs(limit: int = 12) -> list[dict]:
    out = _ddb.query(
        TableName=TABLE,
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :p",
        ExpressionAttributeValues={":p": {"S": "JOB"}},
        ScanIndexForward=False,
        Limit=limit,
    )
    return [
        {
            "run": item.get("PK", {}).get("S", "").removeprefix("RUN#"),
            "job": item.get("SK", {}).get("S", "").removeprefix("JOB#"),
            "at": item.get("GSI1SK", {}).get("S", ""),
            "status": item.get("status", {}).get("S", ""),
            "detail": item.get("detail", {}).get("S", ""),
            "mode": item.get("mode", {}).get("S", ""),
        }
        for item in out.get("Items", [])
    ]


def start_sut_build(ref: str) -> str:
    out = _codebuild.start_build(
        projectName="arenabench-sut-build",
        environmentVariablesOverride=[
            {"name": "GIT_REF", "value": ref, "type": "PLAINTEXT"}
        ],
    )
    return out.get("build", {}).get("id", "")


def submit_smoke() -> str:
    out = _batch.submit_job(
        jobName="smoke-web",
        jobQueue="arenabench-measure",
        jobDefinition="arenabench-trial",
    )
    return out.get("jobId", "")


# ── Mail ─────────────────────────────────────────────────────────────────────


def send_magic_link(email: str) -> None:
    token = make_token("login", email, LINK_TTL)
    link = f"https://{DOMAIN}/auth?t={urllib.parse.quote(token)}"
    _ses.send_email(
        FromEmailAddress=MAIL_FROM,
        Destination={"ToAddresses": [email]},
        Content={
            "Simple": {
                "Subject": {"Data": "Sign in to arenabench.org"},
                "Body": {
                    "Text": {
                        "Data": (
                            "Follow this link to sign in to the ArenaBench "
                            f"control plane:\n\n{link}\n\nThe link works once "
                            "and expires in 15 minutes. If you did not ask "
                            "for it, ignore this mail."
                        )
                    }
                },
            }
        },
    )


# ── HTML ─────────────────────────────────────────────────────────────────────
#
# The instrument tokens from the retired web/app/globals.css, verbatim: jet
# ground, neutral chrome, semantic colour only. Deliberately no `--identity`
# gold — the arena judges stella as one seat among several (#2577).

STYLE = """
:root { color-scheme: dark;
  --bg:#0A0A0C; --panel:#17171B; --line:#26262C; --text:#E8E8EC;
  --dim:#777782; --accent:#E8E8EC; --on-accent:#0A0A0C;
  --ok:#74C991; --bad:#E0687A; }
@media (prefers-color-scheme: light) { :root { color-scheme: light;
  --bg:#ffffff; --panel:#F7F7FA; --line:#E9E9EE; --text:#0A0A0C;
  --dim:#777782; --accent:#0A0A0C; --on-accent:#ffffff;
  --ok:#006933; --bad:#96213C; } }
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--text); margin:0;
  font:14px/1.5 "JetBrains Mono", ui-monospace, monospace; }
main { max-width:960px; margin:0 auto; padding:32px 16px; }
h1 { font-size:16px; font-weight:700; letter-spacing:.04em; }
h2 { font-size:13px; font-weight:500; color:var(--dim);
  text-transform:uppercase; letter-spacing:.08em; margin-top:32px; }
table { border-collapse:collapse; width:100%; overflow-x:auto; }
th,td { text-align:left; padding:6px 12px 6px 0; border-bottom:1px solid var(--line);
  font-size:13px; }
th { color:var(--dim); font-weight:500; }
.ok { color:var(--ok); } .bad { color:var(--bad); } .dim { color:var(--dim); }
input[type=text],input[type=email] { background:var(--panel); color:var(--text);
  border:1px solid var(--line); border-radius:0; padding:8px 12px;
  font:inherit; width:280px; }
button { background:var(--accent); color:var(--on-accent); border:none;
  border-radius:0; padding:8px 16px; font:inherit; cursor:pointer; }
form.inline { display:inline-flex; gap:8px; margin:8px 0; }
.notice { background:var(--panel); border:1px solid var(--line);
  padding:12px 16px; margin:16px 0; }
a { color:var(--text); }
"""


def page(title: str, body: str) -> dict:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
    return {
        "statusCode": 200,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "referrer-policy": "same-origin",
            "x-content-type-options": "nosniff",
        },
        "body": doc,
    }


def redirect(location: str, cookie: str | None = None) -> dict:
    resp = {"statusCode": 303, "headers": {"location": location}, "body": ""}
    if cookie is not None:
        resp["cookies"] = [cookie]
    return resp


def login_page(notice: str = "") -> dict:
    box = f"<div class='notice'>{html.escape(notice)}</div>" if notice else ""
    return page(
        "ArenaBench — sign in",
        "<h1>ArenaBench</h1>"
        "<p class='dim'>Control plane. Access is by magic link, for "
        "allowlisted addresses only.</p>" + box +
        "<form class='inline' method='post' action='/login'>"
        "<input type='email' name='email' placeholder='you@example.org' "
        "required autofocus>"
        "<button type='submit'>Email me a sign-in link</button></form>",
    )


def dashboard(email: str) -> dict:
    csrf = csrf_token(email)
    refs = list_binary_refs()
    ref_rows = []
    for ref in refs[:8]:
        man = latest_manifest(ref) or {}
        sha = str(man.get("commit", ""))[:12]
        built = str(man.get("built_at", man.get("builtAt", "")))
        ref_rows.append(
            f"<tr><td>{html.escape(ref)}</td><td>{html.escape(sha)}</td>"
            f"<td class='dim'>{html.escape(built)}</td></tr>"
        )
    job_rows = []
    for job in recent_jobs():
        cls = (
            "ok"
            if job["status"] in ("succeeded", "SUCCEEDED")
            else "bad"
            if job["status"] in ("failed", "FAILED")
            else "dim"
        )
        job_rows.append(
            f"<tr><td>{html.escape(job['at'])}</td>"
            f"<td>{html.escape(job['run'])}</td>"
            f"<td>{html.escape(job['job'])}</td>"
            f"<td class='{cls}'>{html.escape(job['status'])}</td>"
            f"<td class='dim'>{html.escape(job['detail'])}</td></tr>"
        )
    return page(
        "ArenaBench — control plane",
        "<h1>ArenaBench control plane</h1>"
        f"<p class='dim'>{html.escape(email)} — <a href='/logout'>sign out"
        "</a></p>"
        "<h2>Built binaries</h2><table><tr><th>ref</th><th>commit</th>"
        f"<th>built</th></tr>{''.join(ref_rows) or '<tr><td class=dim>none</td></tr>'}</table>"
        "<form class='inline' method='post' action='/action/build'>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        "<input type='text' name='ref' placeholder='git ref (e.g. main)' required>"
        "<button type='submit'>Build SUT</button></form>"
        "<form class='inline' method='post' action='/action/smoke'>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        "<button type='submit'>Submit smoke trial</button></form>"
        "<h2>Recent jobs</h2><table><tr><th>at</th><th>run</th><th>job</th>"
        f"<th>status</th><th>detail</th></tr>{''.join(job_rows) or '<tr><td class=dim>none yet</td></tr>'}</table>",
    )


# ── Routing ──────────────────────────────────────────────────────────────────


def _form(event: dict) -> dict[str, str]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def handler(event: dict, _context: object) -> dict:
    try:
        _secret()
    except Exception:
        return {
            "statusCode": 503,
            "headers": {"content-type": "text/plain"},
            "body": "auth secret unavailable — refusing to serve",
        }

    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", "GET"), http.get("path", "/")
    email = session_email(event)

    if path == "/login" and method == "POST":
        asked = _form(event).get("email", "").strip().lower()
        if asked in allowed_emails():
            send_magic_link(asked)
        return login_page(
            "If that address is on the allowlist, a sign-in link is on its "
            "way. It expires in 15 minutes."
        )

    if path == "/auth" and method == "GET":
        token = (event.get("queryStringParameters") or {}).get("t", "")
        verified = read_token("login", token)
        if verified is None:
            return login_page("That link is invalid or has expired. Ask for "
                              "a fresh one.")
        cookie = (
            f"ab_s={make_token('session', verified, SESSION_TTL)}; "
            f"Max-Age={SESSION_TTL}; Path=/; HttpOnly; Secure; SameSite=Lax"
        )
        return redirect("/", cookie)

    if path == "/logout":
        return redirect(
            "/", "ab_s=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"
        )

    if email is None:
        return login_page()

    if path.startswith("/action/") and method == "POST":
        form = _form(event)
        if not hmac.compare_digest(form.get("csrf", ""), csrf_token(email)):
            return login_page("Stale form — sign in again.")
        if path == "/action/build":
            ref = form.get("ref", "").strip() or "main"
            build_id = start_sut_build(ref)
            return page(
                "build started",
                f"<h1>Build started</h1><p>{html.escape(build_id)}</p>"
                "<p><a href='/'>back</a></p>",
            )
        if path == "/action/smoke":
            job_id = submit_smoke()
            return page(
                "smoke submitted",
                f"<h1>Smoke trial submitted</h1><p>{html.escape(job_id)}</p>"
                "<p><a href='/'>back</a></p>",
            )

    return dashboard(email)
