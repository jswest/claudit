---
name: claudit
description: Report what your Claude Code usage would have cost on the pay-as-you-go API. Walks every session transcript under ~/.claude/projects, prices each turn at current Anthropic rates with exact cache-tier accounting, and shows API-equivalent cost across time windows (7/30/60/90 days, current + last calendar month) plus a day-by-day table (last 30 days, with a spend bar) and a value comparison against your flat subscription. Use when asked "what would Claude Code cost on the API", "how much am I saving", "my CC spend", or for a usage/cost report.
---

# claudit

You're on a flat Claude subscription, so this is a **hypothetical** "what the same
work would cost à la carte" figure — a subscription-value report, not a bill.

## How to run

```bash
python3 ~/.claude/skills/claudit/report.py
```

Print the script's output to the user as-is (it's already formatted). Flags:

- `--days N` — rows in the day-by-day table (default 30; capped at the days of data on disk).
- `--plan PRICE` — monthly subscription price for the value comparison (default 200, i.e. Max 20×).
- `--retention N` — override `cleanupPeriodDays` instead of reading it from settings.
- `--color auto|always|never` — colourize output (default **always**). `auto` = on only when
  stdout is a terminal; `never` = off. `NO_COLOR` in the environment turns colour off unless
  `--color always` is passed explicitly. Note: colour is on even when piped, so output relayed
  verbatim into a chat carries ANSI escapes — pass `--color never` (or `auto`) if you want it plain.
- `--json` — machine-readable output instead of the text report.
- `--projects DIR` — override the transcripts directory.
- `--config PATH` — TOML config path (default `~/.claude/claudit.toml`).
- `--archive PATH` — per-session archive file (default `~/.claude/claudit/archive.jsonl`).
- `--collect` — force the full path (refresh archive + run the schema audit) even if already
  done today. The full path also runs **automatically on the first invocation of each local day**;
  later same-day runs just render (fresh numbers, no archive write, no audit).

## Config (per-user defaults)

`plan`, `retention`, `projects`, `days`, and `color` resolve as **CLI flag > config file >
built-in default**, so a different user (different plan) just drops a TOML file
and never touches the script:

```bash
cp ~/.claude/skills/claudit/claudit.toml.example ~/.claude/claudit.toml
# then edit `plan` to your subscription price
```

The config lives *outside* the skill directory by design (signet's per-target
config convention) — `report.py` stays byte-identical across machines while each
person keeps their own `plan`. Read with stdlib `tomllib` (Python 3.11+); on an
older interpreter a present config is skipped with a note, never a crash. No YAML —
that would mean a non-stdlib dependency (`PyYAML`), which this skill avoids.

## What it does

- Reads `~/.claude/projects/**/*.jsonl` (every project's session transcripts).
- Prices each assistant turn from its `message.usage`: input, output, cache reads,
  and cache writes split into 5-minute vs 1-hour tiers (the transcripts carry the
  `ephemeral_5m` / `ephemeral_1h` breakdown, so cache cost is exact, not estimated).
- Dedupes turns on `(message.id, requestId)` — resumed sessions and sidechains
  re-emit the same line, so this avoids double-counting.
- Buckets by **local** calendar day, then aggregates the windows.
- Renders a **day-by-day table** (default last 30 days, most-recent-first): per-day
  API-equivalent spend, token volume, and a spend bar heat-mapped green→yellow→red by
  intensity and scaled to the busiest day in the window (eighth-block sub-cell
  resolution). Colour is **on by default** (`--color`/`NO_COLOR`/`--color never` to disable);
  with colour off the table still reads cleanly as plain text.
- **Retention-aware.** Reads `cleanupPeriodDays` from `~/.claude/settings.json`
  (and `settings.local.json`, default 30). The horizon is the longer of retention
  vs. history actually on record (the archive can exceed retention), capped at 90.
  Windows reaching before the earliest day on record are marked `*` (a floor, not
  the full window).
- **Persists + audits** on the first run each day — see the two sections below.

## Persistence (the rolling archive)

Claude Code prunes transcripts after `cleanupPeriodDays` (≈30), so cost history
would evaporate. To beat that, the **full path** upserts a per-session record into
`~/.claude/claudit/archive.jsonl` (JSONL, one session per line, keyed by
`session_id`, last-write-wins). Each record keeps per-day and per-model cost/token
rollups — small enough to keep forever, granular enough to re-aggregate later.

Every run renders from **archive ∪ live transcripts**, so the 60/90-day windows
fill in over time instead of capping at retention. Forward-only: it can't recover
sessions already pruned, so it must collect **at least once every ~30 days** or
that tail is lost (the daily auto-trigger makes this a non-issue with normal use).
The archive header carries its own version (`ARCHIVE_VERSION`); the format is
additive-only, so old rows stay readable as fields are added.

## Schema check — do this every run

On the full path the script also **audits the log schema**: it diffs the keys
actually present in transcripts (`message.usage`) and usage-data
(`session-meta`, `facets`) against `SCHEMA_MANIFEST` in `report.py`, and prints a
**Schema check** line. This exists because Anthropic can change session logging —
adding a new cost driver, renaming a field we price, dropping one we rely on.

- **`Schema check ✓`** — nothing to do; report as-is.
- **`Schema check ⚠`** — *do not ignore it.* Treat each delta as a task and report
  back to the user with a recommendation:
  - **`+ NEW <surface>.<key>`** — an unrecognized field. Judge whether it's a new
    **billable dimension** (if so it likely belongs in `BASE_RATES`/`cost_of`) or
    just metadata. A new `usage.*` token field is the high-stakes case.
  - **`– GONE <surface>.<key>`** — a field we treated as always-present has
    vanished (a rename or removal upstream). If we *consume* it, a number in the
    report may now be silently wrong — say so.
  - **`• <key> … unpriced`** — a known-but-unpriced driver (e.g. `server_tool_use`)
    is present. Flag if it's grown enough to matter.
  When a delta is confirmed and incorporated, update `SCHEMA_MANIFEST` (bump its
  `version`) and the collector/pricing, so the baseline tracks reality.

## Pricing (per MTok, baked into the script)

Cache tiers derive from input: read ≈ 0.1×, 5-min write ≈ 1.25×, 1-hour write ≈ 2×.

| Model | Input | Output |
|---|---|---|
| claude-fable-5 | $10 | $50 |
| claude-opus-4-8 / 4-7 / 4-6 / 4-5 | $5 | $25 |
| claude-sonnet-4-6 / 4-5 | $3 | $15 |
| claude-haiku-4-5 | $1 | $5 |

`BASE_RATES` in `report.py` is the single source of truth — update it when API
prices change or new models appear. Unpriced models (e.g. `<synthetic>`) are
skipped and reported as a warning so nothing is silently zeroed.
