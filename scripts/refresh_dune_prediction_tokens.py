import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests
from requests import Response, Session


DUNE_API_BASE_URL = "https://api.dune.com/api/v1"
SOURCE_SQL = """
SELECT
    token_mint_address,
    symbol,
    name,
    decimals
FROM tokens_solana.fungible
WHERE token_uri IS NOT NULL
  AND LOWER(token_uri) LIKE '%m.world.xyz%'
"""

CATEGORIES = [
    "Politics",
    "Crypto",
    "Sports",
    "Finance",
    "Entertainment",
    "Tech",
    "Economy",
    "Geopolitics",
    "Weather",
    "Other",
]


class ConfigError(RuntimeError):
    pass


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def request_with_retry(
    session: Session,
    method: str,
    url: str,
    *,
    max_attempts: int = 5,
    timeout: int = 60,
    **kwargs: Any,
) -> Response:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.HTTPError(
                    f"retryable HTTP {response.status_code}: {response.text[:500]}",
                    response=response,
                )
            return response
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            sleep_seconds = min(60, 2 ** attempt)
            print(f"Retrying {method} {url} after error: {exc}. Sleeping {sleep_seconds}s")
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Request failed after {max_attempts} attempts: {last_error}")


def raise_for_api_error(response: Response, context: str) -> None:
    if response.ok:
        return
    raise RuntimeError(f"{context} failed: HTTP {response.status_code}: {response.text[:1000]}")


def execute_dune_sql(session: Session, api_key: str, performance: str) -> str:
    response = request_with_retry(
        session,
        "POST",
        f"{DUNE_API_BASE_URL}/sql/execute",
        headers={"X-DUNE-API-KEY": api_key, "Content-Type": "application/json"},
        json={"sql": SOURCE_SQL, "performance": performance},
    )
    raise_for_api_error(response, "Dune SQL execution")
    execution_id = response.json().get("execution_id")
    if not execution_id:
        raise RuntimeError(f"Dune execution response did not include execution_id: {response.text}")
    return execution_id


def wait_for_dune_execution(
    session: Session,
    api_key: str,
    execution_id: str,
    *,
    poll_seconds: int = 5,
    max_wait_seconds: int = 900,
) -> None:
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        response = request_with_retry(
            session,
            "GET",
            f"{DUNE_API_BASE_URL}/execution/{execution_id}/status",
            headers={"X-DUNE-API-KEY": api_key},
            timeout=30,
        )
        raise_for_api_error(response, "Dune execution status")
        payload = response.json()
        state = payload.get("state")
        print(f"Dune execution {execution_id} state: {state}")
        if state == "QUERY_STATE_COMPLETED":
            return
        if state in {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"}:
            raise RuntimeError(f"Dune execution ended in {state}: {json.dumps(payload)[:2000]}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"Dune execution {execution_id} did not finish within {max_wait_seconds}s")


def fetch_dune_results(session: Session, api_key: str, execution_id: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    offset = 0
    limit = int(os.getenv("DUNE_RESULT_PAGE_SIZE", "1000"))

    while True:
        response = request_with_retry(
            session,
            "GET",
            f"{DUNE_API_BASE_URL}/execution/{execution_id}/results",
            headers={"X-DUNE-API-KEY": api_key},
            params={"limit": limit, "offset": offset},
        )
        raise_for_api_error(response, "Dune execution results")
        payload = response.json()
        rows.extend(payload.get("result", {}).get("rows", []))
        next_offset = payload.get("next_offset")
        if next_offset is None:
            break
        offset = int(next_offset)

    return pd.DataFrame(rows, columns=["token_mint_address", "symbol", "name", "decimals"])


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def ai_categorize_batch(
    session: Session,
    names: list[str],
    *,
    api_key: str,
    api_base_url: str,
    model: str,
) -> dict[str, str]:
    system_prompt = (
        "You categorize prediction-market token names. "
        "Use only these categories: "
        + ", ".join(CATEGORIES)
        + ". If unclear, use Other. Return only valid JSON."
    )
    user_prompt = {
        "task": "Map each token name to exactly one category.",
        "allowed_categories": CATEGORIES,
        "token_names": names,
        "output_shape": {"categories": {"<token name>": "<category>"}},
    }
    response = request_with_retry(
        session,
        "POST",
        f"{api_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
            ],
        },
    )
    raise_for_api_error(response, "AI categorization")
    content = response.json()["choices"][0]["message"]["content"]
    parsed = extract_json_object(content)
    raw_categories = parsed.get("categories", parsed)

    categories: dict[str, str] = {}
    for name in names:
        category = str(raw_categories.get(name, "Other")).strip()
        categories[name] = category if category in CATEGORIES else "Other"
    return categories


def categorize_names(session: Session, names: list[str]) -> dict[str, str]:
    api_key = env_required("AI_API_KEY")
    api_base_url = os.getenv("AI_API_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("AI_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
    batch_size = int(os.getenv("AI_BATCH_SIZE", "50"))

    results: dict[str, str] = {}
    for index in range(0, len(names), batch_size):
        batch = names[index : index + batch_size]
        print(f"Categorizing names {index + 1}-{index + len(batch)} of {len(names)}")
        try:
            results.update(
                ai_categorize_batch(
                    session,
                    batch,
                    api_key=api_key,
                    api_base_url=api_base_url,
                    model=model,
                )
            )
        except Exception as exc:
            print(f"AI categorization failed for batch; using Other. Error: {exc}", file=sys.stderr)
            results.update({name: "Other" for name in batch})
    return results


def prepare_final_dataset(df: pd.DataFrame, categories: dict[str, str]) -> pd.DataFrame:
    required_columns = ["token_mint_address", "symbol", "name", "decimals"]
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        raise RuntimeError(f"Missing expected Dune result columns: {missing}")

    final = df[required_columns].copy()
    final = final.drop_duplicates(subset=["token_mint_address"], keep="first")
    final["category"] = final["name"].fillna("").astype(str).map(categories).fillna("Other")
    final["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return final[["token_mint_address", "symbol", "name", "decimals", "category", "updated_at"]]


def create_table_if_needed(session: Session, api_key: str, namespace: str, table_name: str) -> None:
    payload = {
        "namespace": namespace,
        "table_name": table_name,
        "description": "Daily refreshed m.world.xyz Solana fungible tokens categorized by prediction-market theme.",
        "is_private": os.getenv("DUNE_TABLE_PRIVATE", "false").lower() == "true",
        "schema": [
            {"name": "token_mint_address", "type": "varchar", "nullable": False},
            {"name": "symbol", "type": "varchar", "nullable": True},
            {"name": "name", "type": "varchar", "nullable": True},
            {"name": "decimals", "type": "bigint", "nullable": True},
            {"name": "category", "type": "varchar", "nullable": False},
            {"name": "updated_at", "type": "timestamp", "nullable": False},
        ],
    }
    response = request_with_retry(
        session,
        "POST",
        f"{DUNE_API_BASE_URL}/uploads",
        headers={"X-DUNE-API-KEY": api_key, "Content-Type": "application/json"},
        json=payload,
    )
    if response.ok:
        print(f"Table ready: {namespace}.{table_name}")
        return
    if response.status_code == 400 and "exist" in response.text.lower():
        print(f"Table already exists: {namespace}.{table_name}")
        return
    raise_for_api_error(response, "Dune table create")


def clear_table(session: Session, api_key: str, namespace: str, table_name: str) -> None:
    response = request_with_retry(
        session,
        "POST",
        f"{DUNE_API_BASE_URL}/uploads/{namespace}/{table_name}/clear",
        headers={"X-DUNE-API-KEY": api_key},
    )
    raise_for_api_error(response, "Dune table clear")


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False, quoting=csv.QUOTE_MINIMAL)
    return buffer.getvalue().encode("utf-8")


def insert_table(session: Session, api_key: str, namespace: str, table_name: str, df: pd.DataFrame) -> None:
    response = request_with_retry(
        session,
        "POST",
        f"{DUNE_API_BASE_URL}/uploads/{namespace}/{table_name}/insert",
        headers={"X-DUNE-API-KEY": api_key, "Content-Type": "text/csv"},
        data=dataframe_to_csv_bytes(df),
        timeout=120,
    )
    raise_for_api_error(response, "Dune table insert")
    print(f"Inserted table rows: {response.text}")


def main() -> None:
    dune_api_key = env_required("DUNE_API_KEY")
    namespace = env_required("DUNE_NAMESPACE")
    table_name = os.getenv("DUNE_OUTPUT_TABLE", "categorized_prediction_markets")
    performance = os.getenv("DUNE_PERFORMANCE", "medium")

    with requests.Session() as session:
        execution_id = execute_dune_sql(session, dune_api_key, performance)
        wait_for_dune_execution(session, dune_api_key, execution_id)
        source_df = fetch_dune_results(session, dune_api_key, execution_id)
        print(f"Fetched {len(source_df)} source rows")

        unique_names = sorted(source_df["name"].dropna().astype(str).unique())
        category_map = categorize_names(session, unique_names)
        final_df = prepare_final_dataset(source_df, category_map)
        print("Category counts:")
        print(final_df["category"].value_counts().to_string())

        create_table_if_needed(session, dune_api_key, namespace, table_name)
        clear_table(session, dune_api_key, namespace, table_name)
        insert_table(session, dune_api_key, namespace, table_name, final_df)
        print(f"Refresh complete: {namespace}.{table_name} ({len(final_df)} rows)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Refresh failed: {exc}", file=sys.stderr)
        raise
