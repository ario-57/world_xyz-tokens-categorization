# Dune Prediction Token Refresh

This workflow runs once every 24 hours, fetches `m.world.xyz` Solana fungible tokens from Dune, categorizes each token by `name` with an OpenAI-compatible AI API, and refreshes an uploaded Dune table.

## Required GitHub Secrets

Add these in `Settings -> Secrets and variables -> Actions -> Secrets`:

- `DUNE_API_KEY`: Dune API key with read/write upload permissions.
- `DUNE_NAMESPACE`: Your Dune upload namespace, usually your Dune username or team namespace.
- `AI_API_KEY`: API key for a free OpenAI-compatible AI provider. The workflow defaults to OpenRouter.

## Optional GitHub Variables

Add these in `Settings -> Secrets and variables -> Actions -> Variables` if you want to override defaults:

- `DUNE_OUTPUT_TABLE`: Output table name. Default: `categorized_prediction_markets`.
- `DUNE_PERFORMANCE`: Dune SQL execution tier: `small`, `medium`, or `large`. Default: `medium`.
- `AI_API_BASE_URL`: OpenAI-compatible API base URL. Default: `https://openrouter.ai/api/v1`.
- `AI_MODEL`: AI model name. Default: `meta-llama/llama-3.1-8b-instruct:free`.

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

## How It Avoids Duplicates

Each run performs a full refresh:

1. Executes the source SQL query.
2. Drops duplicate `token_mint_address` values in the fresh result.
3. Creates the Dune upload table if needed.
4. Clears the existing table data.
5. Inserts the refreshed CSV data.

## Run Locally

```bash
pip install -r requirements.txt
export DUNE_API_KEY="..."
export DUNE_NAMESPACE="..."
export AI_API_KEY="..."
export DUNE_OUTPUT_TABLE="categorized_prediction_markets"
python scripts/refresh_dune_prediction_tokens.py
```

On Windows PowerShell:

```powershell
$env:DUNE_API_KEY="..."
$env:DUNE_NAMESPACE="..."
$env:AI_API_KEY="..."
$env:DUNE_OUTPUT_TABLE="categorized_prediction_markets"
python scripts/refresh_dune_prediction_tokens.py
```

## Notes

- The script sends batches of unique token names to the AI API and asks for JSON only.
- Valid categories are `Politics`, `Crypto`, `Sports`, `Finance`, `Entertainment`, `Tech`, `Economy`, `Geopolitics`, `Weather`, and `Other`.
- If an AI request fails or returns an invalid category, the script uses `Other`.
