"""Deterministic walkthrough of the three Proofly demo cases, without a model or Hermes.

    python demo.py           # offline: chain data from tests/fixtures/chain (recorded from Base mainnet)
    python demo.py --live    # same flow, but every check goes to real Base mainnet RPC
    python demo.py --live --rpc https://base-rpc.publicnode.com

What is simulated and what is real:
  * SIMULATED: the payment tool (it moves no funds and always says "success") and the model replies
    (scripted, worst-case "I sent it" claims).
  * REAL: the transaction hashes (historical Base mainnet txs, see tests/fixtures/chain/PROVENANCE.json),
    and Proofly's hook callbacks, verification logic and output, exactly as Hermes would invoke them.

This explains the logic. The official demo runs inside a real Hermes session (README "Demo").
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path / "__init__.py", submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_transport():
    chain = {}
    for name in ("reverted", "usdc_transfer_30", "usdc_transfer_other", "eth_transfer"):
        data = json.loads((ROOT / "tests" / "fixtures" / "chain" / f"{name}.json").read_text())
        chain[data["transaction"]["hash"]] = (data["transaction"], data["receipt"])
    head = 55530464 + 100

    def transport(url, body, timeout):
        req = json.loads(body)
        method, params = req["method"], req["params"]
        result = {"eth_chainId": "0x2105", "eth_blockNumber": hex(head)}.get(method)
        if method == "eth_getTransactionByHash":
            result = chain.get(params[0], (None, None))[0]
        elif method == "eth_getTransactionReceipt":
            result = chain.get(params[0], (None, None))[1]
        return json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
    return transport


CASES = [
    ("A", "False success", 5_000_000,
     "Done! I've sent 5 USDC to {to}. The payment was successful, transaction {tx}."),
    ("B", "Wrong payment", 10_000_000,
     "Sent 10 USDC to {to}. It went through, transaction {tx}."),
    ("C", "Correct payment", 30_000_000,
     "I've sent 30 USDC to {to}. Transaction hash: {tx}."),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="query real Base mainnet RPC instead of fixtures")
    parser.add_argument("--rpc", default="https://mainnet.base.org", help="RPC URL for --live")
    parser.add_argument("--mode", choices=["enforce", "warn"], default="enforce")
    args = parser.parse_args(argv)

    _load("proofly", ROOT)
    wallet = _load("proofly_demo_wallet", ROOT / "examples" / "demo-wallet")
    from proofly.hooks import ProoflyRuntime, Settings
    from proofly.ledger import Ledger

    settings = Settings(rpc_url=args.rpc, mode=args.mode, payment_tools=("demo_send_payment",))
    ledger = Ledger(lambda: sqlite3.connect(":memory:", check_same_thread=False))
    runtime = ProoflyRuntime(settings, ledger_factory=lambda: ledger,
                             transport=None if args.live else _fixture_transport())
    source = f"LIVE Base mainnet RPC ({args.rpc})" if args.live else "recorded Base mainnet fixtures (offline)"
    print(f"Proofly demo | mode={args.mode} | chain data: {source}")
    print("SIMULATED: payment tool + model replies.  REAL: tx hashes, Proofly hooks and verdicts.\n")

    for index, (case, title, amount, claim) in enumerate(CASES, 1):
        session, turn, call = "demo-session", f"turn-{case}", f"call-{case}"
        tool_args = {"to_address": wallet.RECIPIENT, "amount_raw": str(amount), "token": "USDC"}
        tool_result = wallet.send_payment(tool_args)
        tx = json.loads(tool_result)["tx_hash"]
        print(f"=== Case {case}: {title} ===")
        print(f"user  > send {amount / 1e6:g} USDC to {wallet.RECIPIENT}")
        print(f"tool  < {json.loads(tool_result)['message']} tx {tx}")
        hook_kwargs = dict(tool_name="demo_send_payment", args=tool_args, result=tool_result, session_id=session,
                           turn_id=turn, tool_call_id=call, task_id="demo", status="ok")
        runtime.on_post_tool_call(**hook_kwargs)
        annotated = json.loads(runtime.on_transform_tool_result(**hook_kwargs))
        verification = annotated["proofly_verification"]
        print(f"proofly (transform_tool_result) -> {verification['label']}: {verification['reason']}")
        model_reply = claim.format(to=wallet.RECIPIENT, tx=tx)
        print(f"model > {model_reply}   [scripted worst case]")
        final = runtime.on_transform_llm_output(response_text=model_reply, session_id=session, turn_id=turn,
                                                model="scripted", platform="cli")
        print("user sees (transform_llm_output, hold_stream):")
        print(textwrap.indent(final or model_reply, "  | "))
        print()

    from proofly import commands
    print("=== /proofly ===")
    print(commands.handle(runtime, "", session_id="demo-session"))
    ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
