import dataclasses

import pandas as pd
from conftest import token

from rwa_redeems.protocols.securitize import ZERO, SecuritizeAdapter

BUIDL = "0x7712c34205737192402172409a8f7ccef8aa2aec"
WALLET = "0x8780dd016171b91e4df47075da0a947959c34200"
REDEMPTION_USDC = "0x31d3f59ad4aac0eee2247c65ebe8bf6e9e470a53"
REDEMPTION_USDTB = "0x4580d3c5fff079bde60af9e98b62d4fb031ee0a0"
SETTLEMENT_USDC = "0x57dd4e92712b0fbc8d3f3e3645eebcf2600acef0"
SETTLEMENT_USDTB = "0x6c97c9754880e569c8f098b3cc654089f7ac5fbb"
OFFRAMP = "0x04fb23ef3cb3eb559d0ba096fbfda90241ff8e66"
CIRCLE_LP = "0xcf64a444c3aaa68d7bcbf353c7f3f654b139729e"  # USDC settlement recipient (set by RecipientUpdated)
LAUNCH_LP = "0x9d9affc9b217ef2276d3d1781dbda02fcb1c4582"  # USDC settlement recipient during launch week
RIPPLE_LP = "0x0cb4a346edd1b0c1f7b3ee8b1879630a1cc0b78b"  # off-ramp liquidity wallet (two-step: holder -> off-ramp -> LP)
USDTB_LP = "0xc704beced0c5ac8844ca0c42738a5efb932e16bb"  # USDtb settlement recipient, set in the constructor (no event)


def _by_tx(df, prefix):
    rows = df[df["tx_hash"].str.startswith(prefix)]
    assert len(rows) == 1
    return rows.iloc[0]


def _collect(config, make_ctx, logs):
    return SecuritizeAdapter(make_ctx(logs)).collect(token(config, "BUIDL"))


def test_buidl_classification(config, make_ctx, fixture_logs):
    result = _collect(config, make_ctx, fixture_logs)
    df = result.redemptions

    # request: plain transfer to the redemption wallet
    req = _by_tx(df, "0x2d99e530")
    assert (req.redemption_type, req.method, req.contract) == ("request", "transfer_to_redemption_wallet", BUIDL)
    assert req.holder == req.caller and pd.isna(req.token_out)

    # instant via legacy USDC Redemption.sol: row from the settlement's SettlementCompleted, 1:1 in 6 decimals
    usdc = _by_tx(df, "0x78da2a43")
    assert (usdc.redemption_type, usdc.method, usdc.contract) == ("instant", "settlement_completed", REDEMPTION_USDC)
    assert usdc.token_out_symbol == "USDC" and usdc.token_amount == usdc.token_out_amount == 25.0

    # same path through an integrator contract (Ondo OUSGInstantManager): the integrator is the redeemer
    ondo = _by_tx(df, "0x446df67f")
    assert ondo.holder == "0x2826989983e3a66f0622132d019c2ae173eb6a43"

    # instant via USDtb Redemption.sol: payout in 18 decimals, BUIDL amount in 6
    usdtb = _by_tx(df, "0xe8fb2db1")
    assert usdtb.contract == REDEMPTION_USDTB and usdtb.token_out_symbol == "USDtb"
    assert usdtb.token_amount == usdtb.token_out_amount == 1.0 and usdtb.token_amount_raw == "1000000"

    # instant via SecuritizeOffRamp
    off = _by_tx(df, "0x0816e1eb")
    assert (off.redemption_type, off.method, off.contract) == ("instant", "redemption_completed", OFFRAMP)
    assert off.token_out_symbol == "RLUSD" and off.token_out_fee_amount == 0
    assert off.token_out_amount == off.token_amount

    assert result.checks["instant_untraced"] == 0


def test_liquidity_sweeps_are_not_requests(config, make_ctx, fixture_logs):
    result = _collect(config, make_ctx, fixture_logs)
    df = result.redemptions
    requests = df[df["redemption_type"] == "request"]
    assert set(requests["holder"]) & {CIRCLE_LP, LAUNCH_LP, RIPPLE_LP} == set()
    assert len(requests) == 1

    swept = result.checks["excluded_liquidity_sweeps"]
    assert set(swept) == {CIRCLE_LP, LAUNCH_LP, RIPPLE_LP}
    wallets = result.checks["liquidity_wallets"]
    assert wallets[CIRCLE_LP]["settlement_recipient"] and wallets[CIRCLE_LP]["instant_redemptions"] == 1
    assert wallets[LAUNCH_LP]["settlement_recipient"]  # also the receipt trace of the 0x78da2a43 redemption
    assert not wallets[RIPPLE_LP]["settlement_recipient"] and wallets[RIPPLE_LP]["instant_redemptions"] == 1
    assert wallets[USDTB_LP]["instant_redemptions"] == 1


def test_recipient_change_alone_excludes_sweeps(config, make_ctx, fixture_logs):
    """Without any instant redemption to trace, the settlement's RecipientUpdated history still excludes sweeps."""
    no_instant = [lg for lg in fixture_logs if lg.address not in (SETTLEMENT_USDTB, OFFRAMP)]
    no_instant = [lg for lg in no_instant if not (lg.address == SETTLEMENT_USDC and lg.data != "0x")]
    result = _collect(config, make_ctx, no_instant)
    assert result.checks["instant"] == 0
    assert set(result.checks["excluded_liquidity_sweeps"]) == {CIRCLE_LP, LAUNCH_LP}
    assert result.checks["settlements"][0]["recipients"] == [LAUNCH_LP, CIRCLE_LP]


def test_settlements_discovered_from_settlement_updated(config, make_ctx, fixture_logs):
    ctx = make_ctx(fixture_logs)
    result = SecuritizeAdapter(ctx).collect(token(config, "BUIDL"))
    settlements = {s["settlement"]: s for s in result.checks["settlements"]}
    assert set(settlements) == {SETTLEMENT_USDC, SETTLEMENT_USDTB}
    # each settlement is read from the block it was set, not from the token's start block
    calls = {(c[1], c[3]) for c in ctx.fetcher._source.calls}
    assert (SETTLEMENT_USDC, settlements[SETTLEMENT_USDC]["set_at_block"]) in calls
    assert (SETTLEMENT_USDTB, settlements[SETTLEMENT_USDTB]["set_at_block"]) in calls


def test_mint_to_redemption_wallet_is_not_a_request(config, make_ctx, fixture_logs, abis):
    topic0 = abis.event("securitize/DSToken", "Transfer").topic0
    req = next(lg for lg in fixture_logs if lg.address == BUIDL and lg.topics[0] == topic0)
    mint = dataclasses.replace(req, topics=(topic0, "0x" + "0" * 64, req.topics[2]), tx_hash="0x" + "e" * 64)
    result = _collect(config, make_ctx, [mint])
    assert result.redemptions.empty or (result.redemptions["holder"] != ZERO).all()
    assert result.checks["excluded_mints"] == 1 and result.checks["request"] == 0


def test_redemption_wallet_reconciliation(config, make_ctx, fixture_logs):
    rw = _collect(config, make_ctx, fixture_logs).checks["redemption_wallet"]
    assert rw["address"] == WALLET and rw["transfers_in"] == 4 and rw["burns"] == 1


def test_constructor_recipient_from_eth_call(config, make_ctx, fixture_logs, abis):
    """USDtb settlement's recipient was set in the constructor (no RecipientUpdated): read `recipient()` at to_block."""
    fn = abis.function("securitize/Settlement", "recipient")
    calls = {(SETTLEMENT_USDTB, fn.encode()): "0x" + "0" * 24 + USDTB_LP[2:]}
    result = SecuritizeAdapter(make_ctx(fixture_logs, calls=calls)).collect(token(config, "BUIDL"))
    usdtb = next(s for s in result.checks["settlements"] if s["settlement"] == SETTLEMENT_USDTB)
    assert usdtb["recipient_now"] == USDTB_LP and usdtb["recipients"] == [USDTB_LP]
    assert result.checks["liquidity_wallets"][USDTB_LP]["settlement_recipient"]
