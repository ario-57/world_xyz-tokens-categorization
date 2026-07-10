# Dune Prediction Token Refresh

This workflow runs once every 24 hours, fetches `m.world.xyz` Solana fungible tokens from Dune, categorizes each token by `name` with a configurable classifier service, and appends new rows to an uploaded Dune table.

## Required GitHub Secrets

Add these in `Settings -> Secrets and variables -> Actions -> Secrets`:

- `DUNE_API_KEY`: Dune API key with read/write upload permissions.
- `DUNE_NAMESPACE`: Your Dune upload namespace, usually your Dune username or team namespace.
- `CLASSIFIER_API_KEY`: API key for the classifier service. Existing `AI_API_KEY` also works.
- `CLASSIFIER_API_BASE_URL`: Classifier service base URL. Existing `AI_API_BASE_URL` also works. Default: `https://openrouter.ai/api/v1`.

## Optional GitHub Variables

Add these in `Settings -> Secrets and variables -> Actions -> Variables` if you want to override defaults:

- `DUNE_OUTPUT_TABLE`: Output table name. Default: `categorized_prediction_markets`.
- `DUNE_PERFORMANCE`: Dune SQL execution tier: `small`, `medium`, or `large`. Default: `medium`.
- `CLASSIFIER_MODEL`: Classifier model name. Existing `AI_MODEL` also works. Default: `openrouter/free`.
- `DUNE_REFRESH_MODE`: Use `auto` for normal runs or `full_rebuild` for a one-time historical reload. Default: `auto`.

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
- One-time rebuild: set `DUNE_REFRESH_MODE` to `full_rebuild`, run the workflow manually, then set it back to `auto` or delete the variable.

Daily incremental loads also include a `NOT EXISTS` check against the destination Dune table, so a rerun will not append a token whose `token_mint_address` is already present.

Each run:

1. Creates the Dune upload table if needed.
2. Counts existing destination rows.
3. Runs the historical or last-24-hours Dune SQL query.
4. Drops duplicate `token_mint_address` values within the current batch.
5. Categorizes new token names as `Sport` or `Crypto`.
6. Appends only the new rows to the destination table.

## Run Locally

```bash
pip install -r requirements.txt
export DUNE_API_KEY="..."
export DUNE_NAMESPACE="..."
export AI_API_KEY="..."
export AI_API_BASE_URL="https://openrouter.ai/api/v1"
export AI_MODEL="openrouter/free"
export DUNE_OUTPUT_TABLE="categorized_prediction_markets"
export DUNE_REFRESH_MODE="auto"
python scripts/refresh_dune_prediction_tokens.py
```

On Windows PowerShell:

```powershell
$env:DUNE_API_KEY="..."
$env:DUNE_NAMESPACE="..."
$env:AI_API_KEY="..."
$env:AI_API_BASE_URL="https://openrouter.ai/api/v1"
$env:AI_MODEL="openrouter/free"
$env:DUNE_OUTPUT_TABLE="categorized_prediction_markets"
$env:DUNE_REFRESH_MODE="auto"
python scripts/refresh_dune_prediction_tokens.py
```

## Notes

- The script sends batches of unique token names to the classifier service and expects structured JSON.
- Valid categories are `Sport` and `Crypto`.
- If categorization fails or returns an invalid category, the script uses fallback rules based on the token name.
