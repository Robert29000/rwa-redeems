import dataclasses

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
    assert checks["rebasing_unmatched"] == 0 and checks["rebasing_wrapper_in"] > checks["rebasing_token_in"] > 0


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


OUSG_RECIPIENT = "0x72be8c14b7564f7a61ba2f6b7e50d18dc1d4b63d"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def test_ousg_instant_all_manager_generations(config, make_ctx, fixture_logs):
    result = OndoAdapter(make_ctx(fixture_logs)).collect(token(config, "OUSG"))
    df = result.redemptions

    current = _by_tx(df, "0xbdc59bf6")  # xManager Redemption
    assert (current.contract_name, current.method, current.token_out) == ("instant_manager", "redeem", USDC)
    rebasing = _by_tx(df, "0x69e96f6a")  # Redemption + InstantRedemptionRebasingOUSG in the same tx → one row
    assert (rebasing.contract_name, rebasing.method) == ("instant_manager", "redeem_rebasing_ousg")

    legacy = _by_tx(df, "0x1f1ca864")  # InstantRedemptionOUSG; usdcAmountOut is after RedeemFeesDeducted.usdcFees
    assert (legacy.contract_name, legacy.method, legacy.token_out_symbol) == ("instant_manager_legacy", "redeem", "USDC")
    assert legacy.token_out_fee_amount == 0 and legacy.token_out_usd == legacy.token_out_amount > 0
    legacy_rebasing = _by_tx(df, "0xf780b59e")
    assert legacy_rebasing.method == "redeem_rebasing_ousg"
    v0 = _by_tx(df, "0x0227a62b")  # first, undocumented OUSGInstantManager
    assert (v0.contract_name, v0.method) == ("instant_manager_v0", "redeem_rebasing_ousg")

    managers = result.checks["instant_managers"]
    assert managers["instant_manager_legacy"]["fees_unmatched"] == 0
    assert managers["instant_manager"]["rebasing_unmatched"] == 0


def test_ousg_requests(config, make_ctx, fixture_logs):
    df = OndoAdapter(make_ctx(fixture_logs)).collect(token(config, "OUSG")).redemptions

    hub = _by_tx(df, "0x104fad4f")  # OUSGManager (RWAHub) RedemptionRequested, claimed later in USDC
    assert (hub.contract_name, hub.redemption_type, hub.token_out_symbol) == ("ousg_manager", "request", "USDC")
    assert hub.request_id >= 0
    cash = _by_tx(df, "0xc5494c01")  # CashManager RedemptionRequested(user, cashAmountIn, epoch): no request id
    assert (cash.contract_name, cash.method) == ("cash_manager", "request_redemption") and pd.isna(cash.request_id)
    assert _by_tx(df, "0xe60e1fa4").contract_name == "cash_manager_v0"

    manual = _by_tx(df, "0xd4c888f7")  # holder → OUSG Recipient
    assert (manual.contract, manual.contract_name, manual.method) == (
        OUSG_RECIPIENT, "redemption_wallet", "transfer_to_redemption_wallet"
    )
    assert manual.holder != OUSG_RECIPIENT and manual.token_amount > 0


def test_ousg_wallet_drops_mints(config, make_ctx, fixture_logs, abis):
    transfer = abis.event("ondo/OUSG", "Transfer").topic0
    manual = next(lg for lg in fixture_logs if lg.topics[0] == transfer and lg.topics[2].endswith(OUSG_RECIPIENT[2:]))
    mint = dataclasses.replace(manual, topics=(manual.topics[0], "0x" + "0" * 64, manual.topics[2]), tx_hash="0x" + "e" * 64)
    result = OndoAdapter(make_ctx([manual, mint])).collect(token(config, "OUSG"))
    assert len(result.redemptions) == 1
    assert result.checks["redemption_wallet"]["excluded_mints"] == 1
