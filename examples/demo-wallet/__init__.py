"""SIMULATED demo wallet for the Proofly demo. Never sends funds.

``demo_send_payment`` always *claims* success, the way a buggy or over-optimistic payment tool
might. The hashes are real, historical Base mainnet transactions (see the Proofly repo's
``tests/fixtures/chain/PROVENANCE.json``), so Proofly's checks against Base are genuine:

* 5 USDC  -> 0x716a...be20, which REVERTED onchain                  (demo case A: false success)
* 10 USDC -> 0xbc12...8044, which paid 2.115636 USDC to another wallet (demo case B: wrong payment)
* 30 USDC -> 0xaea5...9070, which paid exactly 30 USDC to 0x77b0...5ab2 (demo case C: correct)

Only the recipient 0x77b0d999195a301477494de535720b58fd085ab2 is scripted.
"""

import json

RECIPIENT = "0x77b0d999195a301477494de535720b58fd085ab2"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SCRIPT = {
    5_000_000: "0x716a6f5fdf191d72233bd8c1b5ddb0ee1de6aae95cb5bfaba566150830cebe20",
    10_000_000: "0xbc124c469915c21441056b911c75bf359f3d7082461f2fd64850f62499e58044",
    30_000_000: "0xaea5caf798ef5492f22d078e96e5a563357188bc6962fcf5cc7069d6a4b19070",
}
NOTICE = ("SIMULATED demo wallet: no funds were moved. The tx hash is a pre-recorded historical Base "
          "mainnet transaction used to demonstrate verification.")

SCHEMA = {
    "name": "demo_send_payment",
    "description": "Send a USDC payment on Base mainnet from the demo wallet and return the transaction hash.",
    "parameters": {
        "type": "object",
        "properties": {
            "to_address": {"type": "string", "description": "Recipient address (0x + 40 hex)."},
            "amount_raw": {"type": "string",
                           "description": "Amount in USDC base units as an integer string (1 USDC = 1000000)."},
            "token": {"type": "string", "description": "Token symbol. Only USDC is supported."},
        },
        "required": ["to_address", "amount_raw", "token"],
    },
}


def send_payment(args, **kwargs):
    args = args if isinstance(args, dict) else {}
    to = str(args.get("to_address", "")).strip().lower()
    token = str(args.get("token", "")).strip().upper()
    try:
        amount = int(str(args.get("amount_raw", "")).strip())
    except ValueError:
        amount = -1
    tx_hash = SCRIPT.get(amount) if to == RECIPIENT and token == "USDC" else None
    if tx_hash is None:
        return json.dumps({"status": "error", "simulation_notice": NOTICE,
                           "message": "The demo wallet only has scripted payments of 5, 10 or 30 USDC "
                                      f"to {RECIPIENT}."})
    return json.dumps({
        "status": "success",
        "message": f"Payment sent successfully: {amount / 1_000_000:g} USDC to {to}.",
        "tx_hash": tx_hash,
        "token_address": USDC,
        "simulation_notice": NOTICE,
    })


def register(ctx):
    ctx.register_tool(name="demo_send_payment", toolset="proofly_demo", schema=SCHEMA, handler=send_payment,
                      description="SIMULATED demo wallet (no funds move)", emoji="\U0001f9ea")
