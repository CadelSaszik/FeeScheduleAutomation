# Options Exchange Fee Schedule Automation

Automated pipeline that monitors, extracts, diffs, and alerts on fee schedule changes across all 18 U.S. options exchanges.

## What it does

1. **Fetches** each exchange's current fee schedule (PDF or HTML) from stable public URLs
2. **Extracts** structured fee data using Claude AI — normalized to a consistent schema, with source citations and a full footnote catalog for every run
3. **Diffs** the current extraction against the prior stored version
4. **Alerts** via Microsoft Teams (and optionally email) when rates change, rows are added, or rows disappear
5. **Flags** anything the AI is uncertain about for human review, with precise citations back to the source document
6. **Analyzes** the full fee landscape cross-exchange to surface routing insights

## Supported Exchanges

| Exchange | Operator | Format |
|----------|----------|--------|
| EDGX, BZX, C2, CBOE | CBOE | PDF |
| MIAX, Pearl, Emerald, Sapphire | MIAX | PDF |
| NOM, BX, PHLX, ISE, Gemini, Mercury | Nasdaq | HTML |
| ARCA, AMEX | NYSE | PDF |
| BOX | BOX | PDF |
| MEMX | MEMX | HTML/PDF |

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

### 3. Configure exchanges

`config/exchanges.yaml` contains all 18 exchanges with their fee schedule URLs and operator family. Set `enabled: false` to skip any exchange. URLs are stable (CBOE and NYSE update in-place; Nasdaq pages are live HTML).

## Usage

```bash
# Test the full pipeline without an API key (synthetic data, real DB/diff/alert chain)
python main.py --mock --exchange edgx

# Simulate rate changes to test alert delivery
python main.py --mock --mock-jitter --exchange edgx

# Run the full pipeline for all exchanges (requires ANTHROPIC_API_KEY)
python main.py --run-now

# Run for a single exchange
python main.py --run-now --exchange edgx

# Run for multiple exchanges
python main.py --run-now --exchange edgx bzx c2 cboe

# Start the weekly scheduler (blocking — run under systemd/screen/Task Scheduler)
python main.py --schedule

# Cross-exchange fee comparison table
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

Each extracted row represents a unique combination of:

| Field | Values |
|-------|--------|
| `exchange_id` | edgx, bzx, c2, … |
| `ticker_class` | Penny, Non-Penny, or specific class |
| `sec_type` | OPT (single-leg), MLEG (complex) |
| `account_type` | CUST, PCUST |
| `trade_type` | Electronic, PI, Solicitation |
| `liq_code` | Exchange liquidity code where applicable |
| `make_rate` | Per-contract dollar amount (positive = rebate) |
| `take_rate` | Per-contract dollar amount (negative = fee) |
| `auction_init_rate` | AIM/PIP/PRIME initiating side |
| `auction_resp_rate` | AIM/PIP/PRIME responding side |
| `breakup_rate` | Fee when auction does not execute |
| `source_page` | Page or section reference in the fee schedule |
| `source_section` | Exact table/heading the rate was taken from |
| `footnote_refs` | Footnote IDs that qualify this row, e.g. `["1","*"]` |
| `confidence` | high / medium / low |
| `confidence_reason` | Explanation when confidence is below high |
| `notes` | Other qualifications not captured in structured fields |

## Source citations and footnote handling

Every extraction run produces three outputs:

**Footnote catalog** — Claude reads the entire document before extracting any rates and catalogs every footnote verbatim, with its location in the document. Stored in the `footnotes` table and viewable with `--footnotes --exchange <id>`.

**Row citations** — every fee row records exactly where in the document the number came from (`source_page`, `source_section`) and which footnotes apply to it (`footnote_refs`).

**Confidence flags** — rows where the AI is uncertain (tiered structures, conditional footnotes, ambiguous table layouts) are marked `medium` or `low` confidence with a plain-English explanation. These fire a separate Teams alert and appear in `--review` output.

The AI is explicitly instructed to use the **base table rate** and document footnote modifications separately — it never silently applies a footnote adjustment without recording it. A low-confidence row is always better than a silently wrong number.

## Data storage

SQLite database at `data/db/fees.db` (configurable via `DB_PATH` env var). Schema migrates automatically on startup — no manual steps needed when columns are added.

Tables:
- `fee_rows` — all extracted rows with citations, confidence, and footnote references
- `footnotes` — every footnote extracted from each run
- `extraction_flags` — AI-flagged issues (e.g. ambiguous tables, conflicting rates)
- `run_history` — one record per extraction run with status and token counts
- `raw_files` — metadata for downloaded fee schedule files

Raw downloaded files are stored under `data/raw/<exchange_id>/` with UTC timestamps.

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

All costs are for the Anthropic Claude API (`claude-sonnet-4-20250514`). Pricing as of mid-2025: **$3.00 / 1M input tokens, $15.00 / 1M output tokens**.

| Scenario | Input tokens | Output tokens | Est. cost |
|----------|-------------|---------------|-----------|
| Single exchange (e.g. EDGX) | ~30k–60k | ~2k–4k | ~$0.12–$0.24 |
| Full 18-exchange run | ~500k–900k | ~30k–60k | ~$2.00–$3.60 |
| Cross-exchange insight pass | ~40k–60k | ~1k–2k | ~$0.14–$0.20 |
| **Full weekly run (all-in)** | **~600k–1M** | **~35k–65k** | **~$2.25–$4.00** |

**Annual cost at weekly cadence:** ~$115–$210/year.

Token usage varies by exchange — CBOE PDFs tend to be dense, Nasdaq HTML pages are longer but more structured. The `--history` command shows actual token counts per run once you start running.

Token usage is logged per run in `run_history.input_tokens` and `run_history.output_tokens`.

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
3. Add an operator-specific extraction prompt to `src/extractor/prompts.py`
4. Run `python main.py --run-now --exchange <id> --debug` to validate, then check `--review` and `--footnotes` output

## Validating extraction accuracy

The recommended validation workflow for a new exchange:

1. Run `python main.py --run-now --exchange edgx`
2. Run `python main.py --report --filter-trade-type Electronic` and compare against the hand-built spreadsheet
3. Run `python main.py --footnotes --exchange edgx` to confirm footnotes were captured
4. Run `python main.py --review` to check for any low-confidence rows and verify them against the source PDF
5. If rates are wrong, check `data/raw/edgx/` for the downloaded PDF and compare against the extracted text

## Known limitations

- Nasdaq fee pages may require JS rendering for some content — the HTML fetcher captures server-rendered HTML. A Playwright-based fetcher can be added if needed.
- PHLX/ISE have complex tiered structures; the system defaults to Tier 1 and notes it.
- First run produces no diff (nothing to compare against). The second run produces the first meaningful change report.
- The `--mock` command generates synthetic data only — use it to test pipeline mechanics, not to check rates.
