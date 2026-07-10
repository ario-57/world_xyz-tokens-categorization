# Dune Prediction Token Refresh

This workflow runs once every 24 hours, fetches `m.world.xyz` Solana fungible tokens from Dune, categorizes each token by `name` with a configurable classifier service, and appends new rows to an uploaded Dune table.

## Required GitHub Secrets

Add these in `Settings -> Secrets and variables -> Actions -> Secrets`:

- `DUNE_API_KEY`: Dune API key with read/write upload permissions.
- `DUNE_NAMESPACE`: Your Dune upload namespace, usually your Dune username or team namespace.
- `CLASSIFIER_API_KEY`: API key for the classifier service.
- `CLASSIFIER_API_BASE_URL`: Classifier service base URL.

## Optional GitHub Variables

Add these in `Settings -> Secrets and variables -> Actions -> Variables` if you want to override defaults:

- `DUNE_OUTPUT_TABLE`: Output table name. Default: `categorized_prediction_markets`.
- `DUNE_PERFORMANCE`: Dune SQL execution tier: `small`, `medium`, or `large`. Default: `medium`.
- `CLASSIFIER_MODEL`: Classifier model name. Default: `google/gemini-2.5-flash`.

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

The script checks whether the configured Dune output table already exists and has rows.

- First run, or an existing empty table: loads all matching historical tokens.
- Later runs: loads only tokens where `tokens_solana.fungible.created_at >= NOW() - INTERVAL '24' HOUR`.

Daily incremental loads also include a `NOT EXISTS` check against the destination Dune table, so a rerun will not append a token whose `token_mint_address` is already present.

Each run:

1. Creates the Dune upload table if needed.
2. Counts existing destination rows.
3. Runs the historical or last-24-hours Dune SQL query.
4. Drops duplicate `token_mint_address` values within the current batch.
5. Categorizes new token names.
6. Appends only the new rows to the destination table.

## Run Locally

```bash
pip install -r requirements.txt
export DUNE_API_KEY="..."
export DUNE_NAMESPACE="..."
export CLASSIFIER_API_KEY="..."
export CLASSIFIER_API_BASE_URL="..."
export CLASSIFIER_MODEL="google/gemini-2.5-flash"
export DUNE_OUTPUT_TABLE="categorized_prediction_markets"
python scripts/refresh_dune_prediction_tokens.py
```

On Windows PowerShell:

```powershell
$env:DUNE_API_KEY="..."
$env:DUNE_NAMESPACE="..."
$env:CLASSIFIER_API_KEY="..."
$env:CLASSIFIER_API_BASE_URL="..."
$env:CLASSIFIER_MODEL="google/gemini-2.5-flash"
$env:DUNE_OUTPUT_TABLE="categorized_prediction_markets"
python scripts/refresh_dune_prediction_tokens.py
```

## Notes

- The script sends batches of unique token names to the classifier service and expects structured JSON.
- Valid categories are `Politics`, `Crypto`, `Sports`, `Finance`, `Entertainment`, `Tech`, `Economy`, `Geopolitics`, `Weather`, and `Other`.
- If categorization fails or returns an invalid category, the script uses `Other`.
