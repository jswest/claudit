#!/usr/bin/env python3
"""claudit — what your Claude Code usage would have cost on the pay-as-you-go API.

Walks every session transcript under ~/.claude/projects/**/*.jsonl, prices each
assistant turn against current Anthropic API rates (exact cache-tier accounting),
and reports the API-equivalent cost across a set of time windows plus a daily
chart. You're on a flat subscription, so this is a hypothetical "à la carte"
figure — a subscription-value report, not a bill.

Beyond the report, claudit does two durable things on its first run each day (or
whenever --collect is passed):

  * Persistence — upserts a per-session JSONL archive (~/.claude/claudit/
    archive.jsonl) so cost history survives Claude Code's transcript cleanup.
    Later runs render from archive ∪ live transcripts, so windows fill in past
    the retention horizon instead of evaporating.

  * Schema audit — diffs the keys actually present in your transcripts and
    usage-data against a known-fields manifest, and flags new fields (possible
    new cost drivers) or vanished ones (a lever we relied on). See SCHEMA_MANIFEST.

Usage:
    report.py [--days N] [--plan PRICE] [--retention N] [--json]
              [--projects DIR] [--config PATH] [--archive PATH] [--collect]

    --days N       Number of days in the daily chart (default: auto from retention).
    --plan PRICE   Monthly subscription price for the value comparison (default 200).
    --retention N  Override cleanupPeriodDays instead of reading it from settings.
    --json         Emit machine-readable JSON instead of the text report.
    --projects DIR Override the transcripts directory (default ~/.claude/projects).
    --config PATH  TOML config with per-user defaults (default ~/.claude/claudit.toml).
    --archive PATH Per-session archive file (default ~/.claude/claudit/archive.jsonl).
    --collect      Force the full path (refresh archive + run schema audit) even if
                   already done today. The full path also runs automatically on the
                   first invocation of each local day.

Tunables (plan, retention, projects, days) resolve as: CLI flag > config file >
built-in default. The config is signet's per-target file — the script itself stays
byte-identical across machines; your plan price lives in the TOML, not the code.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

# Per-MTok base rates (USD). Cache tiers derive from input:
#   cache read  ≈ 0.1× input,  5-min write ≈ 1.25× input,  1-hour write ≈ 2× input.
BASE_RATES = {
    "claude-fable-5":     {"in": 10.0, "out": 50.0},
    "claude-opus-4-8":    {"in": 5.0,  "out": 25.0},
    "claude-opus-4-7":    {"in": 5.0,  "out": 25.0},
    "claude-opus-4-6":    {"in": 5.0,  "out": 25.0},
    "claude-opus-4-5":    {"in": 5.0,  "out": 25.0},
    "claude-sonnet-4-6":  {"in": 3.0,  "out": 15.0},
    "claude-sonnet-4-5":  {"in": 3.0,  "out": 15.0},
    "claude-haiku-4-5":   {"in": 1.0,  "out": 5.0},
}

# --- Schema manifest --------------------------------------------------------
# The known-fields baseline, version-controlled here in the script (same home as
# BASE_RATES, the other "what we know how to read" table). The schema audit diffs
# what's actually on disk against this and reports the delta; SKILL.md tasks the
# agent with judging any delta. Baselined from observed data on 2026-06-14.
#
# Per key: "class" — "always" (alarm if it drops to ~0; we depend on it) or
# "optional" (legitimately absent on some records — never alarm on absence).
# "consumed" marks a field the pricing path actually reads. "driver" marks a
# field that looks billable but we do NOT yet price — surfaced as a heads-up.
_ALWAYS = {"class": "always"}
_OPTIONAL = {"class": "optional"}

SCHEMA_MANIFEST = {
    "version": 1,
    "surfaces": {
        # message.usage in transcripts
        "usage": {
            "input_tokens":                {"class": "always", "consumed": True},
            "output_tokens":               {"class": "always", "consumed": True},
            "cache_read_input_tokens":     {"class": "always", "consumed": True},
            "cache_creation_input_tokens": {"class": "always", "consumed": True},
            "cache_creation":              {"class": "always", "consumed": True},
            "service_tier":                _ALWAYS,
            "inference_geo":               _ALWAYS,
            "iterations":                  _OPTIONAL,
            "speed":                       _OPTIONAL,
            "server_tool_use": {"class": "optional", "driver": True,
                                "note": "server-side tool calls (e.g. web search); "
                                        "unpriced — watch as a billable dimension"},
            "output_tokens_details":       _OPTIONAL,
        },
        # message.usage.cache_creation.* — the 5m/1h write split we price exactly
        "usage_cache_creation": {
            "ephemeral_5m_input_tokens": {"class": "always", "consumed": True},
            "ephemeral_1h_input_tokens": {"class": "always", "consumed": True},
        },
        # ~/.claude/usage-data/session-meta/*.json
        "session_meta": {k: _ALWAYS for k in (
            "session_id", "project_path", "start_time", "duration_minutes",
            "user_message_count", "assistant_message_count", "tool_counts",
            "languages", "git_commits", "git_pushes", "input_tokens",
            "output_tokens", "first_prompt", "user_interruptions",
            "user_response_times", "tool_errors", "tool_error_categories",
            "uses_task_agent", "uses_mcp", "uses_web_search", "uses_web_fetch",
            "lines_added", "lines_removed", "files_modified", "message_hours",
            "user_message_timestamps",
        )},
        # ~/.claude/usage-data/facets/*.json
        "facets": {k: _ALWAYS for k in (
            "underlying_goal", "goal_categories", "outcome",
            "user_satisfaction_counts", "claude_helpfulness", "session_type",
            "friction_counts", "friction_detail", "primary_success",
            "brief_summary", "session_id",
        )},
    },
}

# A surface is too thin to trust for "GONE" alarms below this many records; an
# "always" field seen in fewer than ALWAYS_MIN of them is reported as vanished.
SCHEMA_MIN_SAMPLE = 20
SCHEMA_ALWAYS_MIN = 0.5

ARCHIVE_VERSION = 1


def rates_for(model: str):
    """Return per-token ($/token) rates for a model id, or None if unpriced.

    Matches exact ids first, then the longest known prefix (handles dated
    suffixes like claude-haiku-4-5-20251001).
    """
    base = BASE_RATES.get(model)
    if base is None:
        for key in sorted(BASE_RATES, key=len, reverse=True):
            if model.startswith(key):
                base = BASE_RATES[key]
                break
    if base is None:
        return None
    i, o = base["in"], base["out"]
    return {
        "in": i / 1e6,
        "out": o / 1e6,
        "cache_read": (i * 0.1) / 1e6,
        "cache_write_5m": (i * 1.25) / 1e6,
        "cache_write_1h": (i * 2.0) / 1e6,
    }


def usage_components(usage: dict):
    """Token counts from a usage object as (input, output, cache_read, w5, w1),
    where w5/w1 are 5-minute / 1-hour cache writes. When the ephemeral split is
    absent, all cache-creation is treated as a 5-minute write."""
    cc = usage.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5 = usage.get("cache_creation_input_tokens", 0) or 0
        w1 = 0
    else:
        w5 = w5 or 0
        w1 = w1 or 0
    return (
        usage.get("input_tokens", 0) or 0,
        usage.get("output_tokens", 0) or 0,
        usage.get("cache_read_input_tokens", 0) or 0,
        w5,
        w1,
    )


def cost_of(usage: dict, r: dict) -> float:
    i, o, cr, w5, w1 = usage_components(usage)
    return (
        i * r["in"]
        + o * r["out"]
        + cr * r["cache_read"]
        + w5 * r["cache_write_5m"]
        + w1 * r["cache_write_1h"]
    )


def scan(projects_dir: Path):
    """One pass over all transcripts. Returns (sessions, schema_obs).

    `sessions` is keyed by session id; each record carries per-day and per-model
    cost/token rollups (the durable, re-aggregatable grain we archive). Dedupes
    assistant turns on (message id, requestId) — resumed sessions and sidechains
    re-emit lines. `schema_obs` tallies the keys seen on `message.usage` (and its
    cache_creation sub-object) for the schema audit.
    """
    sessions: dict[str, dict] = {}
    usage_obs = collections.Counter()
    cc_obs = collections.Counter()
    obs_turns = 0
    seen = set()

    for path in projects_dir.rglob("*.jsonl"):
        project = path.parent.name
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                model = msg.get("model")
                if not isinstance(usage, dict) or not model:
                    continue

                key = (msg.get("id"), rec.get("requestId"))
                if key != (None, None) and key in seen:
                    continue
                seen.add(key)

                # Schema observation: every deduped usage row, priced or not.
                obs_turns += 1
                usage_obs.update(usage.keys())
                cc = usage.get("cache_creation")
                if isinstance(cc, dict):
                    cc_obs.update(cc.keys())

                ts = rec.get("timestamp")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                day = dt.astimezone().date().isoformat()
                sid = rec.get("sessionId") or "(unknown)"

                s = sessions.get(sid)
                if s is None:
                    s = sessions[sid] = {
                        "session_id": sid, "project": project,
                        "first_seen": ts, "last_seen": ts,
                        "turns": 0, "cost": 0.0, "tokens": 0,
                        "by_day": {}, "by_model": {}, "unpriced": {},
                    }
                if ts < s["first_seen"]:
                    s["first_seen"] = ts
                if ts > s["last_seen"]:
                    s["last_seen"] = ts

                i, o, cr, w5, w1 = usage_components(usage)
                tok = i + o + cr + w5 + w1
                r = rates_for(model)
                if r is None:
                    s["unpriced"][model] = s["unpriced"].get(model, 0) + 1
                    continue

                cost = (i * r["in"] + o * r["out"] + cr * r["cache_read"]
                        + w5 * r["cache_write_5m"] + w1 * r["cache_write_1h"])
                s["turns"] += 1
                s["cost"] += cost
                s["tokens"] += tok
                bd = s["by_day"].setdefault(day, {"cost": 0.0, "tokens": 0})
                bd["cost"] += cost
                bd["tokens"] += tok
                bm = s["by_model"].setdefault(model, {
                    "turns": 0, "cost": 0.0, "tokens": 0, "input": 0, "output": 0,
                    "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0})
                bm["turns"] += 1
                bm["cost"] += cost
                bm["tokens"] += tok
                bm["input"] += i
                bm["output"] += o
                bm["cache_read"] += cr
                bm["cache_write_5m"] += w5
                bm["cache_write_1h"] += w1

    schema_obs = {"usage": dict(usage_obs), "usage_cache_creation": dict(cc_obs),
                  "usage_turns": obs_turns}
    return sessions, schema_obs


# --- Archive (persistence) --------------------------------------------------
def default_archive_path() -> Path:
    return Path.home() / ".claude" / "claudit" / "archive.jsonl"


def load_archive(path: Path):
    """Return (header, sessions). Header carries the archive version and the
    last_collected_date that drives the once-a-day trigger. Missing/garbled
    file → an empty archive (forward-only: we never recover what's already gone)."""
    header = {"version": ARCHIVE_VERSION, "last_collected_date": None}
    sessions: dict[str, dict] = {}
    if not path.is_file():
        return header, sessions
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return header, sessions
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if idx == 0 and obj.get("kind") == "claudit-archive":
            header["version"] = obj.get("version", ARCHIVE_VERSION)
            header["last_collected_date"] = obj.get("last_collected_date")
            continue
        sid = obj.get("session_id")
        if sid:
            sessions[sid] = obj
    return header, sessions


def save_archive(path: Path, sessions: dict, today: date) -> None:
    """Write header + one session per line, atomically (tmp then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"kind": "claudit-archive", "version": ARCHIVE_VERSION,
              "last_collected_date": today.isoformat(),
              "updated_at": datetime.now().isoformat(timespec="seconds")}
    out = [json.dumps(header, separators=(",", ":"))]
    for sid in sorted(sessions):
        out.append(json.dumps(sessions[sid], separators=(",", ":"), sort_keys=True))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)


def merge_sessions(archived: dict, live: dict) -> dict:
    """Upsert: live (freshly-scanned, possibly grown) wins over archived for any
    session id present in both."""
    merged = dict(archived)
    merged.update(live)
    return merged


def sessions_to_daily(sessions: dict):
    daily_cost = defaultdict(float)
    daily_tokens = defaultdict(int)
    for s in sessions.values():
        for day, v in s.get("by_day", {}).items():
            try:
                d = date.fromisoformat(day)
            except ValueError:
                continue
            daily_cost[d] += v.get("cost", 0.0)
            daily_tokens[d] += v.get("tokens", 0)
    return daily_cost, daily_tokens


def sessions_stats(sessions: dict) -> dict:
    turns = 0
    nsess = 0
    unpriced = defaultdict(int)
    for s in sessions.values():
        turns += s.get("turns", 0)
        if s.get("turns", 0) > 0:
            nsess += 1
        for m, c in s.get("unpriced", {}).items():
            unpriced[m] += c
    return {"turns": turns, "sessions": nsess, "unpriced": dict(unpriced)}


def should_collect(header: dict, today: date, force: bool) -> bool:
    """Full path (archive write + schema audit) runs on --collect or the first
    invocation of a local day; later same-day runs render without side effects."""
    return force or header.get("last_collected_date") != today.isoformat()


# --- Schema audit -----------------------------------------------------------
def _keys_over(directory: Path, pattern: str):
    counts = collections.Counter()
    n = 0
    if not directory.is_dir():
        return dict(counts), n
    for f in directory.glob(pattern):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            n += 1
            counts.update(obj.keys())
    return dict(counts), n


def audit_schema(schema_obs: dict) -> dict:
    """Diff observed keys (transcripts + usage-data) against SCHEMA_MANIFEST.

    Emits three buckets of facts for the agent to judge: `new` (unrecognized
    keys — a possible new cost driver), `gone` ("always" fields that have
    collapsed to near-absence on a well-sampled surface), and `unpriced_drivers`
    (manifest fields flagged billable-ish that are present but we don't price).
    """
    ud = Path.home() / ".claude" / "usage-data"
    sm = _keys_over(ud / "session-meta", "*.json")
    fc = _keys_over(ud / "facets", "*.json")
    obs = {
        "usage": (schema_obs.get("usage", {}), schema_obs.get("usage_turns", 0)),
        "usage_cache_creation": (schema_obs.get("usage_cache_creation", {}),
                                 schema_obs.get("usage_turns", 0)),
        "session_meta": sm,
        "facets": fc,
    }

    new, gone, drivers = [], [], []
    for surface, spec in SCHEMA_MANIFEST["surfaces"].items():
        seen, n = obs.get(surface, ({}, 0))
        known = set(spec)
        for k, cnt in sorted(seen.items()):
            if k not in known:
                new.append({"surface": surface, "key": k, "count": cnt,
                            "pct": round(cnt / n * 100, 1) if n else 0.0})
        if n >= SCHEMA_MIN_SAMPLE:
            for k, meta in spec.items():
                if meta.get("class") == "always" and seen.get(k, 0) / n < SCHEMA_ALWAYS_MIN:
                    gone.append({"surface": surface, "key": k,
                                 "count": seen.get(k, 0), "n": n})
        for k, meta in spec.items():
            if meta.get("driver") and seen.get(k, 0) > 0:
                drivers.append({"surface": surface, "key": k,
                                "count": seen.get(k, 0), "note": meta.get("note", "")})

    return {
        "manifest_version": SCHEMA_MANIFEST["version"],
        "clean": not new and not gone,
        "new": new,
        "gone": gone,
        "unpriced_drivers": drivers,
        "samples": {s: obs.get(s, ({}, 0))[1] for s in SCHEMA_MANIFEST["surfaces"]},
    }


def read_retention(default: int = 30) -> int:
    """Claude Code's transcript-retention horizon (cleanupPeriodDays), in days.
    settings.local.json overrides settings.json; falls back to the 30-day default."""
    val = default
    for name in ("settings.json", "settings.local.json"):  # later wins
        try:
            data = json.loads((Path.home() / ".claude" / name).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        v = data.get("cleanupPeriodDays")
        if isinstance(v, (int, float)) and v > 0:
            val = int(v)
    return val


def read_config(path: Path | None) -> dict:
    """Read per-user tunables from a TOML config (signet's per-target file).

    Default path is ~/.claude/claudit.toml. A missing file → {}. TOML is read
    with stdlib `tomllib` (Python 3.11+); on an older interpreter a present
    config is skipped with a note rather than crashing — the script stays usable.
    """
    if path is None:
        path = Path.home() / ".claude" / "claudit.toml"
    if not path.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:
        v = sys.version_info
        print(f"note: {path.name} ignored — TOML config needs Python 3.11+ "
              f"(running {v.major}.{v.minor}); falling back to flags/defaults", file=sys.stderr)
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"warning: could not read {path}: {e}", file=sys.stderr)
        return {}


def window_sum(daily: dict, start: date, end: date) -> float:
    """Inclusive [start, end] sum over a date-keyed dict."""
    return sum(v for d, v in daily.items() if start <= d <= end)


def month_bounds(year: int, month: int):
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, nxt - timedelta(days=1)


def build_report(daily_cost, daily_tokens, stats, today: date, plan: float,
                 table_days: int, retention: int):
    earliest = min(daily_cost) if daily_cost else today
    available = (today - earliest).days + 1
    # How far back a window can meaningfully reach: the longer of what retention
    # keeps and what's actually on record (archive can exceed retention), capped
    # at 90 (the deepest window we show).
    horizon = min(90, max(retention, available))

    def w(start: date, end: date) -> dict:
        # `truncated`: the window reaches before the earliest day we have on
        # record, so the figure is a floor, not the full window.
        return {"cost": window_sum(daily_cost, start, end), "truncated": start < earliest}

    windows = {}
    for label, n in (("last 7 days", 7), ("last 30 days", 30),
                     ("last 60 days", 60), ("last 90 days", 90)):
        if n > horizon and n > 30:   # always keep 7/30; drop deeper windows retention can't fill
            continue
        windows[label] = w(today - timedelta(days=n - 1), today)

    cm_start, cm_end = month_bounds(today.year, today.month)
    windows[f"current month ({cm_start:%b %Y})"] = w(cm_start, today)

    lm_year, lm_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    lm_start, lm_end = month_bounds(lm_year, lm_month)
    windows[f"last month ({lm_start:%b %Y})"] = w(lm_start, lm_end)

    return {
        "generated": today.isoformat(),
        "plan_monthly": plan,
        "earliest": earliest.isoformat(),
        "retention_days": retention,
        "horizon_days": horizon,
        "windows": windows,
        "current_month": {"label": f"{cm_start:%b %Y}", "cost": window_sum(daily_cost, cm_start, today)},
        "last_month": {"label": f"{lm_start:%b %Y}", "cost": window_sum(daily_cost, lm_start, lm_end)},
        "table_days": table_days,
        # Reverse-chronological: most recent day first.
        "table": [
            {"day": (today - timedelta(days=i)).isoformat(),
             "cost": daily_cost.get(today - timedelta(days=i), 0.0),
             "tokens": daily_tokens.get(today - timedelta(days=i), 0)}
            for i in range(table_days)
        ],
        "stats": stats,
    }


def _tokens(v: float) -> str:
    """Compact token count: 1.9B / 12.3M / 940K / 512."""
    if v >= 1e9:
        return f"{v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}K"
    return str(int(v))


def hbar(frac: float, width: int) -> str:
    """A horizontal bar `width` cells wide at fraction `frac` of full, with
    sub-cell resolution via eighth-blocks (▏▎▍▌▋▊▉ → █)."""
    frac = max(0.0, min(1.0, frac))
    eighths = round(frac * width * 8)
    full, rem = divmod(eighths, 8)
    return "█" * full + (" ▏▎▍▌▋▊▉"[rem] if rem else "")


class Palette:
    """ANSI colour, gated in one place so the same code emits plain text when
    colour is off (piped output, --color never, NO_COLOR)."""

    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32",
             "yellow": "33", "blue": "94", "cyan": "36"}

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, name: str, s: str) -> str:
        if not self.enabled or not s:
            return s
        return f"\033[{self.CODES[name]}m{s}\033[0m"


def _bar_cell(frac: float, width: int, pal: "Palette", colour: str) -> str:
    """A colour-wrapped horizontal bar padded to `width` *visible* columns. The
    pad is plain and lives outside the ANSI wrapper, so colour codes (which have
    zero display width) never throw off the column alignment."""
    bar = hbar(frac, width)
    return pal(colour, bar) + " " * (width - len(bar))


def _heat(frac: float) -> str:
    """Spend intensity → green (low) · yellow (mid) · red (hot)."""
    return "red" if frac >= 0.66 else "yellow" if frac >= 0.34 else "green"


def day_table(table: list, pal: "Palette") -> list:
    """Day-by-day table (reverse-chronological): weekday/date, API-equivalent
    spend, token volume, and a spend bar heat-mapped by intensity (green→
    yellow→red) and scaled to the busiest day in the window."""
    peak_c = max((d["cost"] for d in table), default=0.0) or 1.0
    bw = 10
    n = len(table)
    lines = [f"  Last {n} days, most recent first  ·  spend bar scaled to the busiest day:"]
    lines.append(pal("dim", f"  {'Day':<10}{'API-equiv':>11}  {'tokens':>8}  {'spend':<{bw}}"))
    lines.append(pal("dim", f"  {'-' * 10}{'-' * 11}  {'-' * 8}  {'-' * bw}"))
    tot_c = tot_t = 0.0
    for d in table:
        dt = date.fromisoformat(d["day"])
        day = f"{dt:%a} {dt:%m-%d}"
        cost = f"${d['cost']:,.2f}".rjust(11)
        tok = _tokens(d["tokens"]).rjust(8)
        cfrac = d["cost"] / peak_c
        spend_bar = _bar_cell(cfrac, bw, pal, _heat(cfrac))
        lines.append(f"  {day:<10}{cost}  {tok}  {spend_bar}")
        tot_c += d["cost"]
        tot_t += d["tokens"]
    lines.append(pal("dim", f"  {'-' * 10}{'-' * 11}  {'-' * 8}  {'-' * bw}"))
    total = f"  {'total':<10}{('$' + format(tot_c, ',.2f')).rjust(11)}  {_tokens(tot_t).rjust(8)}"
    lines.append(pal("bold", total))
    return lines


def schema_lines(sc: dict, pal: "Palette") -> list:
    """Render the schema-audit section (only present on full-path runs)."""
    v = sc["manifest_version"]
    lines = []
    if sc["clean"]:
        lines.append(pal("green", f"  Schema check ✓  — manifest v{v}: all known fields present, none new."))
    else:
        lines.append(pal("yellow", f"  Schema check ⚠  — manifest v{v}: drift detected (judge each below)."))
        for d in sc["new"]:
            lines.append(pal("yellow",
                f"    + NEW  {d['surface']}.{d['key']}  ({d['count']} recs, {d['pct']}%) — unrecognized; possible new driver"))
        for d in sc["gone"]:
            lines.append(pal("yellow",
                f"    – GONE {d['surface']}.{d['key']}  ({d['count']}/{d['n']}) — was always-present"))
    for d in sc.get("unpriced_drivers", []):
        lines.append(pal("dim",
            f"    • {d['surface']}.{d['key']} present ({d['count']}) but unpriced — {d['note']}"))
    return lines


def render_text(rep: dict, pal: "Palette") -> str:
    plan = rep["plan_monthly"]
    out = []
    out.append(pal("bold", "Claude Code — API-equivalent spend"))
    out.append("(what this usage would cost pay-as-you-go; you're on a flat plan, so it's a value figure, not a bill)")
    out.append("")

    label_w = max(len(k) for k in rep["windows"])
    any_trunc = any(v["truncated"] for v in rep["windows"].values())

    def row(k):
        v = rep["windows"][k]
        mark = " *" if v["truncated"] else "  "
        return f"  {k.ljust(label_w)}   ${v['cost']:>11,.2f}{mark}"

    out.append("  " + "Window".ljust(label_w) + "   API-equivalent")
    out.append("  " + "-" * label_w + "   --------------")
    order = ["last 7 days", "last 30 days", "last 60 days", "last 90 days"]
    for k in order:
        if k in rep["windows"]:
            out.append(row(k))
    out.append("  " + " " * label_w + "   ")
    for k in rep["windows"]:
        if k not in order:
            out.append(row(k))
    if any_trunc:
        out.append(f"  * extends before the earliest day on record ({rep['earliest']}); figure is a floor.")
    out.append(f"  (showing windows up to {rep['horizon_days']}d; transcript retention is "
               f"{rep['retention_days']}d via cleanupPeriodDays — the archive carries history past that)")
    out.append("")

    cm, lm = rep["current_month"], rep["last_month"]
    out.append(f"  vs your ${plan:,.0f}/mo plan:")
    out.append(f"    {cm['label']} so far : ${cm['cost']:>10,.2f}   →  {cm['cost'] / plan:>5.1f}× the plan")
    out.append(f"    {lm['label']} (full) : ${lm['cost']:>10,.2f}   →  {lm['cost'] / plan:>5.1f}× the plan")
    out.append("")

    if rep.get("table"):
        out.extend(day_table(rep["table"], pal))
        out.append("")

    s = rep["stats"]
    out.append(f"  {s['turns']:,} assistant turns across {s['sessions']:,} sessions.")
    if s["unpriced"]:
        items = ", ".join(f"{m} ({n})" for m, n in s["unpriced"].items())
        out.append(f"  ⚠ unpriced models skipped: {items}")

    arc = rep.get("archive", {})
    if arc.get("collected"):
        out.append(pal("dim", f"  ✓ archive refreshed — {arc['sessions']:,} sessions on record "
                              f"(history since {rep['earliest']})."))
    else:
        out.append(pal("dim", f"  archive: {arc.get('sessions', 0):,} sessions on record "
                              f"(refreshes once daily; --collect to force)."))
    out.append("")

    if rep.get("schema"):
        out.extend(schema_lines(rep["schema"], pal))
        out.append("")

    out.append(pal("dim", "  Tip: pipe through a pager to scroll with colour intact — "
                          "`report.py | less -R` (-R keeps the ANSI)."))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=None,
                    help="days in the day-by-day table (default 30, capped at days of data on disk)")
    ap.add_argument("--plan", type=float, default=None, help="monthly plan price for comparison (default 200)")
    ap.add_argument("--retention", type=int, default=None,
                    help="override cleanupPeriodDays instead of reading it from settings")
    ap.add_argument("--color", choices=["auto", "always", "never"], default=None,
                    help="colourize output (default: always; auto = only at a TTY; never = off)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--projects", type=Path, default=None,
                    help="transcripts directory (default ~/.claude/projects)")
    ap.add_argument("--config", type=Path, default=None,
                    help="TOML config path (default ~/.claude/claudit.toml)")
    ap.add_argument("--archive", type=Path, default=None,
                    help="per-session archive file (default ~/.claude/claudit/archive.jsonl)")
    ap.add_argument("--collect", action="store_true",
                    help="force archive refresh + schema audit (also runs first time each day)")
    args = ap.parse_args()

    # Tunables resolve flag > config > built-in default. The config is signet's
    # per-target file, so the script stays byte-identical across machines.
    cfg = read_config(args.config)

    plan = args.plan if args.plan is not None else float(cfg.get("plan", 200.0))

    projects = args.projects
    if projects is None:
        projects = Path(cfg["projects"]).expanduser() if cfg.get("projects") \
            else Path.home() / ".claude" / "projects"

    if not projects.is_dir():
        print(f"transcripts directory not found: {projects}", file=sys.stderr)
        return 1

    # Scan live transcripts, then merge with the persisted archive. Live wins on
    # any session id present in both (a session may have grown since last run).
    live_sessions, schema_obs = scan(projects)
    arc_path = args.archive or (Path(cfg["archive"]).expanduser() if cfg.get("archive")
                                else default_archive_path())
    header, archived = load_archive(arc_path)
    merged = merge_sessions(archived, live_sessions)

    today = datetime.now().date()
    collecting = should_collect(header, today, args.collect)
    schema = None
    if collecting:
        save_archive(arc_path, merged, today)
        schema = audit_schema(schema_obs)

    daily_cost, daily_tokens = sessions_to_daily(merged)
    stats = sessions_stats(merged)
    retention = int(args.retention or cfg.get("retention") or read_retention())

    # Table length: request 30 days by default, but never show days before the
    # earliest day on record (those would be misleading $0 rows).
    earliest = min(daily_cost) if daily_cost else today
    available = (today - earliest).days + 1
    requested = int(args.days or cfg.get("days") or 30)
    table_days = max(1, min(requested, available))

    rep = build_report(daily_cost, daily_tokens, stats, today, plan, table_days, retention)
    rep["archive"] = {"collected": collecting, "sessions": len(merged), "path": str(arc_path)}
    if schema is not None:
        rep["schema"] = schema

    # Colour resolves flag > config > built-in (on by default); never for JSON.
    mode = args.color or cfg.get("color") or "always"
    if args.json or mode == "never":
        color_on = False
    elif mode == "auto":
        color_on = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    else:  # always (the default)
        # NO_COLOR is honoured as a global opt-out unless --color always is explicit.
        color_on = not ("NO_COLOR" in os.environ and args.color is None)
    pal = Palette(color_on)

    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(render_text(rep, pal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
