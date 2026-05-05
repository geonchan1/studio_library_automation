# Studio Library automation — full pipeline (no Chrome, no manual upload)

End-to-end automation for the Upstage Studio Library use-case publishing workflow. Replaces the Studio web UI with direct Upstage API calls, automates synthetic-data generation for customer files, pushes the use-case set into Notion for the Library team, and emits SEO/meta tags ready for the public Library page.

## The 7-step workflow this implements

```
1. 고객으로부터 샘플 문서 수취
        │  (place files in a folder of your choice)
        ▼
2. 샘플 문서 기반 워크플로우 생성
        │  python pipeline.py one.pdf
        │  python batch.py /folder --schema schemas/x.json
        ▼
3. 워크플로우 config 전체 세트 문서화
        │  use_case_profile.json + library_card.json +
        │  workflow_design.md + extract_schema (in config)
        ▼
4. 고객 문서 전체 합성 데이터화
        │  python synth_batch.py /folder --render --verify
        ▼
5. 세트 문서 노션 내 축적
        │  python notion_sync.py --set ./trade_invoices_set
        ▼
6. 노션 → Studio Library 담당자가 주기적 배치 업데이트
        │  cron / GitHub Actions calls notion_sync.py + seo_meta.py
        ▼
7. 라이브러리 SEO + 메타태그 관리
           python seo_meta.py --card library_card.json --html
```

Each step is a single command. The whole pipeline can also be run as `make all`.

## Quick start

```bash
# 1. Install
pip install -r requirements.txt --break-system-packages

# 2. Get an Upstage API key, set it
cp .env.example .env
# edit .env, then:
export $(grep -v '^#' .env | xargs)

# 3. Smoke test on one PDF
python pipeline.py /path/to/one.pdf

# 4. Run the whole folder
python batch.py /path/to/customer/folder \
    --schema examples/trade_invoices.json \
    --workers 4 \
    --out-dir results
```

## Files

| File | What it does | Step |
|---|---|---|
| `pipeline.py` | Single PDF → Document Parse + Classify + IE → unified JSON. Drop-in replacement for the Studio agent we built in Chrome. | 2 |
| `batch.py` | Run `pipeline.py` over a whole folder in parallel; emits one JSON per file plus a CSV summary. | 2 |
| `synth.py` | One PDF → layout-preserving synthetic HTML (and optional PDF render via WeasyPrint). Uses Document Parse + Solar Pro2 with `synth_data.system.txt`. | 4 |
| `synth_batch.py` | Run `synth.py` on a whole folder, render to PDF, verify against the headless pipeline (no leakage + structural sanity). | 4 |
| `notion_sync.py` | Upsert the use-case set into a Notion database, idempotent on `slug`. Designed to run on a schedule. | 5–6 |
| `seo_meta.py` | Emit `<title>`, meta description, Open Graph, Twitter Card, JSON-LD `SoftwareApplication`. Validates against SEO best practices. | 7 |
| `examples/trade_invoices.json` | The schema config used for the 아성다이소 dogfood (Trade invoices). Copy + edit for new use-cases. | — |
| `Makefile` | One-shot wrappers — `make all`, `make batch`, etc. | — |
| `prompts/synth_data.system.txt` | System prompt for the synthesizer (in repo root). | 4 |
| `prompts/synth_data.user.txt` | User prompt template. | 4 |

> If you're starting from `outputs/`, the prompts are at `outputs/synth_data.{system,user}.txt`. Either place them in `automation/prompts/` or run `synth.py` from `outputs/automation/` — it auto-searches both locations.

## Step-by-step

### Step 2 — pipeline (one file, end-to-end)

```bash
python pipeline.py /path/to/[CI-SA\ 1-1]1-2-1-맨파워.PDF \
    --schema examples/trade_invoices.json
```

Output (also written to `*.result.json`):

```json
{
  "file": "[CI-SA 1-1]1-2-1-맨파워.PDF",
  "classification": {
    "commercial_invoice": {"pages": [1]},
    "packing_list":       {"pages": [2]},
    "proforma_invoice":   {"pages": [3]}
  },
  "extraction": {
    "invoice_number": "JSJT-DC20251125-2",
    "vendor_name":    "JIANGSU JIUTONG PLASTIC MANUFACTURING CO., LTD",
    "buyer_name":     "ASUNGHMP CO.,Ltd",
    "invoice_date":   "2025-11-25",
    "departure_port": "Shanghai",
    "destination_port": "PYONGTAEK",
    "currency":       "USD",
    "total_amount":   "$7,920.00"
  },
  "errors": []
}
```

This is exactly what the `Trade invoices` Studio agent produced in the Chrome dogfood, and it's reproducible without any browser interaction.

### Step 2 (bulk) — batch

```bash
python batch.py /Users/.../아성다이소/인보이스,패킹리스트 \
    --schema examples/trade_invoices.json \
    --workers 4 \
    --out-dir results
```

Produces `results/<filename>.result.json` per input plus `results/summary.csv` with one row per file × 8 schema fields × 4 classes.

### Step 4 — synthetic data (Phase 2)

```bash
# One file, save synthetic HTML
python synth.py /path/to/real.pdf \
    --hint industry='household plastics' \
    --hint country_pair='CN -> KR'

# Whole folder, render to PDF, verify
python synth_batch.py /path/to/customer/folder \
    --out-dir synth_results --workers 3 --render --verify \
    --hint industry='household plastics'
```

`--verify` runs the headless pipeline against the freshly-rendered synthetic PDF and checks (a) **no original identifiers leak** (vendor name, invoice number, total amount must not appear) and (b) **the same set of schema fields populate** as on the original. Files that fail leakage check are marked in `synth_summary.json`.

WeasyPrint is required for `--render`. On macOS:
```bash
brew install pango cairo glib gdk-pixbuf libffi
pip install weasyprint --break-system-packages
```

### Step 5–6 — Notion sync

One-time setup:
1. Create an integration at https://www.notion.so/my-integrations
2. Create a Notion database with the properties listed in the docstring of `notion_sync.py`
3. Share the database with the integration
4. `export NOTION_TOKEN=...` and `export NOTION_LIBRARY_DB_ID=...`

Then upsert a use-case set:
```bash
python notion_sync.py \
    --set ../trade_invoices_set/ \
    --agent-url "https://studio.upstage.ai/agents/agt_8dSHuEBXsm9mmNxszqWn9Y"
```

The set folder must contain `library_card.json`, `use_case_profile.json`, and ideally `workflow_design.md`, `end_to_end_run_log.md`, `synth_summary.json`.

Re-running with the same `slug` updates the existing row instead of creating a duplicate. Wire this into a daily cron / GitHub Actions schedule and the Library team gets a stable Notion view that always reflects the latest verified state.

### Step 7 — SEO meta tags

```bash
python seo_meta.py --card ../trade_invoices_set/library_card.json --html
```

Prints a ready-to-paste `<head>` snippet:

```html
<title>Trade invoices agent — Logistics document AI | Upstage Studio</title>
<meta name="description" content="This agent automatically classifies and extracts...">
<link rel="canonical" href="https://studio.upstage.ai/library/library/trade-invoices">
<meta property="og:title" content="...">
...
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  ...
}
</script>
```

Validation runs automatically: `<title>` 30–60 chars, description 70–160 chars, slug ≤60 chars lowercase-hyphen, OG image absolute https, no marketing buzzwords. `python seo_meta.py --check` exits non-zero on any fail.

## Schedule it (cron / GitHub Actions)

Once a use-case is live, the only ongoing work is the periodic Notion sync (step 6) so the Library page stays current. Sample crontab:

```
# Every morning at 06:30, refresh all use-case rows from local sets folder
30 6 * * * cd /opt/studio-automation && \
    for d in sets/*/ ; do \
        python notion_sync.py --set "$d" || echo "Failed: $d" ; \
    done
```

For GitHub Actions, the same loop in a `cron`-triggered workflow with `UPSTAGE_API_KEY`, `NOTION_TOKEN`, and `NOTION_LIBRARY_DB_ID` as secrets.

## Why this is faster than the Chrome flow we used

| Step | Chrome flow (UI) | This script (API) |
|---|---|---|
| Create agent | ~3 minutes per use case | n/a (no agent needed) |
| Configure schema | manual click loop | one JSON file |
| Wire Classify→Extract | manual drag (easy to miss) | API has no separate "wiring" step |
| Upload sample | native file picker (no automation) | direct API call |
| Run | 1 click + wait | one HTTP request, parallelizable |
| Iterate on schema | re-edit fields in UI | edit `examples/<name>.json`, rerun |
| Bulk over a folder | upload one file at a time | `batch.py` with N workers |

The Chrome flow is still the right tool for **interactive exploration**: discovering classes, debugging a tricky page, tweaking descriptions. Once the schema stabilizes, this automation reproduces the same results without a browser.

## Limits / known gaps

- Classification & IE are limited to **100 pages per request** (sync). Use the async endpoints for >100 pages — `pipeline.py` doesn't currently call them, but the URLs and request shape are the same.
- IE schema **first-level properties cannot be `object`**. Use top-level scalars + arrays of objects instead.
- `synth.py` re-renders via WeasyPrint, which approximates layout but won't match raster fidelity. For production-grade synthetic samples, consider Playwright + headless Chromium.
- `notion_sync.py` does an additive append to the page body on update. If you want full replacement semantics, archive old children before patching.

## File map

```
automation/
├── README.md
├── Makefile
├── requirements.txt
├── .env.example
├── pipeline.py
├── batch.py
├── synth.py
├── synth_batch.py
├── notion_sync.py
├── seo_meta.py
└── examples/
    └── trade_invoices.json
```

Prompts (`synth_data.system.txt`, `synth_data.user.txt`) live in `outputs/` from this session and the synth scripts auto-locate them. Copy them into `automation/prompts/` for a self-contained deployment.
