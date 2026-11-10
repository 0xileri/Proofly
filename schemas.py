"""Schema of the one read-only tool Proofly registers."""

PROOFLY_VERIFY_TX = {
    "name": "proofly_verify_tx",
    "description": (
        "Read-only check of one Base mainnet (chain 8453) transaction against onchain evidence. "
        "Call it before telling the user that a Base payment or transfer succeeded. "
        "With only tx_hash it reports whether the transaction executed, reverted, is pending or was not "
        "found; that alone never proves the requested payment happened. Add expected_transfer with the "
        "exact recipient, token and integer amount the user asked for to check the actual transfer. "
        "Never sends, signs or approves anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tx_hash": {
                "type": "string",
                "description": "Transaction hash: 0x followed by exactly 64 hex characters.",
            },
            "expected_transfer": {
                "type": "object",
                "description": (
                    "Exact transfer the user requested. Omit it if you do not know these details; "
                    "never guess them from the transaction itself."
                ),
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["native_eth", "erc20"],
                        "description": "native_eth for a direct ETH transfer, erc20 for a token transfer.",
                    },
                    "to_address": {"type": "string", "description": "Recipient address, 0x + 40 hex characters."},
                    "amount_raw": {
                        "type": "string",
                        "description": (
                            "Integer amount in base units as a decimal string: wei for ETH "
                            "(1 ETH = 1000000000000000000), token units for ERC-20 (Base USDC has 6 decimals: "
                            "1 USDC = 1000000)."
                        ),
                    },
                    "token_address": {
                        "type": "string",
                        "description": "ERC-20 contract address (required for erc20, omit for native_eth). "
                                       "Base USDC is 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913.",
                    },
                    "from_address": {"type": "string", "description": "Optional expected sender address."},
                },
                "required": ["type", "to_address", "amount_raw"],
            },
        },
        "required": ["tx_hash"],
    },
}
