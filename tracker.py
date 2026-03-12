#!/usr/bin/env python3
"""
Claude Code Usage Tracker
=========================
Parses local Claude Code session data (~/.claude/projects/) and pushes
an aggregate stats JSON to a shared GitHub Gist for friend comparison.

How it works:
  1. Scans all JSONL session files Claude Code writes locally
  2. Extracts per-message metadata: tokens, model, tools, timestamps
  3. Buckets activity by calendar date (using message timestamps)
  4. Produces both cumulative totals and a daily time series
  5. PATCHes the result onto a shared GitHub Gist (one file per user)

The daily breakdown lets the dashboard show trends, streaks, and
"today vs. all time" leaderboards — all from the same data source.

Designed to run daily via cron — see setup.sh for installation.
"""

import json
import glob
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone, date
from pathlib import Path

# ─── Configuration ──────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
CONFIG_PATH = Path(__file__).parent / "config.json"

# Approximate pricing per million tokens (USD).
# Ballpark figures for fun comparison — not billing-accurate.
# Source: https://docs.anthropic.com/en/docs/about-claude/models
MODEL_PRICING = {
    # model_substring: (input_per_M, output_per_M)
    "opus":   (15.0, 75.0),
    "sonnet": (3.0,  15.0),
    "haiku":  (0.25, 1.25),
}


def get_ssl_context() -> ssl.SSLContext:
    """
    Build an SSL context with proper root certificates.

    Homebrew Python on macOS ships without bundled CA certs, so
    ssl.create_default_context() starts with an empty cert store.
    We fix this by extracting certs from the macOS system keychain
    via the `security` CLI and loading them into a temp PEM file.
    """
    ctx = ssl.create_default_context()

    # If certs are already loaded (e.g., certifi is installed), use them
    if ctx.cert_store_stats()["x509_ca"] > 0:
        return ctx

    # Extract root CAs from the macOS system keychain
    try:
        result = subprocess.run(
            ["security", "find-certificate", "-a", "-p",
             "/System/Library/Keychains/SystemRootCertificates.keychain"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "BEGIN CERTIFICATE" in result.stdout:
            # Write to a temp file and load it
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                f.write(result.stdout)
                f.flush()
                ctx.load_verify_locations(f.name)
            return ctx
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Last resort: disable verification (not ideal, but this is a fun tracker)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Shared SSL context (created once, reused for all requests)
SSL_CTX = get_ssl_context()


def load_config() -> dict:
    """Load config.json created by setup.sh."""
    if not CONFIG_PATH.exists():
        print("ERROR: config.json not found. Run setup.sh first.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ─── Parsing ────────────────────────────────────────────────────────────────

def iter_session_files() -> list[Path]:
    """Find all JSONL session files across projects."""
    pattern = str(CLAUDE_DIR / "projects" / "*" / "*.jsonl")
    return [Path(p) for p in glob.glob(pattern)]


def parse_timestamp(ts) -> datetime | None:
    """
    Normalize a timestamp to a timezone-aware datetime.

    Claude Code uses two formats:
      - ISO 8601 strings (e.g., "2026-03-11T18:24:11.890Z")
      - Epoch milliseconds (e.g., 1772956579443) — used in history.jsonl
    """
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return None


def parse_session(filepath: Path) -> list[dict]:
    """
    Parse a single session JSONL file into a list of activity records.

    Returns one record per assistant message, each tagged with its date.
    This per-message granularity lets us bucket by day during aggregation.
    """
    messages = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Extract project name from the directory-encoded path
    project_dir = filepath.parent.name
    project_name = project_dir.rsplit("-", 1)[-1] if "-" in project_dir else project_dir

    session_id = None
    records = []

    # We also need all timestamps for session duration
    all_timestamps = []

    for msg in messages:
        ts_raw = msg.get("timestamp")
        ts = parse_timestamp(ts_raw)
        if ts:
            all_timestamps.append(ts)

        if session_id is None and msg.get("sessionId"):
            session_id = msg["sessionId"]

        if msg.get("type") != "assistant":
            continue

        inner = msg.get("message", {})
        usage = inner.get("usage", {})
        model = inner.get("model", "unknown")

        # Count tools in this message
        tools = Counter()
        agent_spawns = 0
        for block in inner.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name = block.get("name", "unknown")
                tools[tool_name] += 1
                if tool_name == "Agent":
                    agent_spawns += 1

        # Tag with the calendar date (local timezone of the machine)
        msg_date = ts.astimezone().date().isoformat() if ts else None

        records.append({
            "date": msg_date,
            "session_id": session_id,
            "project": project_name,
            "model": model,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_create_tokens": usage.get("cache_creation_input_tokens", 0),
            "tools": dict(tools),
            "agent_spawns": agent_spawns,
        })

    # Compute session duration and attach to first record
    if all_timestamps and records:
        all_timestamps.sort()
        duration = (all_timestamps[-1] - all_timestamps[0]).total_seconds()
        records[0]["session_duration_seconds"] = duration

    return records


# ─── Cost Estimation ───────────────────────────────────────────────────────

def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """
    Rough cost estimate for a given model's token counts.

    Matches model string against known pricing tiers by substring.
    Falls back to sonnet pricing if the model is unrecognized.
    """
    for keyword, (in_price, out_price) in MODEL_PRICING.items():
        if keyword in model.lower():
            return (input_tokens / 1_000_000 * in_price) + (output_tokens / 1_000_000 * out_price)
    return (input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0)


# ─── Aggregation ────────────────────────────────────────────────────────────

def aggregate_records(records: list[dict]) -> dict:
    """
    Aggregate a list of per-message records into summary stats.

    Used for both cumulative totals and per-day slices — same logic,
    different input sets.
    """
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    total_duration = 0.0
    models = Counter()
    tools = Counter()
    projects = set()
    sessions = set()
    agent_spawns = 0
    model_tokens = defaultdict(lambda: {"input": 0, "output": 0})

    for r in records:
        total_input += r["input_tokens"]
        total_output += r["output_tokens"]
        total_cache_read += r["cache_read_tokens"]
        total_cache_create += r["cache_create_tokens"]
        total_duration += r.get("session_duration_seconds", 0)
        agent_spawns += r["agent_spawns"]
        projects.add(r["project"])
        if r.get("session_id"):
            sessions.add(r["session_id"])

        models[r["model"]] += 1
        for tool, count in r["tools"].items():
            tools[tool] += count

        model_tokens[r["model"]]["input"] += r["input_tokens"]
        model_tokens[r["model"]]["output"] += r["output_tokens"]

    total_cost = sum(
        estimate_cost(t["input"], t["output"], m)
        for m, t in model_tokens.items()
    )

    return {
        "total_sessions": len(sessions),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_create_tokens": total_cache_create,
        "total_tokens": total_input + total_output,
        "total_duration_hours": round(total_duration / 3600, 2),
        "estimated_cost_usd": round(total_cost, 2),
        "agent_spawns": agent_spawns,
        "models": dict(models.most_common()),
        "tools": dict(tools.most_common()),
        "projects": sorted(projects),
        "num_projects": len(projects),
    }


def build_stats(all_records: list[dict]) -> dict:
    """
    Build the full stats payload: cumulative totals + daily time series.

    The daily array is sorted by date and contains the same metrics
    as the cumulative object, just scoped to that calendar day.
    This lets the dashboard show trends, "today" stats, and streaks.
    """
    # Filter out records with no token activity
    records = [r for r in all_records if r["input_tokens"] > 0 or r["output_tokens"] > 0]

    # ── Cumulative totals ──
    cumulative = aggregate_records(records)
    cumulative["last_updated"] = datetime.now(timezone.utc).isoformat()

    # ── Daily breakdown ──
    # Group records by date, then aggregate each day independently
    by_date = defaultdict(list)
    for r in records:
        day = r.get("date") or "unknown"
        by_date[day].append(r)

    daily = []
    for day in sorted(by_date.keys()):
        if day == "unknown":
            continue
        day_stats = aggregate_records(by_date[day])
        day_stats["date"] = day
        daily.append(day_stats)

    # ── Streak calculation ──
    # How many consecutive days (ending today or yesterday) had activity?
    active_dates = {d["date"] for d in daily}
    streak = 0
    check = date.today()
    while check.isoformat() in active_dates:
        streak += 1
        check = date.fromordinal(check.toordinal() - 1)
    # Also count if streak ended yesterday (cron runs at night)
    if streak == 0:
        check = date.fromordinal(date.today().toordinal() - 1)
        while check.isoformat() in active_dates:
            streak += 1
            check = date.fromordinal(check.toordinal() - 1)

    cumulative["current_streak_days"] = streak
    cumulative["total_active_days"] = len(active_dates)

    return {
        "cumulative": cumulative,
        "daily": daily,
    }


# ─── Gist Upload ────────────────────────────────────────────────────────────

def fetch_existing_data(config: dict) -> dict | None:
    """
    Fetch this user's existing data from the Gist (if any).

    This lets us preserve historical daily data even if local session
    files get cleaned up. We merge rather than replace.
    """
    gist_id = config["gist_id"]
    token = config["github_token"]
    username = config["username"]
    filename = f"{username}.json"

    url = f"https://api.github.com/gists/{gist_id}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )

    try:
        with urllib.request.urlopen(req, context=SSL_CTX) as resp:
            gist = json.loads(resp.read().decode("utf-8"))
            if filename in gist.get("files", {}):
                content = gist["files"][filename].get("content", "{}")
                return json.loads(content)
    except (urllib.error.HTTPError, json.JSONDecodeError, KeyError):
        pass
    return None


def merge_daily_data(existing: dict | None, new_stats: dict) -> dict:
    """
    Merge new daily data with any existing data on the Gist.

    For each date, the freshly-parsed local data takes priority
    (it's the most complete). But dates that only exist in the
    existing Gist data are preserved — this handles the case where
    old session files were deleted locally.
    """
    if not existing or "daily" not in existing:
        return new_stats

    # Index new daily entries by date
    new_by_date = {d["date"]: d for d in new_stats["daily"]}

    # Add any old dates not present in the new parse
    for old_day in existing.get("daily", []):
        if old_day["date"] not in new_by_date:
            new_by_date[old_day["date"]] = old_day

    # Rebuild sorted daily array
    new_stats["daily"] = [
        new_by_date[d] for d in sorted(new_by_date.keys())
    ]

    return new_stats


def push_to_gist(config: dict, stats: dict) -> None:
    """
    Update (or create) this user's file in the shared Gist.

    Uses the GitHub Gist API:
      PATCH /gists/{gist_id}
      Body: {"files": {"username.json": {"content": "..."}}}

    The PATCH merges files — it won't overwrite other participants' data.
    """
    gist_id = config["gist_id"]
    token = config["github_token"]
    username = config["username"]
    filename = f"{username}.json"

    payload = json.dumps({
        "files": {
            filename: {
                "content": json.dumps(stats, indent=2)
            }
        }
    }).encode("utf-8")

    url = f"https://api.github.com/gists/{gist_id}"
    req = urllib.request.Request(
        url,
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, context=SSL_CTX) as resp:
            if resp.status == 200:
                print(f"  Updated {filename} on Gist {gist_id}")
            else:
                print(f"WARNING: Unexpected status {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR pushing to Gist: {e.code} {e.reason}\n{body}")
        sys.exit(1)


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    config = load_config()
    session_files = iter_session_files()

    if not session_files:
        print("No Claude Code session data found in ~/.claude/projects/")
        sys.exit(0)

    print(f"Parsing {len(session_files)} session files...")

    # Parse all sessions into flat list of per-message records
    all_records = []
    for f in session_files:
        all_records.extend(parse_session(f))

    stats = build_stats(all_records)

    # Merge with existing Gist data to preserve history
    existing = fetch_existing_data(config)
    stats = merge_daily_data(existing, stats)

    # Pretty-print local summary
    c = stats["cumulative"]
    today = date.today().isoformat()
    today_data = next((d for d in stats["daily"] if d["date"] == today), None)

    print(f"\n{'─' * 50}")
    print(f"  ALL TIME")
    print(f"  Sessions:       {c['total_sessions']}")
    print(f"  Total tokens:   {c['total_tokens']:,}")
    print(f"  Time (hours):   {c['total_duration_hours']}")
    print(f"  Est. cost:      ${c['estimated_cost_usd']:.2f}")
    print(f"  Agent spawns:   {c['agent_spawns']}")
    print(f"  Active days:    {c['total_active_days']}")
    print(f"  Current streak: {c['current_streak_days']} days")
    print(f"  Models:         {c['models']}")

    if today_data:
        print(f"\n  TODAY ({today})")
        print(f"  Sessions:       {today_data['total_sessions']}")
        print(f"  Tokens:         {today_data['total_tokens']:,}")
        print(f"  Est. cost:      ${today_data['estimated_cost_usd']:.2f}")
        print(f"  Agent spawns:   {today_data['agent_spawns']}")
    else:
        print(f"\n  TODAY ({today}): no activity yet")

    print(f"{'─' * 50}\n")

    push_to_gist(config, stats)


if __name__ == "__main__":
    main()
