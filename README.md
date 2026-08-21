# Dune Prediction Token Refresh

This workflow runs once every 24 hours, fetches `m.world.xyz` Solana fungible tokens from Dune, categorizes each token by `name` with a configurable classifier service, and appends new rows to an uploaded Dune table. Valid categories are `Sport`, `Crypto`, `Finance`, and `Politics`.

## Required GitHub Secrets

Add these in `Settings -> Secrets and variables -> Actions -> Secrets`:

- `DUNE_API_KEY`: Dune API key with read/write upload permissions.
- `CLASSIFIER_API_KEY`: API key for the classifier service. Existing `AI_API_KEY` also works.
- `CLASSIFIER_API_BASE_URL`: Classifier service base URL. Existing `AI_API_BASE_URL` also works. Default: `https://openrouter.ai/api/v1`.

`DUNE_API_KEY` must belong to the personal Dune account whose namespace is `ario_57`. The GitHub Actions workflow targets that namespace explicitly and writes to `dune.ario_57.categorized_prediction_markets` by default.

## Optional GitHub Variables

Add these in `Settings -> Secrets and variables -> Actions -> Variables` if you want to override defaults:

- `DUNE_OUTPUT_TABLE`: Output table name. Default: `categorized_prediction_markets`.
- `DUNE_PERFORMANCE`: Dune SQL execution tier: `small`, `medium`, or `large`. Default: `medium` for reliable API execution.
- `CLASSIFIER_MODEL`: Classifier model name. Existing `AI_MODEL` also works. Default: `openrouter/free`.

Scheduled runs always use `auto` refresh mode. For the one-time migration, start the workflow manually and choose `csv_rebuild`. This loads the checked-in `data/historical_categorized_prediction_markets.csv`, then fetches and categorizes only tokens created since the CSV's latest `updated_at` value. It avoids fetching historical rows through the Dune API.

## Required Dune Credit Cap

GitHub Actions cannot set a Dune compute-credit limit in an individual SQL execution request. Configure the hard cap in Dune before enabling the schedule:

1. Sign in to Dune and select the user or team that owns the API key.
2. Open `Settings`.
3. Find the query cost cap setting and set the maximum cost per query to `30` credits.
4. Save the setting and keep `DUNE_PERFORMANCE` set to `medium`.

Dune applies this cap to API-triggered queries. If an execution reaches the cap, Dune stops it instead of allowing that query to consume more than 30 compute credits. Configure the cap on the same user or team account associated with `DUNE_API_KEY`.

## Output Schema

The uploaded Dune table contains:

```text
token_mint_address
symbol
name
decimals
category
updated_at
```

## Initial Load And Daily Incremental Loads

The script checks whether the configured Dune output table already exists. The GitHub Actions workflow uses the `ario_57` namespace; local runs also default to `ario_57` unless `DUNE_NAMESPACE` is set explicitly.

- First run with `csv_rebuild`: loads the historical CSV and adds tokens created since its latest update.
- Normal later runs start from the destination table's latest `updated_at`, with a 48-hour overlap to recover from delayed or failed runs.
- The `NOT EXISTS` check prevents duplicate `token_mint_address` values across runs.

The 30-credit query cost cap is enforced by Dune, independently of how many tokens the query returns.

Each run:

1. Creates the Dune upload table if needed.
2. Loads the local historical CSV during migration or queries only the incremental source window on normal runs.
3. Lets Dune enforce the configured 30-credit maximum for that query.
4. Drops duplicate `token_mint_address` values within the current batch.
5. Categorizes new token names as `Sport`, `Crypto`, `Finance`, or `Politics`.
6. Appends only the new rows to the destination table.

The Actions log prints Dune's reported execution cost after each completed SQL query.

## Historical CSV Seed

`data/historical_categorized_prediction_markets.csv` is a deduplicated export of Dune query `8397329`. It contains the latest row for each of 100 unique token mint addresses from the supplied 93,695-row export. Its latest `updated_at` is `2026-07-26 06:14:23` UTC, so `csv_rebuild` queries only tokens created after that watermark, with a 48-hour safety overlap.

To replace the seed later, keep the same six-column schema shown above. The script validates timestamps and categories, keeps the newest row per mint address, and rejects unsupported values before changing the destination table.

## Run Locally

```bash
pip install -r requirements.txt
export DUNE_API_KEY="..."
export DUNE_NAMESPACE="ario_57"
export AI_API_KEY="..."
export AI_API_BASE_URL="https://openrouter.ai/api/v1"
export AI_MODEL="openrouter/free"
export DUNE_OUTPUT_TABLE="categorized_prediction_markets"
export DUNE_REFRESH_MODE="auto"
export HISTORICAL_CSV_PATH="data/historical_categorized_prediction_markets.csv"
export DUNE_PERFORMANCE="medium"
python scripts/refresh_dune_prediction_tokens.py
```

On Windows PowerShell:

```powershell
$env:DUNE_API_KEY="..."
$env:DUNE_NAMESPACE="ario_57"
$env:AI_API_KEY="..."
$env:AI_API_BASE_URL="https://openrouter.ai/api/v1"
$env:AI_MODEL="openrouter/free"
$env:DUNE_OUTPUT_TABLE="categorized_prediction_markets"
$env:DUNE_REFRESH_MODE="auto"
$env:HISTORICAL_CSV_PATH="data/historical_categorized_prediction_markets.csv"
$env:DUNE_PERFORMANCE="medium"
python scripts/refresh_dune_prediction_tokens.py
```

## Notes

- The script sends batches of unique token names to the classifier service and expects structured JSON.
- Valid categories are `Sport`, `Crypto`, `Finance`, and `Politics`.
- If categorization fails or returns an invalid category, the script uses fallback rules based on the token name.
