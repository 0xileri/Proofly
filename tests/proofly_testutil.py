"""Shared test helpers: load the plugin directory as the package ``proofly`` (the same namespaced,
relative-import shape Hermes uses), plus a fake JSON-RPC node built from saved fixtures."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chain"


def load_plugin():
    if "proofly" in sys.modules:
        return sys.modules["proofly"]
    spec = importlib.util.spec_from_file_location(
        "proofly", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["proofly"] = module
    spec.loader.exec_module(module)
    return module


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeNode:
    """In-memory JSON-RPC 2.0 node. ``txs`` maps hash -> (transaction|None, receipt|None)."""

    def __init__(self, *, chain_id="0x2105", head=55530464 + 100, txs=None):
        self.chain_id = chain_id
        self.head = head
        self.txs = dict(txs or {})
        self.calls: list[str] = []
        self.override = None  # callable(method, request) -> bytes, to inject transport faults

    def add(self, name: str):
        data = fixture(name)
        self.txs[data["transaction"]["hash"]] = (data["transaction"], data["receipt"])
        return data["transaction"]["hash"]

    def __call__(self, url: str, body: bytes, timeout: float) -> bytes:
        request = json.loads(body)
        method, params = request["method"], request["params"]
        self.calls.append(method)
        if self.override is not None:
            out = self.override(method, request)
            if out is not None:
                return out
        if method == "eth_chainId":
            result = self.chain_id
        elif method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getTransactionByHash":
            result = self.txs.get(params[0].lower(), (None, None))[0]
        elif method == "eth_getTransactionReceipt":
            result = self.txs.get(params[0].lower(), (None, None))[1]
        else:
            return json.dumps({"jsonrpc": "2.0", "id": request["id"],
                               "error": {"code": -32601, "message": "method not found"}}).encode()
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
