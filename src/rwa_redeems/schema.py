"""Canonical output schema: one row per redemption event, identical for every protocol."""

from __future__ import annotations

import pandas as pd

REDEMPTION_TYPES = ("instant", "request", "fiat_request")

# column -> pandas dtype
COLUMNS: dict[str, str] = {
    "chain": "string",
    "chain_id": "Int64",
    "protocol": "string",
    "token": "string",
    "token_address": "string",
    "contract": "string",  # contract the redemption went through (vault / redemption contract / token)
    "contract_name": "string",
    "redemption_type": "string",  # instant | request | fiat_request
    "method": "string",  # protocol-level variant (event / path)
    "tx_hash": "string",
    "log_index": "Int64",
    "block_number": "Int64",
    "timestamp": "datetime64[s, UTC]",
    "caller": "string",  # msg.sender-level actor when it differs from holder (e.g. helper contract)
    "holder": "string",  # economic owner whose tokens were redeemed
    "recipient": "string",  # receiver of the payout when known on-chain
    "request_id": "Int64",
    "token_amount": "Float64",  # gross redeemed amount, decimal-normalised
    "token_amount_raw": "string",  # exact on-chain integer
    "fee_token_amount": "Float64",  # fee charged in the redeemed token
    "token_out": "string",
    "token_out_symbol": "string",
    "token_out_amount": "Float64",  # payout received (instant only)
    "token_out_fee_amount": "Float64",  # fee charged in the payout token
    "token_out_usd": "Float64",  # USD value of payout received
    "price_usd": "Float64",  # token NAV/price at the event block
    "price_source": "string",
    "price_timestamp": "datetime64[s, UTC]",  # oracle update time / NAV date behind price_usd
    "price_is_backfilled": "boolean",
    "amount_usd": "Float64",  # token_amount * price_usd
    "is_pre_launch": "boolean",
    "via_fallback": "boolean",
}


def conform(df: pd.DataFrame) -> pd.DataFrame:
    """Add missing columns, enforce dtypes/order, validate enums."""
    df = df.copy()
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    extra = set(df.columns) - set(COLUMNS)
    if extra:
        raise ValueError(f"unexpected columns: {sorted(extra)}")
    df = df[list(COLUMNS)]
    for col, dtype in COLUMNS.items():
        if dtype.startswith("datetime"):
            s = df[col]
            if pd.api.types.is_numeric_dtype(s) or s.isna().all():
                s = pd.to_datetime(pd.to_numeric(s, errors="coerce").astype("float64"), unit="s", utc=True)
            df[col] = pd.to_datetime(s, utc=True).astype(dtype)
        else:
            df[col] = df[col].astype(dtype)
    bad = set(df["redemption_type"].dropna()) - set(REDEMPTION_TYPES)
    if bad:
        raise ValueError(f"invalid redemption_type values: {bad}")
    return df


def empty() -> pd.DataFrame:
    return conform(pd.DataFrame())
