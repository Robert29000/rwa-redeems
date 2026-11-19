from conftest import token

from rwa_redeems.protocols.centrifuge import CentrifugeAdapter

V2 = "0xe9d1f733f406d4bbbdfac6d4cfcd2e13a6ee1d01"
USDC_VAULT = "0x4880799ee5200fc58da299e965df644fbf46780b"
USDT_VAULT = "0x4a76fb02421b1d38aaa40db541f59b2cb04b0770"
ROUTER = "0xf684014771c01e50b8b526968b3a1e33acda63f6"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _by_tx(df, prefix):
    rows = df[df["tx_hash"].str.startswith(prefix)]
    assert len(rows) == 1
    return rows.iloc[0]


def test_jaaa_requests(config, make_ctx, fixture_logs):
    result = CentrifugeAdapter(make_ctx(fixture_logs)).collect(token(config, "JAAA"))
    df = result.redemptions
    assert set(df["redemption_type"]) == {"request"} and set(df["method"]) == {"request_redeem"}

    # legacy v2 vault: last event field is named `assets` in its ABI but holds the shares sent to escrow
    v2 = _by_tx(df, "0x7fa522f1")
    assert (v2.contract, v2.contract_name, v2.token_out, v2.token_out_symbol) == (V2, "usdc_v2", USDC, "USDC")
    assert v2.token_amount_raw == "4495987000000" and v2.token_amount == 4_495_987
    assert v2.holder == v2.caller and v2.request_id == 0

    # VaultRouter request: sender is the router, holder is the owner
    routed = _by_tx(df, "0xddef0328")
    assert routed.contract == USDC_VAULT and routed.caller == ROUTER and routed.holder != ROUTER

    usdt = _by_tx(df, "0xd9297aad")
    assert (usdt.contract, usdt.token_out_symbol) == (USDT_VAULT, "USDT")

    # lifecycle events are counted, not turned into rows (claims of the routed request)
    usdc_checks = result.checks["vaults"]["usdc"]
    assert usdc_checks["redeem_requests"] == 2 and usdc_checks["sender_not_owner"] == 1
    assert usdc_checks["RedeemClaimable"] == 1 and usdc_checks["Withdraw"] == 1
    assert set(result.checks["cancellations"].values()) == {0}
    assert result.checks["unconfigured_vaults"] == []
    assert len(result.checks["vault_updates"]) == 13
    assert result.checks["not_collected"]


def test_unconfigured_linked_vault_is_reported(config, make_ctx, fixture_logs):
    jaaa = token(config, "JAAA")
    vaults = [v for v in jaaa.contracts["vaults"] if v.name != "usdt"]
    jaaa = jaaa.model_copy(update={"contracts": jaaa.contracts | {"vaults": vaults}})
    result = CentrifugeAdapter(make_ctx(fixture_logs)).collect(jaaa)
    assert result.checks["unconfigured_vaults"] == [USDT_VAULT]
    assert USDT_VAULT not in set(result.redemptions["contract"])
