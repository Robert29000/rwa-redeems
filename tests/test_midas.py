import dataclasses

import pandas as pd
from conftest import token

from rwa_redeems.protocols.midas import FIAT_TOKEN, MidasAdapter

MAIN = "0x605704d7b36d1677a8d242ded68ed505523c7924"
DIALECTIC = "0x078b0858f609beb26098379fb504763766b6f128"


def _by_tx(df, prefix):
    """Exactly one row per tx: also proves other vaults' events in the same tx (swapper fallback) are not counted."""
    rows = df[df["tx_hash"].str.startswith(prefix)]
    assert len(rows) == 1
    return rows.iloc[0]


def test_mwin_classification(config, make_ctx, fixture_logs):
    result = MidasAdapter(make_ctx(fixture_logs)).collect(token(config, "mWIN"))
    df = result.redemptions

    req = _by_tx(df, "0x8a0a7d2b")
    assert (req.redemption_type, req.method, req.contract_name) == ("request", "redeem_request", "main")
    assert req.request_id >= 0 and req.recipient == req.holder
    assert req.token_out_symbol in ("USDC", "PYUSD") and pd.isna(req.token_out_amount)

    inst = _by_tx(df, "0xe8bdfd9f")
    assert (inst.redemption_type, inst.method, inst.contract) == ("instant", "redeem_instant", MAIN)
    assert inst.token_out_amount > 0 and inst.token_out_usd == inst.token_out_amount  # stable payout
    assert inst.via_fallback  # mTBILL vault RedeemInstant with user == main vault in the same tx

    dia = _by_tx(df, "0x2b75a1d9")
    assert dia.contract == DIALECTIC and dia.redemption_type == "instant"
    assert not dia.via_fallback


def test_fiat_request(config, make_ctx, fixture_logs, abis):
    topic0 = abis.event("midas/RedemptionVault", "RedeemRequest").topic0
    req = next(lg for lg in fixture_logs if lg.topics[0] == topic0)
    fiat = dataclasses.replace(
        req,
        topics=(*req.topics[:3], "0x" + "0" * 64),  # tokenOut = address(0)
        tx_hash="0x" + "f" * 64,
    )
    df = MidasAdapter(make_ctx([fiat])).collect(token(config, "mWIN")).redemptions
    row = df.iloc[0]
    assert (row.redemption_type, row.token_out, row.token_out_symbol) == ("fiat_request", FIAT_TOKEN, "FIAT")


MTBILL_MAIN = "0xf6e51d24f4793ac5e71e0502213a9bbe3a6d4517"
MTBILL_USTB = "0x569d7dccbf6923350521ecbc28a555a500c4f0ec"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def test_mtbill_liquidity_sources(config, make_ctx, fixture_logs):
    result = MidasAdapter(make_ctx(fixture_logs)).collect(token(config, "mTBILL"))
    df = result.redemptions

    buidl = _by_tx(df, "0xf2d751f9")  # vault was RedemptionVaultWithBUIDL: BUIDL Settlement SettlementCompleted
    assert (buidl.contract, buidl.redemption_type, buidl.token_out_symbol) == (MTBILL_USTB, "instant", "USDC")
    assert buidl.via_fallback

    no_source = _by_tx(df, "0xb1fd158c")  # same vault, paid from its own USDC
    assert no_source.redemption_type == "instant" and not no_source.via_fallback

    ustb = _by_tx(df, "0x861d680c")  # after the upgrade to RedemptionVaultWithUSTB: Superstate RedeemV2
    assert ustb.via_fallback and ustb.token_out_usd == ustb.token_out_amount


def test_mtbill_requests_and_legacy_payment_tokens(config, make_ctx, fixture_logs):
    result = MidasAdapter(make_ctx(fixture_logs)).collect(token(config, "mTBILL"))
    df = result.redemptions

    custom = _by_tx(df, "0x65d1258d")
    assert (custom.contract, custom.method) == (MTBILL_MAIN, "redeem_request_custom_recipient")
    assert custom.recipient != custom.holder and pd.isna(custom.via_fallback)

    fiat = _by_tx(df, "0x8eb45c69")
    assert (fiat.redemption_type, fiat.token_out_symbol) == ("fiat_request", "FIAT")

    # payment tokens were added with the pre-2025 AddPaymentToken signature (no `allowance`)
    vaults = result.checks["vaults"]
    assert vaults["main"]["payment_tokens"][USDC]["stable"]
    assert USDC in vaults["ustb"]["payment_tokens"]
