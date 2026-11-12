# topic0 values verified on mainnet. Guards the ABI JSON files, including events without real fixture logs
# (custom-recipient variants, AddPaymentToken, SetRedemptionContract).
KNOWN_TOPICS = {
    ("superstate/SuperstateToken", "OffchainRedeem"): "0x5380355699fac5266e4d95cf6985cf6a48abe03aa33d07723bdd0338a367af25",
    ("superstate/SuperstateToken", "SetRedemptionContract"): "0x64e7fc3f3d24b9b7ea1e0c7fbe7fdb5a9e9e1aab410fa818accc1d545e809cb8",
    ("superstate/Redemption", "Redeem"): "0xe5b754fb1abb7f01b499791d0b820ae3b6af3424ac1c59768edb53f4ec31a929",
    ("superstate/Redemption", "RedeemV2"): "0x5672b8cf9019e04abf8b13cb4c77d156d0cc231cc52c14f65f00892482beb87a",
    ("midas/RedemptionVault", "RedeemInstant"): "0x1af12536d161c2c30ad907b0abe442f94c4a7824f2463585b3fc893275247cce",
    ("midas/RedemptionVault", "RedeemInstantWithCustomRecipient"): "0x4fd0e2f3f27549d8d0c242f7193eaa0f61546e887fec39e69dfbff6b2384a4c3",
    ("midas/RedemptionVault", "RedeemRequest"): "0x55ba94d231fa70a45e82b0a1c6a60ef72e41bb2455385128ee5cf8d98c0c1c0e",
    ("midas/RedemptionVault", "RedeemRequestWithCustomRecipient"): "0x691cd372bb63a5126a324513b634040d0ba3747c0a625207d99b6ba302c51a23",
    ("midas/RedemptionVault", "AddPaymentToken"): "0x049000a9db89588d7bfb162bc0f7e4299ee8762430a468131c2caf0824f1f995",
}


def test_topic0_derived_from_abi_files(abis):
    derived = {key: abis.event(*key).topic0 for key in KNOWN_TOPICS}
    assert derived == KNOWN_TOPICS
