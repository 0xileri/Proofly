import unittest

from proofly_testutil import load_plugin

load_plugin()
from proofly import intent as I  # noqa: E402

RECIPIENT = "0x77b0d999195a301477494de535720b58fd085ab2"
USDC = I.BASE_USDC


class AddressAndAmountTests(unittest.TestCase):
    def test_normalize_address(self):
        self.assertEqual(I.normalize_address(RECIPIENT.upper().replace("0X", "0x")), RECIPIENT)
        self.assertEqual(I.normalize_address("  " + RECIPIENT + " "), RECIPIENT)
        for bad in (RECIPIENT[:-1], RECIPIENT + "a", RECIPIENT[2:], "0x" + "g" * 40, None, 5, ""):
            self.assertIsNone(I.normalize_address(bad), bad)

    def test_parse_amount_raw_is_exact(self):
        self.assertEqual(I.parse_amount_raw(30_000_000), 30_000_000)
        self.assertEqual(I.parse_amount_raw("30000000"), 30_000_000)
        self.assertEqual(I.parse_amount_raw(" 42 "), 42)
        self.assertEqual(I.parse_amount_raw(str(2**256 - 1)), 2**256 - 1)
        for bad in (30.0, True, False, -1, 0, "0", "1e6", "10.5", "-5", "0x10", None, str(2**256), ""):
            self.assertIsNone(I.parse_amount_raw(bad), bad)

    def test_amount_from_decimal(self):
        self.assertEqual(I.amount_from_decimal("30", 6), 30_000_000)
        self.assertEqual(I.amount_from_decimal("30.5", 6), 30_500_000)
        self.assertEqual(I.amount_from_decimal("0.000001", 6), 1)
        self.assertEqual(I.amount_from_decimal("1.5", 18), 1_500_000_000_000_000_000)
        self.assertIsNone(I.amount_from_decimal("0.0000001", 6))  # not representable: refuse, no rounding
        self.assertIsNone(I.amount_from_decimal("1", True))
        self.assertIsNone(I.amount_from_decimal("1", 99))
        self.assertIsNone(I.amount_from_decimal(1.5, 6))
        self.assertIsNone(I.amount_from_decimal("1,5", 6))

    def test_format_amount(self):
        self.assertEqual(I.format_amount(30_000_000, I.ERC20, USDC), "30 USDC (raw 30000000)")
        self.assertEqual(I.format_amount(2_115_636, I.ERC20, USDC), "2.115636 USDC (raw 2115636)")
        self.assertIn("ETH (raw 1185524242299554 wei)", I.format_amount(1185524242299554, I.NATIVE_ETH, None))
        self.assertIn("raw units of token", I.format_amount(5, I.ERC20, "0x" + "1" * 40))
        big = 2**256 - 1
        self.assertIn(str(big), I.format_amount(big, I.ERC20, USDC))


class ParseIntentTests(unittest.TestCase):
    def test_valid_erc20(self):
        intent, err = I.parse_intent({"type": "erc20", "to_address": RECIPIENT, "amount_raw": "30000000",
                                      "token_address": USDC}, source="t")
        self.assertIsNone(err)
        self.assertEqual((intent.type, intent.to_address, intent.amount_raw, intent.token_address),
                         ("erc20", RECIPIENT, 30_000_000, USDC))
        self.assertEqual(intent.to_dict()["amount_raw"], "30000000")

    def test_rejections(self):
        cases = {
            "intent_not_an_object": "nope",
            "intent_type_invalid": {"type": "erc721", "to_address": RECIPIENT, "amount_raw": 1},
            "intent_chain_unsupported": {"type": "native_eth", "to_address": RECIPIENT, "amount_raw": 1, "chain_id": 1},
            "intent_to_address_invalid": {"type": "native_eth", "to_address": "bob.eth", "amount_raw": 1},
            "intent_amount_invalid": {"type": "native_eth", "to_address": RECIPIENT, "amount_raw": 1.0},
            "intent_token_address_invalid": {"type": "erc20", "to_address": RECIPIENT, "amount_raw": 1},
            "intent_native_has_token": {"type": "native_eth", "to_address": RECIPIENT, "amount_raw": 1,
                                        "token_address": USDC},
            "intent_from_address_invalid": {"type": "native_eth", "to_address": RECIPIENT, "amount_raw": 1,
                                            "from_address": "0x12"},
        }
        for expected, data in cases.items():
            intent, err = I.parse_intent(data, source="t")
            self.assertIsNone(intent, expected)
            self.assertEqual(err, expected)

    def test_chain_bool_rejected(self):
        _, err = I.parse_intent({"type": "native_eth", "to_address": RECIPIENT, "amount_raw": 1, "chain_id": True},
                                source="t")
        self.assertEqual(err, "intent_chain_unsupported")


class PaymentArgsAdapterTests(unittest.TestCase):
    def test_usdc_symbol_and_key_aliases(self):
        for key in ("to_address", "to", "recipient", "recipient_address"):
            intent, err = I.intent_from_payment_args({key: RECIPIENT, "amount_raw": "30000000", "token": "usdc"})
            self.assertIsNone(err, key)
            self.assertEqual(intent.token_address, USDC)
            self.assertEqual(intent.source, "payment_tool_args")

    def test_native_variants(self):
        for token in (None, "ETH", "native", "eth"):
            args = {"to": RECIPIENT, "amount_raw": 7}
            if token:
                args["token"] = token
            intent, err = I.intent_from_payment_args(args)
            self.assertIsNone(err)
            self.assertEqual(intent.type, I.NATIVE_ETH)

    def test_amount_plus_decimals(self):
        intent, err = I.intent_from_payment_args({"to": RECIPIENT, "amount": "30", "decimals": 6, "token": USDC})
        self.assertIsNone(err)
        self.assertEqual(intent.amount_raw, 30_000_000)

    def test_refusals(self):
        self.assertEqual(I.intent_from_payment_args({"to": RECIPIENT, "amount_raw": 1, "token": "DAI"})[1],
                         "intent_token_unsupported")
        self.assertEqual(I.intent_from_payment_args({"to": RECIPIENT, "amount": "10", "token": "USDC"})[1],
                         "intent_amount_missing_or_inexact")  # decimal amount without decimals: no guessing
        self.assertEqual(I.intent_from_payment_args({"to": RECIPIENT, "amount_raw": 1, "chain_id": 1})[1],
                         "intent_chain_unsupported")
        self.assertEqual(I.intent_from_payment_args({"amount_raw": 1})[1], "intent_to_address_missing_or_invalid")
        self.assertEqual(I.intent_from_payment_args({"to": RECIPIENT, "amount_raw": 1, "from": "x"})[1],
                         "intent_from_address_invalid")
        self.assertEqual(I.intent_from_payment_args(None)[1], "args_not_an_object")
        intent, err = I.intent_from_payment_args({"to": RECIPIENT, "amount_raw": 1, "chain": "base"})
        self.assertIsNone(err)

    def test_command_tokens(self):
        intent, err = I.intent_from_command_tokens([f"to={RECIPIENT}", "amount_raw=30000000", "token=usdc"])
        self.assertIsNone(err)
        self.assertEqual(intent.source, "user_command")
        self.assertEqual(I.intent_from_command_tokens([]), (None, None))
        self.assertTrue(I.intent_from_command_tokens(["amount=5"])[1].startswith("unknown_key"))
        self.assertTrue(I.intent_from_command_tokens(["garbage"])[1].startswith("unrecognized_argument"))


if __name__ == "__main__":
    unittest.main()
