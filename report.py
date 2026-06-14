#!/usr/bin/env python3
"""report-spending — what your Claude Code usage would have cost on the pay-as-you-go API.

Walks every session transcript under ~/.claude/projects/**/*.jsonl, prices each
assistant turn against current Anthropic API rates (exact cache-tier accounting),
and reports the API-equivalent cost across a set of time windows plus a daily
chart. You're on a flat subscription, so this is a hypothetical "à la carte"
figure — a subscription-value report, not a bill.

Usage:
    report.py [--days N] [--plan PRICE] [--retention N] [--json]
              [--projects DIR] [--config PATH]

    --days N       Number of days in the daily chart (default: auto from retention).
    --plan PRICE   Monthly subscription price for the value comparison (default 200).
    --retention N  Override cleanupPeriodDays instead of reading it from settings.
    --json         Emit machine-readable JSON instead of the text report.
    --projects DIR Override the transcripts directory (default ~/.claude/projects).
    --config PATH  TOML config with per-user defaults (default ~/.claude/report-spending.toml).

Tunables (plan, retention, projects, days) resolve as: CLI flag > config file >
built-in default. The config is signet's per-target file — the script itself stays
byte-identical across machines; your plan price lives in the TOML, not the code.
"""
from __future__ import annotations

import argparse
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


def cost_of(usage: dict, r: dict) -> float:
    cc = usage.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        # No split available — treat all cache-creation as 5-minute writes.
        w5 = usage.get("cache_creation_input_tokens", 0) or 0
        w1 = 0
    else:
        w5 = w5 or 0
        w1 = w1 or 0
    return (
        (usage.get("input_tokens", 0) or 0) * r["in"]
        + (usage.get("output_tokens", 0) or 0) * r["out"]
        + (usage.get("cache_read_input_tokens", 0) or 0) * r["cache_read"]
        + w5 * r["cache_write_5m"]
        + w1 * r["cache_write_1h"]
    )


def scan(projects_dir: Path):
    """One pass over all transcripts. Returns (daily_cost, daily_tokens, stats).

    daily_cost / daily_tokens are keyed by local date. Dedupes assistant turns
    on (message id, requestId) — resumed sessions and sidechains re-emit lines.
    """
    daily_cost = defaultdict(float)
    daily_tokens = defaultdict(int)
    seen = set()
    unpriced = defaultdict(int)  # model -> turns
    turns = 0
    sessions = set()

    for path in projects_dir.rglob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
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

                    ts = rec.get("timestamp")
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    local_day = dt.astimezone().date()

                    r = rates_for(model)
                    tok = (
                        (usage.get("input_tokens", 0) or 0)
                        + (usage.get("output_tokens", 0) or 0)
                        + (usage.get("cache_read_input_tokens", 0) or 0)
                        + (usage.get("cache_creation_input_tokens", 0) or 0)
                    )
                    if r is None:
                        unpriced[model] += 1
                        continue
                    turns += 1
                    sessions.add(rec.get("sessionId"))
                    daily_cost[local_day] += cost_of(usage, r)
                    daily_tokens[local_day] += tok
        except OSError:
            continue

    stats = {"turns": turns, "sessions": len(sessions - {None}), "unpriced": dict(unpriced)}
    return daily_cost, daily_tokens, stats


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

    Default path is ~/.claude/report-spending.toml. A missing file → {}. TOML is
    read with stdlib `tomllib` (Python 3.11+); on an older interpreter a present
    config is skipped with a note rather than crashing — the script stays usable.
    """
    if path is None:
        path = Path.home() / ".claude" / "report-spending.toml"
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
    # keeps and what's actually on disk, capped at 90 (the deepest window we show).
    horizon = min(90, max(retention, available))

    def w(start: date, end: date) -> dict:
        # `truncated`: the window reaches before any transcript we still have on
        # disk, so the figure is a floor, not the full window.
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
        out.append(f"  * extends before your earliest transcript ({rep['earliest']}); figure is a floor.")
    out.append(f"  (showing windows up to {rep['horizon_days']}d; transcript retention is "
               f"{rep['retention_days']}d via cleanupPeriodDays)")
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
                    help="TOML config path (default ~/.claude/report-spending.toml)")
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

    daily_cost, daily_tokens, stats = scan(projects)
    today = datetime.now().date()
    retention = int(args.retention or cfg.get("retention") or read_retention())

    # Table length: request 30 days by default, but never show days before the
    # earliest transcript on disk (those would be misleading $0 rows).
    earliest = min(daily_cost) if daily_cost else today
    available = (today - earliest).days + 1
    requested = int(args.days or cfg.get("days") or 30)
    table_days = max(1, min(requested, available))

    rep = build_report(daily_cost, daily_tokens, stats, today, plan, table_days, retention)

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
