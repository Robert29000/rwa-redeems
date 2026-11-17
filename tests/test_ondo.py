import pandas as pd
from conftest import token

from rwa_redeems.protocols.ondo import FIAT_TOKEN, OndoAdapter

INSTANT_MANAGER = "0xa42613c243b67bf6194ac327795b926b4b491f15"
USDY_MANAGER = "0x25a103a1d6aec5967c1a4fe2039cdc514886b97e"


def _by_tx(df, prefix):
    rows = df[df["tx_hash"].str.startswith(prefix)]
    assert len(rows) == 1
    return rows.iloc[0]


def test_usdy_instant(config, make_ctx, fixture_logs):
    result = OndoAdapter(make_ctx(fixture_logs)).collect(token(config, "USDY"))
    df = result.redemptions

    plain = _by_tx(df, "0xf8d7d4e0")
    assert (plain.contract, plain.redemption_type, plain.method) == (INSTANT_MANAGER, "instant", "redeem")
    assert plain.token_out_symbol == "USDC" and plain.token_out_amount > 0
    assert plain.token_out_usd == plain.token_out_amount and plain.token_out_fee_amount == 0
    assert plain.holder == plain.caller == plain.recipient

    # rUSDY in: one row from Redemption (USDY amount), flagged by InstantRedemptionRebasingUSDY in the same tx
    rebasing = _by_tx(df, "0x601ae27b")
    assert (rebasing.redemption_type, rebasing.method) == ("instant", "redeem_rebasing_usdy")

    checks = result.checks["instant_managers"]["instant_manager"]
    assert checks["rebasing_unmatched"] == 0 and checks["rebasing_rusdy_in"] > checks["rebasing_usdy_in"] > 0


def test_usdy_requests(config, make_ctx, fixture_logs):
    df = OndoAdapter(make_ctx(fixture_logs)).collect(token(config, "USDY")).redemptions

    first = _by_tx(df, "0x9cdb79af")
    assert (first.contract, first.redemption_type, first.method) == (
        USDY_MANAGER, "request", "request_redemption_serviced_offchain"
    )
    assert first.request_id == 1 and first.token_amount > 0
    assert (first.token_out, first.token_out_symbol) == (FIAT_TOKEN, "FIAT") and pd.isna(first.token_out_amount)

    last = _by_tx(df, "0x2c7580b2")
    assert last.request_id > first.request_id
