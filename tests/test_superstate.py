import pandas as pd
from conftest import token

from rwa_redeems.protocols.superstate import SuperstateAdapter

USTB = "0x43415eb6ff9db7e26a15b704e7a3edce97d31c4e"
REDEMPTION = "0x4c21b7577c8fe8b0b0669165ee7c8f67fa1454cf"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _by_tx(df, prefix):
    rows = df[df["tx_hash"].str.startswith(prefix)]
    assert len(rows) == 1
    return rows.iloc[0]


def test_ustb_classification(config, make_ctx, fixture_logs):
    result = SuperstateAdapter(make_ctx(fixture_logs)).collect(token(config, "USTB"))
    df = result.redemptions

    # transfer-to-token request: OffchainRedeem burner == src == holder
    req = _by_tx(df, "0xe9ead3a7")
    assert (req.redemption_type, req.method, req.contract) == ("request", "offchain_redeem", USTB)
    assert req.holder == req.caller != REDEMPTION
    assert pd.isna(req.token_out)  # no on-chain payout

    # instant via current RedeemV2: holder comes from the redemption contract event
    v2 = _by_tx(df, "0x46c2f7bf")
    assert (v2.redemption_type, v2.method, v2.contract) == ("instant", "redeem_v2", REDEMPTION)
    assert v2.caller == REDEMPTION and v2.holder != REDEMPTION
    assert v2.token_out == USDC and v2.token_out_symbol == "USDC"
    assert v2.token_out_amount > 0 and v2.token_out_usd == v2.token_out_amount

    # instant via legacy Redeem event (redemption impl v2): recipient == redeemer, no fee split
    v1 = _by_tx(df, "0xff79034e")
    assert (v1.redemption_type, v1.method) == ("instant", "redeem_v1")
    assert v1.recipient == v1.holder != REDEMPTION
    assert v1.token_out_fee_amount == 0

    assert result.checks["instant_without_contract_event"] == 0
    assert result.checks["contract_events_without_token_event"] == 0
    assert result.checks["instant"] == 3 and result.checks["request"] == 1


def test_instant_without_contract_event_is_kept(config, make_ctx, fixture_logs):
    logs = [lg for lg in fixture_logs if not (lg.address == REDEMPTION and lg.tx_hash.startswith("0x46c2f7bf"))]
    result = SuperstateAdapter(make_ctx(logs)).collect(token(config, "USTB"))
    row = _by_tx(result.redemptions, "0x46c2f7bf")
    assert row.redemption_type == "instant" and pd.isna(row.holder)
    assert result.checks["instant_without_contract_event"] == 1


def test_start_block_skips_legacy(config, make_ctx, fixture_logs):
    """Collection starts at the configured start_block for token and redemption contract alike."""
    ctx = make_ctx(fixture_logs)
    SuperstateAdapter(ctx).collect(token(config, "USTB"))
    assert {call[3] for call in ctx.fetcher._source.calls} == {token(config, "USTB").start_block}


def test_uscc_requests_only(config, make_ctx, fixture_logs):
    df = SuperstateAdapter(make_ctx(fixture_logs)).collect(token(config, "USCC")).redemptions
    assert list(df["redemption_type"]) == ["request"]
    assert df.iloc[0].tx_hash.startswith("0x3830b83a")
