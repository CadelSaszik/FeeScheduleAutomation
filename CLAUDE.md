# FeeScheduleAutomation — Claude Context

## What this project does
Automated pipeline that monitors all 18 U.S. options exchange fee schedules weekly.
Fetches → AI-extracts → diffs against prior run → alerts via Teams + email.
Owned by Watershed Technology LLC. Contact: cadel@watershedtech.us

## Project status (as of 2026-06-04)
- CBOE (edgx, bzx, c2, cboe): **working** — uses CSV export endpoint, not PDF
- Other 15 exchanges: URLs configured, not yet validated
- Run `python main.py --preflight` before every `--run-now` to verify endpoints first
- Teams webhook and email SMTP not yet configured in .env

## Key commands
```bash
python main.py --preflight              # validate all endpoints, no API calls
python main.py --preflight --exchange edgx
python main.py --run-now --exchange edgx   # single exchange real run
python main.py --run-now               # all 18 exchanges
python main.py --mock --exchange edgx  # pipeline test, no API key needed
python main.py --report                # cross-exchange fee table from DB
python main.py --review                # low-confidence rows + flags needing human check
python main.py --footnotes --exchange edgx
python main.py --history
```

## Architecture
```
src/
  fetcher/       — per-operator HTTP fetchers (cboe uses CSV endpoint; others PDF/HTML)
  extractor/     — Claude AI extraction (claude.py) + mock (mock.py) + prompts (prompts.py)
  persistence/   — SQLite via db.py; schema auto-migrates on startup
  diff/          — row-keyed diff engine producing DiffReport
  alerts/        — Teams (Adaptive Cards webhook) + email (SMTP)
  insights/      — cross-exchange Claude analysis
  pipeline.py    — orchestrates fetch→extract→diff→alert for one or all exchanges
config/
  exchanges.yaml — all 18 exchanges with URLs, operator, enabled flag
data/
  db/fees.db     — SQLite (gitignored)
  raw/<id>/      — downloaded fee schedules (gitignored)
  raw/<id>/manual.pdf  — drop here to bypass HTTP fetch for a blocked exchange
```

## CBOE specifics (most important)
CBOE changed their CDN path structure. Their PDFs now live at date-stamped URLs
that change with every update. The fetcher instead hits:
  `https://www.cboe.com/us/options/membership/fee_schedule/<exchange>/?csv=true&feedate=YYYY-MM-DD`

Landing page slugs: edgx=edgx, bzx=bzx, c2=ctwo, cboe=cone

The CSV returns Code, Description, Fee (dollars/contract). Claude maps codes like
"CA = Customer Add Penny" → CUST / Penny / Electronic / make_rate.

## Extraction output schema
Every row: exchange_id, ticker_class (Penny/Non-Penny), sec_type (OPT/MLEG),
account_type (CUST/PCUST), trade_type (Electronic/PI/Solicitation), liq_code,
make_rate, take_rate, auction_init_rate, auction_resp_rate, breakup_rate,
source_page, source_section, footnote_refs (JSON array), confidence (high/medium/low),
confidence_reason, notes.

Rates: positive = rebate, negative = fee. Always 2dp. Null means absent, not zero.

## DB schema migration
_init_schema() + _migrate_schema() in db.py run on every startup.
Add new columns to the migrations dict in _migrate_schema() — never delete the DB manually.

## Known issues / next steps
- Nasdaq HTML pages may need JS rendering (Playwright) if preflight shows empty content
- MIAX/NYSE/BOX/MEMX URLs not yet preflight-tested
- Teams webhook URL placeholder in .env needs real value from Teams channel setup
- Email SMTP not configured
- Validate EDGX extraction against the hand-built spreadsheet (U_S__Options_Exchange_Microstructure_Overview.xlsx)

## mulch — expertise across sessions
mulch-cli is installed globally (`@os-eco/mulch-cli`). Requires bun.exe on PATH:
`$env:PATH = "$env:APPDATA\npm\node_modules\bun\bin;$env:PATH"` (already set permanently in user PATH)

Domains in this project: `exchanges` `fetching` `extraction` `pipeline` `failures`

```bash
ml prime                    # dump all expertise as AI-optimized context — run at session start
ml query exchanges          # browse a specific domain
ml query failures           # check known failure patterns before debugging
ml record failures --type failure --description "..." --resolution "..."
ml record exchanges --type convention --content "..."
ml status                   # check freshness
ml sync                     # validate + commit to git
```

**Always run `ml prime` at the start of a new session** to load accumulated knowledge before writing any code.

## graphify code index
graphify-ts is installed globally. Index is at graphify-out/graph.json (gitignored).
Rebuild after significant file changes:
```bash
PATH="$PATH:$HOME/.bun/bin" graphify build .
```
Query a symbol:
```bash
PATH="$PATH:$HOME/.bun/bin" graphify query graphify-out/graph.json Database
```
