import pandas as pd
import pytest
from conftest import token
from pydantic import ValidationError

from rwa_redeems import schema
from rwa_redeems.config import AppConfig
from rwa_redeems.pipeline import _price_consistency


def test_contract_price_source_override(config):
    mwin = token(config, "mWIN")
    dialectic = next(c for c in mwin.contracts_for("vaults") if c.name == "dialectic")
    main = next(c for c in mwin.contracts_for("vaults") if c.name == "main")
    assert mwin.price_sources_for(dialectic.address)[0].name == "midas_mwin_dialectic_aggregator"
    assert mwin.price_sources_for(main.address) == mwin.price_sources
    assert mwin.from_block(main) == main.from_block  # later than token start_block


def _token(**over):
    base = {
        "symbol": "X", "protocol": "superstate", "chain": "ethereum", "address": "0x" + "1" * 40,
        "decimals": 6, "start_block": 1, "price_sources": [{"type": "constant", "value": 1.0}],
    }
    return base | over


def test_config_validation():
    chains = {"ethereum": {"chain_id": 1, "rpc_url": "http://x"}}
    with pytest.raises(ValidationError, match="unknown chain"):
        AppConfig(chains=chains, tokens=[_token(chain="nope")])
    with pytest.raises(ValidationError, match="not an EVM address"):
        AppConfig(chains=chains, tokens=[_token(address="0x123")])
    with pytest.raises(ValidationError):
        AppConfig(chains=chains, tokens=[_token(price_sources=[{"type": "magic"}])])


def test_schema_conform_rejects_unknown_type():
    with pytest.raises(ValueError, match="redemption_type"):
        schema.conform(pd.DataFrame({"redemption_type": ["burn"]}))
    assert list(schema.empty().columns) == list(schema.COLUMNS)


def test_price_consistency_exact_match():
    df = pd.DataFrame(
        {
            "redemption_type": ["instant"], "contract_name": ["r"], "token_amount": [10.0], "fee_token_amount": [0.0],
            "token_out_amount": [109.0], "token_out_fee_amount": [1.0], "token_out_usd": [109.0], "price_usd": [11.0],
        }
    )
    assert _price_consistency(df)["r"]["max_abs_rel_diff"] < 1e-12
