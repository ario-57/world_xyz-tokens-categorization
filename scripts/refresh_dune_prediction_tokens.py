import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests
from requests import Response, Session


DUNE_API_BASE_URL = "https://api.dune.com/api/v1"
SOURCE_COLUMNS = ["token_mint_address", "symbol", "name", "decimals"]

CATEGORIES = ["Sport", "Crypto"]

CATEGORY_GUIDANCE = {
    "Crypto": "Token prices, tickers, chains, market direction, protocol names, or onchain assets.",
    "Sport": "Teams, athletes, tournaments, matches, leagues, fights, winners, or score outcomes.",
}

SPORT_PATTERN = re.compile(
    r"\b("
    r"wc26|wm26|ww26|world cup|championship|tournament|"
    r"wins?|doesn'?t win|beats?|loses?|vs|"
    r"tennis|football|soccer|basketball|baseball|hockey|"
    r"boxing|mma|ufc|fifa|nba|nfl|mlb|nhl|epl"
    r")\b",
    re.IGNORECASE,
)


class ConfigError(RuntimeError):
    pass


def quote_identifier(identifier: str) -> str:
    if not identifier:
        raise ConfigError("Dune SQL identifier cannot be empty")
    return '"' + identifier.replace('"', '""') + '"'


def uploaded_table_sql_name(namespace: str, table_name: str) -> str:
    return f"dune.{quote_identifier(namespace)}.{quote_identifier(table_name)}"


def build_source_sql(*, incremental: bool, namespace: str, table_name: str) -> str:
    filters = [
        "source.token_uri IS NOT NULL",
        "LOWER(source.token_uri) LIKE '%m.world.xyz%'",
    ]
    if incremental:
        destination_table = uploaded_table_sql_name(namespace, table_name)
        filters.extend(
            [
                "source.created_at >= NOW() - INTERVAL '24' HOUR",
                f"""
NOT EXISTS (
    SELECT 1
    FROM {destination_table} existing
    WHERE existing.token_mint_address = source.token_mint_address
)""".strip(),
            ]
        )

    return f"""
SELECT
    source.token_mint_address,
    source.symbol,
    source.name,
    source.decimals
FROM tokens_solana.fungible source
WHERE {' AND '.join(filters)}
"""


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def env_first(*names: str, default: str | None = None) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    if default is not None:
        return default
    raise ConfigError(f"Missing required environment variable: {' or '.join(names)}")


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
            response = getattr(exc, "response", None)
            retry_after = response.headers.get("Retry-After") if response is not None else None
            sleep_seconds = int(retry_after) if retry_after and retry_after.isdigit() else min(180, 5 * 2 ** attempt)
            print(f"Retrying {method} {url} after error: {exc}. Sleeping {sleep_seconds}s")
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Request failed after {max_attempts} attempts: {last_error}")


def raise_for_api_error(response: Response, context: str) -> None:
    if response.ok:
        return
    raise RuntimeError(f"{context} failed: HTTP {response.status_code}: {response.text[:1000]}")


def execute_dune_sql(session: Session, api_key: str, performance: str, sql: str) -> str:
    response = request_with_retry(
        session,
        "POST",
        f"{DUNE_API_BASE_URL}/sql/execute",
        headers={"X-DUNE-API-KEY": api_key, "Content-Type": "application/json"},
        json={"sql": sql, "performance": performance},
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
            execution_cost = payload.get("execution_cost_credits")
            if execution_cost is not None:
                print(f"Dune execution {execution_id} cost: {execution_cost} credits")
            return
        if state in {
            "QUERY_STATE_FAILED",
            "QUERY_STATE_CANCELED",
            "QUERY_STATE_CANCELLED",
            "QUERY_STATE_EXPIRED",
        }:
            raise RuntimeError(f"Dune execution ended in {state}: {json.dumps(payload)[:2000]}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"Dune execution {execution_id} did not finish within {max_wait_seconds}s")


def fetch_dune_results(
    session: Session,
    api_key: str,
    execution_id: str,
    *,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    csv_response = request_with_retry(
        session,
        "GET",
        f"{DUNE_API_BASE_URL}/execution/{execution_id}/results/csv",
        headers={"X-DUNE-API-KEY": api_key},
        params={"columns": ",".join(columns)} if columns else None,
        max_attempts=8,
        timeout=180,
    )
    if csv_response.ok:
        csv_text = csv_response.text.strip()
        if not csv_text:
            return pd.DataFrame(columns=columns)
        return pd.read_csv(io.StringIO(csv_text))

    raise_for_api_error(csv_response, "Dune execution CSV results")


def execute_sql_to_dataframe(
    session: Session,
    api_key: str,
    performance: str,
    sql: str,
    *,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    execution_id = execute_dune_sql(session, api_key, performance, sql)
    wait_for_dune_execution(session, api_key, execution_id)
    return fetch_dune_results(session, api_key, execution_id, columns=columns)


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def fallback_category(name: str) -> str:
    return "Sport" if SPORT_PATTERN.search(name) else "Crypto"


def categorize_batch(
    session: Session,
    names: list[str],
    *,
    api_key: str,
    api_base_url: str,
    model: str,
) -> dict[str, str]:
    system_prompt = (
        "You are a strict prediction-market token classifier. "
        "Every token name must be classified as exactly one of: Sport or Crypto. "
        "Do not invent categories. Do not use unclear, unknown, other, or null. "
        "Return only valid JSON with no prose."
    )
    user_prompt = {
        "task": "Map each token name to exactly one category.",
        "allowed_categories": CATEGORIES,
        "category_guidance": CATEGORY_GUIDANCE,
        "rules": [
            "Use only the token name.",
            "Ticker names, asset names, chains, symbols, or up/down price direction names are Crypto.",
            "World Cup, tennis, boxing, MMA, team-versus-team, athlete, match, fight, or tournament names are Sport.",
            "If the name is short, ticker-like, or unclear, choose Crypto.",
        ],
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
    raise_for_api_error(response, "Categorization")
    content = response.json()["choices"][0]["message"]["content"]
    parsed = extract_json_object(content)
    raw_categories = parsed.get("categories", parsed)

    categories: dict[str, str] = {}
    for name in names:
        category = str(raw_categories.get(name, "")).strip()
        categories[name] = category if category in CATEGORIES else fallback_category(name)
    return categories


def categorize_names(session: Session, names: list[str]) -> dict[str, str]:
    api_key = env_first("CLASSIFIER_API_KEY", "AI_API_KEY")
    api_base_url = env_first("CLASSIFIER_API_BASE_URL", "AI_API_BASE_URL", default="https://openrouter.ai/api/v1")
    model = env_first("CLASSIFIER_MODEL", "AI_MODEL", default="openrouter/free")
    batch_size = int(os.getenv("CLASSIFIER_BATCH_SIZE", "50"))

    results: dict[str, str] = {}
    for index in range(0, len(names), batch_size):
        batch = names[index : index + batch_size]
        print(f"Categorizing names {index + 1}-{index + len(batch)} of {len(names)}")
        try:
            results.update(
                categorize_batch(
                    session,
                    batch,
                    api_key=api_key,
                    api_base_url=api_base_url,
                    model=model,
                )
            )
        except Exception as exc:
            print(f"Categorization failed for batch; using fallback rules. Error: {exc}", file=sys.stderr)
            results.update({name: fallback_category(name) for name in batch})
    return results


def prepare_final_dataset(df: pd.DataFrame, categories: dict[str, str]) -> pd.DataFrame:
    missing = [column for column in SOURCE_COLUMNS if column not in df.columns]
    if missing:
        raise RuntimeError(f"Missing expected Dune result columns: {missing}")

    final = df[SOURCE_COLUMNS].copy()
    final = final.drop_duplicates(subset=["token_mint_address"], keep="first")
    final["category"] = final["name"].fillna("").astype(str).map(categories)
    final["category"] = final.apply(
        lambda row: row["category"] if row["category"] in CATEGORIES else fallback_category(str(row["name"] or "")),
        axis=1,
    )
    final["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return final[["token_mint_address", "symbol", "name", "decimals", "category", "updated_at"]]


def create_table_if_needed(session: Session, api_key: str, namespace: str, table_name: str) -> None:
    payload = {
        "namespace": namespace,
        "table_name": table_name,
        "description": "Daily m.world.xyz Solana fungible tokens categorized by prediction-market theme.",
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


def list_uploaded_tables(session: Session, api_key: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    offset = 0
    limit = 10000
    while True:
        response = request_with_retry(
            session,
            "GET",
            f"{DUNE_API_BASE_URL}/uploads",
            headers={"X-DUNE-API-KEY": api_key},
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        raise_for_api_error(response, "Dune table list")
        payload = response.json()
        tables.extend(payload.get("tables", []))
        next_offset = payload.get("next_offset")
        if next_offset is None:
            break
        offset = int(next_offset)
    return tables


def table_exists(session: Session, api_key: str, namespace: str, table_name: str) -> bool:
    full_name = f"dune.{namespace}.{table_name}".lower()
    return any(
        str(table.get("full_name", "")).lower() == full_name and table.get("purged_at") is None
        for table in list_uploaded_tables(session, api_key)
    )


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


def clear_table(session: Session, api_key: str, namespace: str, table_name: str) -> None:
    response = request_with_retry(
        session,
        "POST",
        f"{DUNE_API_BASE_URL}/uploads/{namespace}/{table_name}/clear",
        headers={"X-DUNE-API-KEY": api_key},
    )
    raise_for_api_error(response, "Dune table clear")
    print(f"Cleared table: {namespace}.{table_name}")


def main() -> None:
    dune_api_key = env_required("DUNE_API_KEY")
    namespace = env_required("DUNE_NAMESPACE")
    table_name = os.getenv("DUNE_OUTPUT_TABLE", "categorized_prediction_markets")
    performance = os.getenv("DUNE_PERFORMANCE", "small")
    refresh_mode = os.getenv("DUNE_REFRESH_MODE", "auto").strip().lower()
    if refresh_mode not in {"auto", "full_rebuild"}:
        raise ConfigError("DUNE_REFRESH_MODE must be auto or full_rebuild")
    full_rebuild = refresh_mode == "full_rebuild"

    with requests.Session() as session:
        exists_before_run = table_exists(session, dune_api_key, namespace, table_name)
        create_table_if_needed(session, dune_api_key, namespace, table_name)
        incremental = exists_before_run and not full_rebuild
        mode = (
            "full rebuild"
            if full_rebuild
            else "incremental last-24-hours append"
            if incremental
            else "initial full seed"
        )
        print(f"Running in {mode} mode with the {performance} Dune engine.")

        source_sql = build_source_sql(
            incremental=incremental,
            namespace=namespace,
            table_name=table_name,
        )
        source_df = execute_sql_to_dataframe(
            session,
            dune_api_key,
            performance,
            source_sql,
            columns=SOURCE_COLUMNS,
        )
        print(f"Fetched {len(source_df)} source rows")

        if source_df.empty:
            print("No new tokens to insert.")
            return

        unique_names = sorted(source_df["name"].dropna().astype(str).unique())
        category_map = categorize_names(session, unique_names)
        final_df = prepare_final_dataset(source_df, category_map)
        print("Category counts:")
        print(final_df["category"].value_counts().to_string())

        if full_rebuild and exists_before_run:
            clear_table(session, dune_api_key, namespace, table_name)
        insert_table(session, dune_api_key, namespace, table_name, final_df)
        print(f"Refresh complete: {namespace}.{table_name} ({len(final_df)} inserted rows)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Refresh failed: {exc}", file=sys.stderr)
        raise
