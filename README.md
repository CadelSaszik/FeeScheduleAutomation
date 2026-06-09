# Options Exchange Fee Schedule Automation

Automated pipeline that monitors, extracts, diffs, and alerts on fee schedule changes across all 18 U.S. options exchanges.

## What it does

1. **Fetches** each exchange's current fee schedule (CSV, PDF, or HTML) from stable public URLs
2. **Extracts** structured fee data using Claude AI — normalized to a consistent schema, with a mandatory full footnote catalog for every run
3. **Diffs** the current extraction against the prior stored version — keyed on `(exchange_id, ticker_class, sec_type, account_type, trade_type, liq_code)`
4. **Alerts** via Microsoft Teams (and optionally email) when rates change, rows are added, or rows disappear
5. **Flags** anything the AI is uncertain about for human review, with precise citations back to the source document
6. **Analyzes** the full fee landscape cross-exchange to surface routing insights

## Supported Exchanges

| Exchange | Operator | Format | Status |
|----------|----------|--------|--------|
| EDGX, BZX, C2, CBOE | CBOE | CSV (via `?csv=true` endpoint) | Working |
| MIAX, Pearl, Emerald, Sapphire | MIAX | PDF (miaxglobal.com) | URLs updated June 2026 |
| NOM, BX, PHLX, ISE, Gemini, Mercury | Nasdaq | HTML (JS-rendered — see limitations) | Fetch works; content needs Playwright |
| ARCA, AMEX | NYSE | PDF | AMEX working; ARCA URL updated |
| BOX | BOX | PDF (boxexchange.com) | URL updated June 2026 |
| MEMX | MEMX | HTML → CSV (dynamic link) | URL updated June 2026 |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Requires Python 3.11+.

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — set at minimum ANTHROPIC_API_KEY for real runs.
# TEAMS_WEBHOOK_URL and email settings are optional.
```

### 3. Validate endpoints before spending API tokens

```bash
python main.py --preflight              # check all 18 exchanges
python main.py --preflight --exchange edgx  # single exchange
```

Preflight checks HTTP reachability, content size, and whether the extracted text looks like a real fee schedule. It detects JS-rendered login walls (Nasdaq) and warns on suspiciously small responses. **Always run preflight before `--run-now`.**

## Usage

```bash
# Run the full pipeline for all exchanges (requires ANTHROPIC_API_KEY)
python main.py --run-now

# Run for a single exchange
python main.py --run-now --exchange edgx

# Run for multiple exchanges
python main.py --run-now --exchange edgx bzx c2 cboe

# Test the full pipeline without an API key (synthetic data, real DB/diff/alert chain)
python main.py --mock --exchange edgx

# Simulate rate changes to test alert delivery
python main.py --mock --mock-jitter --exchange edgx

# Start the weekly scheduler (blocking — run under systemd/screen/Task Scheduler)
python main.py --schedule

# Cross-exchange fee comparison table (with confidence and footnote indicators)
python main.py --report

# Filter report to one trade type
python main.py --report --filter-trade-type Electronic

# Show rows and AI flags that need human review (with source citations)
python main.py --review

# Show all footnotes extracted from the latest EDGX run
python main.py --footnotes --exchange edgx

# Show recent run history
python main.py --history

# Verbose logging
python main.py --run-now --debug
```

## Output schema

Each extracted row represents a unique fee entry from the source document:

| Field | Values |
|-------|--------|
| `exchange_id` | edgx, bzx, c2, … |
| `ticker_class` | Penny, Non-Penny, or specific class |
| `sec_type` | OPT (single-leg), MLEG (complex) |
| `account_type` | CUST, PCUST |
| `trade_type` | Electronic, PI, Solicitation |
| `liq_code` | Exchange liquidity code — **required for CBOE CSV; always populated** |
| `make_rate` | Per-contract dollar amount (positive = rebate) |
| `take_rate` | Per-contract dollar amount (negative = fee) |
| `auction_init_rate` | AIM/PIP/M-PIM initiating side (CUST) |
| `auction_resp_rate` | AIM/PIP/M-PIM responding side |
| `breakup_rate` | Fee when auction does not execute |
| `source_page` | Page or section reference in the fee schedule |
| `source_section` | Exact table/heading the rate was taken from |
| `footnote_refs` | Footnote IDs that apply to this row, e.g. `["1","*"]` |
| `confidence` | high / medium / low |
| `confidence_reason` | Explanation when confidence is below high (required for medium/low) |
| `notes` | Footnote-driven conditions, tier information, caveats |

### Rate model for PDF/HTML exchanges

For exchanges that present Maker and Taker in the same table row, a single output row carries both `make_rate` and `take_rate`. For PI auctions, `auction_init_rate` and `breakup_rate` are combined on one row per participant type. This avoids key collisions and keeps the diff engine clean.

### CBOE CSV

CBOE is fetched via the `?csv=true` endpoint — one row per CBOE liquidity code (CA, NC, BC, etc.). `liq_code` is **always required** for CBOE rows. All CBOE rows are marked **medium confidence** because the CSV export strips footnotes from the underlying fee schedule.

## Footnote handling

Every extraction run is literally two separate API calls:

**Pass 1 — Footnote catalog (dedicated API call).**
The first call asks Claude for footnotes only, using a forced tool call (`record_footnotes`). The model cannot skip this step or defer it — it must call the tool with its complete footnote list before Pass 2 starts. Footnotes are stored in the `footnotes` table and viewable with `--footnotes --exchange <id>`.

Exception: CBOE CSV exchanges skip Pass 1 entirely. The `?csv=true` endpoint strips all footnote text, so there is nothing to catalog. All CBOE rows carry medium confidence as a result.

**Pass 2 — Row extraction with confirmed footnotes.**
The second call supplies the complete footnote list from Pass 1 directly in the prompt, then forces a `record_fee_rows` tool call. Claude must check each confirmed footnote against each row — not as an instruction, but because the footnotes are sitting right above the task. Applicable footnotes are recorded in `footnote_refs`. An empty `footnote_refs` is only acceptable when Claude confirms no listed footnote affects that row.

**Schema enforcement via tool use.**
Both passes use `tool_choice: {"type": "tool"}` which forces a structured tool call matching the exact JSON schema. The API rejects malformed output before it reaches the application — no regex JSON parsing.

**Confidence rules — high is the exception.**

| Level | When | What it means |
|-------|------|---------------|
| **high** | Rate clearly stated; source cited; applicable footnotes verified and recorded; footnotes don't substantially modify the rate | Fully sourced, all footnotes checked |
| **medium** | A footnote applies (even informational); citation incomplete; mapping required interpretation; CBOE CSV always | Use with care; check `--review` |
| **low** | Footnote substantially changes effective rate; conditional waiver; rate inferred; conflicting notes | Human review required before using |

**Post-extraction validators** flag issues the prompt cannot always catch:
- Duplicate rows (same key returned twice) → warning flag
- Null `liq_code` on a CSV extraction → error flag
- High-confidence rows with empty `footnote_refs` when the document has footnotes → warning flag

## Report output

`--report` shows two additional columns:
- **Cf**: blank = high confidence, `?` = medium, `!` = low
- **FnRefs**: footnote IDs that qualify the rate for that row

## Data storage

SQLite database at `data/db/fees.db` (configurable via `DB_PATH` env var). Schema migrates automatically on startup — no manual steps needed when columns are added.

Tables:
- `fee_rows` — all extracted rows with citations, confidence, and footnote references
- `footnotes` — every footnote extracted from each run (empty for CBOE CSV)
- `extraction_flags` — AI-flagged issues + post-extraction validation flags
- `run_history` — one record per extraction run with status and token counts
- `raw_files` — metadata for downloaded fee schedule files

Raw downloaded files are stored under `data/raw/<exchange_id>/` with UTC timestamps.

## Testing

```bash
# Run full test suite (no API key required — all Anthropic calls are mocked)
python -m pytest tests/

# With coverage report
python -m pytest tests/ --cov=src --cov-report=term-missing

# Specific test file
python -m pytest tests/test_extractor_footnotes.py -v
```

### Test suite overview

| File | What it covers |
|------|---------------|
| `test_extractor_parsing.py` | All parsing functions (`_parse_rows`, `_parse_footnotes`, `_extract_json`, `_to_rate`), dedup, CSV validation, footnote coverage validator, `FeeRow` helpers |
| `test_extractor_footnotes.py` | Integration tests using mocked Claude responses for four realistic footnote scenarios: CBOE CSV, MIAX volume-conditional rebates, NYSE cascading/multi-level footnotes, BOX conflicting footnotes and PFOF waivers |
| `test_diff_engine.py` | All diff scenarios: no changes, added/removed/modified rows, null↔value transitions, floating-point stability, key normalisation, delta calculation |
| `test_db.py` | Database round-trips: schema creation, migrations, `save_rows`/`get_latest_rows`, footnote and flag storage, run history, `get_review_needed` |
| `test_prompts.py` | Prompt quality: all operators have prompts, CBOE hard requirements (liq_code, medium confidence, one-row-per-code), confidence rule tightness, footnote pass mandatory instruction, rate extraction rules |

### Footnote test philosophy

The footnote integration tests in `test_extractor_footnotes.py` are written at the complexity level of real fee schedules. Each scenario exercises a different class of footnote difficulty:

- **CBOE CSV**: No footnotes in the CSV — tests verify every row has medium confidence, correct liq_code, no invented rates, no fabricated breakup fees
- **MIAX volume-conditional**: Maker rebate applies only above 1M ADV threshold; taker fee subject to per-execution cap — tests verify base rate preserved, both footnotes referenced, confidence=medium
- **NYSE cascading**: Seven distinct footnotes (asterisks, double-asterisks, daggers, lettered notes) with program qualifications and small-order caps — tests verify all seven catalogued, the correct ones linked to each row, firm-side fees excluded
- **BOX conflicting**: Notes (i) and (ii) literally contradict each other for Non-Penny remove — tests verify conflict flag raised, both notes referenced, confidence=low, base table rate preserved rather than silently adjusted

## Alerts

### Microsoft Teams

Set `TEAMS_WEBHOOK_URL` in `.env`.

**Setup:** In your Teams channel → `···` → Connectors → Incoming Webhook → Configure → copy URL.

The bot posts Adaptive Cards with:
- A rate-change card for each modified exchange (old → new with delta)
- A `⚠️ items need review` card when any rows come back below high confidence
- A run summary across all exchanges at the end of a full run

### Email (optional)

Set `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_SMTP_HOST`, etc. in `.env`. Works with Office 365 or any SMTP server.

## API cost estimate

All costs are for `claude-sonnet-4-20250514`. Pricing as of mid-2025:

| Token type | Rate |
|---|---|
| Input (regular) | $3.00 / 1M |
| Input (cache write, 25% premium) | $3.75 / 1M |
| Input (cache read, 90% discount) | $0.30 / 1M |
| Output | $15.00 / 1M |

### Two-pass extraction with prompt caching

Every non-CBOE exchange runs two API calls:
- **Pass 1** (footnote catalog): system prompt + full fee document → cache written; footnotes JSON output (~500–1k tokens)
- **Pass 2** (row extraction): same system prompt + same fee document → **cache hit on both** (~90% cost reduction on those tokens); rows JSON output (~2k–5k tokens)

CBOE CSV exchanges skip Pass 1 entirely (the CSV has no footnote text), so they use a single API call.

| Scenario | Notes | Est. cost |
|----------|-------|-----------|
| CBOE exchange (CSV, single) | Pass 1 skipped; ~30k input, ~2k output | ~$0.12–$0.18 |
| PDF/HTML exchange (single) | Pass 1 fresh + Pass 2 cached; ~40k input each | ~$0.18–$0.28 |
| Full 18-exchange run | 4 CBOE (1 pass each) + 14 PDF/HTML (2 passes, cached) | ~$2.80–$4.50 |
| Cross-exchange insight pass | Single pass, ~40k–60k input | ~$0.14–$0.20 |
| **Full weekly run (all-in)** | | **~$3.00–$4.75** |

**Annual cost at weekly cadence:** ~$155–$245/year.

Pass 1 output cap is 4,096 tokens (footnotes only). Pass 2 is capped at 16,384 tokens to handle large fee schedules (CBOE main: 115 CSV rows; AMEX PDF: 1,593 text lines).

**Cost compared to single-pass (old):** The two-pass approach costs roughly 15–25% more per PDF/HTML exchange due to the cache-write premium on Pass 1. The cache discount on Pass 2 partially offsets this. CBOE costs are unchanged (Pass 1 skipped). The accuracy improvement from guaranteed footnote extraction before row extraction is the primary motivation.

## Scheduling

### Windows Task Scheduler

Create a task that runs weekly (e.g. Monday 6:00 AM):
```
Program: python
Arguments: "C:\path\to\FeeScheduleAutomation\main.py" --run-now
Start in: C:\path\to\FeeScheduleAutomation
```
Or use the built-in scheduler (blocking process):
```bash
python main.py --schedule
```

### Linux systemd

```ini
# /etc/systemd/system/fee-automation.service
[Unit]
Description=Options Fee Schedule Automation

[Service]
Type=simple
WorkingDirectory=/opt/fee-automation
ExecStart=/opt/fee-automation/.venv/bin/python main.py --schedule
Restart=on-failure
EnvironmentFile=/opt/fee-automation/.env

[Install]
WantedBy=multi-user.target
```

## Adding a new exchange

1. Add an entry to `config/exchanges.yaml` with `operator` set to one of: `cboe`, `nasdaq`, `nyse`, `miax`, `box`, `memx`
2. If it's a new operator family, create `src/fetcher/<operator>.py` implementing `BaseFetcher.extract_text()` and add it to `src/fetcher/__init__.py`
3. Add an operator-specific extraction prompt to `src/extractor/prompts.py` — include footnote type descriptions specific to that exchange's format
4. Run `python main.py --preflight --exchange <id>` to verify the URL works
5. Run `python main.py --run-now --exchange <id> --debug` to validate extraction
6. Check `--review` for low-confidence rows, `--footnotes --exchange <id>` for captured footnotes

## Validating extraction accuracy

Recommended workflow for a new or re-validated exchange:

1. `python main.py --preflight --exchange edgx` — confirm endpoint healthy
2. `python main.py --run-now --exchange edgx` — run extraction
3. `python main.py --report` — scan the `Cf` column for `?` and `!` markers; `FnRefs` shows which footnotes apply
4. `python main.py --footnotes --exchange edgx` — confirm footnotes were captured (non-CSV exchanges)
5. `python main.py --review` — inspect low/medium-confidence rows and their citations
6. Compare against the hand-built spreadsheet or raw downloaded file in `data/raw/edgx/`

## Manual override files

If an exchange blocks automated fetching (403) or publishes dated PDFs that require manual updates, you can drop a file at:

```
data/raw/<exchange_id>/manual.pdf    # for PDF exchanges
data/raw/<exchange_id>/manual.html   # for HTML exchanges
data/raw/<exchange_id>/manual.csv    # for CSV exchanges
```

The fetcher checks for a `manual.*` file **before** attempting any HTTP request. If found, it uses that file and skips the network call entirely. This works for any exchange — MIAX with dated URLs, blocked CDNs, or any endpoint that requires a browser session. Delete the manual file when you want the fetcher to resume normal HTTP fetching.

## Known limitations

- **Nasdaq (6 exchanges)**: The HTML pages at `listingcenter.nasdaq.com` are JS-rendered SPAs. The server-side HTML fetch returns navigation chrome, not fee tables. Preflight will flag these as a JS login wall. A Playwright-based fetcher is needed for actual content extraction.
- **MIAX URL maintenance**: MIAX fee schedule PDFs have dated filenames (e.g. `MIAX_Options_Fee_Schedule_04012026.pdf`). Update `config/exchanges.yaml` when new PDFs are published. Check `miaxglobal.com/markets/us-options/all-options-exchanges/fees-archive` for the current link.
- **BOX URL maintenance**: BOX fee schedule PDFs similarly include the effective date. Check `boxexchange.com` for updates.
- **CBOE footnotes**: CBOE's CSV export does not include the footnotes from the underlying fee schedule. All CBOE rows are marked medium confidence as a result. To review CBOE footnotes, visit the fee schedule landing page directly.
- **PHLX/ISE tiered structures**: The system defaults to Tier 1 rates and notes it in the `notes` field.
- **First run produces no diff**: Nothing to compare against on the first run. The second run produces the first meaningful change report.
- **`--mock` generates synthetic data only**: Use it to test pipeline mechanics, not to check rates.
