"""Trusted transfer intent: parsing, validation and payment-tool argument adapters.

An intent is the structured description of the transfer that was *supposed* to happen. Proofly
only builds one from explicit structured input:

* the arguments an allowlisted payment tool was invoked with (``source="payment_tool_args"``),
* the ``expected_transfer`` object passed to ``proofly_verify_tx`` (``source="verify_tool_args"``),
* ``key=value`` arguments the user typed after ``/proofly check`` (``source="user_command"``).

It never builds one from the assistant's free-text reply, from a tool's success message, or from
a hash that merely appears in the chat. No Hermes imports here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Optional

BASE_CHAIN_ID = 8453
NATIVE_ETH = "native_eth"
ERC20 = "erc20"
INTENT_TYPES = (NATIVE_ETH, ERC20)

# Circle's native USDC on Base mainnet. The only symbol Proofly maps to an address.
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
KNOWN_TOKENS = {BASE_USDC: ("USDC", 6)}
_SYMBOL_TO_TOKEN = {"usdc": BASE_USDC}
_NATIVE_WORDS = {"eth", "native", "native_eth", "ether"}

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DIGITS_RE = re.compile(r"^[0-9]{1,78}$")
MAX_UINT256 = 2**256 - 1


def normalize_address(value: Any) -> Optional[str]:
    """Lower-cased ``0x`` + 40 hex address, or ``None`` when malformed. No EIP-55 checksum check."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _ADDRESS_RE.fullmatch(value):
        return None
    return value.lower()


def parse_amount_raw(value: Any) -> Optional[int]:
    """Exact positive integer in base units (wei / token units). Floats, bools, signs and scientific
    notation are refused, because a lossy amount must never produce a match."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str) and _DIGITS_RE.fullmatch(value.strip()):
        amount = int(value.strip())
    else:
        return None
    if amount <= 0 or amount > MAX_UINT256:
        return None
    return amount


def amount_from_decimal(amount: Any, decimals: Any) -> Optional[int]:
    """``"30.5"`` with ``decimals=6`` -> ``30500000``. Refuses anything that is not exactly
    representable in base units (no rounding)."""
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 36:
        return None
    if isinstance(amount, bool) or not isinstance(amount, (str, int)):
        return None
    text = str(amount).strip()
    if not re.fullmatch(r"[0-9]{1,60}(\.[0-9]{1,36})?", text):
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 200  # exact for every uint256 value
            scaled = Decimal(text) * (Decimal(10) ** decimals)
    except InvalidOperation:
        return None
    if scaled != scaled.to_integral_value():
        return None
    return parse_amount_raw(int(scaled))


@dataclass(frozen=True)
class TransferIntent:
    type: str
    to_address: str
    amount_raw: int
    token_address: Optional[str] = None
    from_address: Optional[str] = None
    chain_id: int = BASE_CHAIN_ID
    source: str = "unknown"

    def fingerprint(self) -> str:
        return "|".join(str(x) for x in (self.chain_id, self.type, self.to_address, self.amount_raw,
                                          self.token_address or "", self.from_address or ""))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["amount_raw"] = str(self.amount_raw)  # JSON-safe for amounts above 2**53
        return data


def parse_intent(data: Any, *, source: str) -> tuple[Optional[TransferIntent], Optional[str]]:
    """Strictly parse an explicit structured intent mapping.

    Returns ``(intent, None)`` or ``(None, reason_code)``.
    """
    if not isinstance(data, Mapping):
        return None, "intent_not_an_object"
    kind = data.get("type")
    if kind not in INTENT_TYPES:
        return None, "intent_type_invalid"
    chain_id = data.get("chain_id", BASE_CHAIN_ID)
    if isinstance(chain_id, bool) or chain_id not in (BASE_CHAIN_ID, str(BASE_CHAIN_ID)):
        return None, "intent_chain_unsupported"
    to_address = normalize_address(data.get("to_address"))
    if to_address is None:
        return None, "intent_to_address_invalid"
    amount = parse_amount_raw(data.get("amount_raw"))
    if amount is None:
        return None, "intent_amount_invalid"
    from_raw = data.get("from_address")
    from_address = None
    if from_raw not in (None, ""):
        from_address = normalize_address(from_raw)
        if from_address is None:
            return None, "intent_from_address_invalid"
    token_raw = data.get("token_address")
    token = None
    if kind == ERC20:
        token = normalize_address(token_raw)
        if token is None:
            return None, "intent_token_address_invalid"
    elif token_raw not in (None, ""):
        return None, "intent_native_has_token"
    return TransferIntent(kind, to_address, amount, token, from_address, BASE_CHAIN_ID, source), None


# ---------------------------------------------------------------------------------------------
# Payment-tool argument adapter
# ---------------------------------------------------------------------------------------------

_TO_KEYS = ("to_address", "to", "recipient", "recipient_address")
_FROM_KEYS = ("from_address", "from", "sender", "sender_address")
_TOKEN_KEYS = ("token_address", "token", "asset")


def _first(args: Mapping, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in args and args[key] not in (None, ""):
            return args[key]
    return None


def intent_from_payment_args(
    args: Any, *, source: str = "payment_tool_args",
) -> tuple[Optional[TransferIntent], Optional[str]]:
    """Map an allowlisted payment tool's structured arguments to an intent.

    Documented convention (README, "Payment tool argument convention"):

    * recipient: ``to_address`` | ``to`` | ``recipient`` | ``recipient_address``
    * amount: ``amount_raw`` (integer base units), or ``amount`` + ``decimals`` (exact)
    * token: ``token_address`` | ``token`` | ``asset``: an ERC-20 address, ``"USDC"`` (Base native
      USDC only), or ``"ETH"`` / ``"native"`` / absent for native ETH
    * sender (optional): ``from_address`` | ``from`` | ``sender``
    * chain (optional): ``chain_id`` must be 8453 if present (``chain: "base"`` also accepted)

    Anything else yields no intent, so the verdict can only be execution-level.
    """
    if not isinstance(args, Mapping):
        return None, "args_not_an_object"
    chain = args.get("chain_id", args.get("chain"))
    if isinstance(chain, bool) or chain not in (
        None, "", BASE_CHAIN_ID, str(BASE_CHAIN_ID), "base", "Base", "base-mainnet",
    ):
        return None, "intent_chain_unsupported"

    to_address = normalize_address(_first(args, _TO_KEYS))
    if to_address is None:
        return None, "intent_to_address_missing_or_invalid"

    amount = None
    if args.get("amount_raw") not in (None, ""):
        amount = parse_amount_raw(args.get("amount_raw"))
    elif args.get("amount") not in (None, "") and args.get("decimals") not in (None, ""):
        amount = amount_from_decimal(args.get("amount"), args.get("decimals"))
    if amount is None:
        return None, "intent_amount_missing_or_inexact"

    token_value = _first(args, _TOKEN_KEYS)
    if token_value is None or (isinstance(token_value, str) and token_value.strip().lower() in _NATIVE_WORDS):
        kind, token = NATIVE_ETH, None
    else:
        token = normalize_address(token_value)
        if token is None and isinstance(token_value, str):
            token = _SYMBOL_TO_TOKEN.get(token_value.strip().lower())
        if token is None:
            return None, "intent_token_unsupported"
        kind = ERC20

    from_value = _first(args, _FROM_KEYS)
    from_address = None
    if from_value is not None:
        from_address = normalize_address(from_value)
        if from_address is None:
            return None, "intent_from_address_invalid"
    return TransferIntent(kind, to_address, amount, token, from_address, BASE_CHAIN_ID, source), None


def intent_from_command_tokens(tokens: list[str]) -> tuple[Optional[TransferIntent], Optional[str]]:
    """``to=0x.. amount_raw=123 token=0x..|usdc|eth from=0x..`` typed by the user after
    ``/proofly check <hash>``. Unknown keys are refused rather than ignored."""
    fields: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            return None, f"unrecognized_argument:{token[:24]}"
        key, value = token.split("=", 1)
        key = key.strip().lower()
        if key not in {"to", "amount_raw", "token", "from"}:
            return None, f"unknown_key:{key[:24]}"
        fields[key] = value.strip()
    if not fields:
        return None, None
    return intent_from_payment_args(
        {"to_address": fields.get("to"), "amount_raw": fields.get("amount_raw"),
         "token": fields.get("token"), "from_address": fields.get("from")},
        source="user_command",
    )


def format_amount(amount_raw: int, intent_type: str, token_address: Optional[str]) -> str:
    """Human amount for known assets; raw base units otherwise (decimals are never guessed)."""
    if intent_type == NATIVE_ETH:
        return f"{_scale(amount_raw, 18)} ETH (raw {amount_raw} wei)"
    known = KNOWN_TOKENS.get((token_address or "").lower())
    if known:
        symbol, decimals = known
        return f"{_scale(amount_raw, decimals)} {symbol} (raw {amount_raw})"
    return f"{amount_raw} raw units of token {token_address}"


def _scale(amount_raw: int, decimals: int) -> str:
    with localcontext() as ctx:
        ctx.prec = 200  # uint256 has 78 digits; never round a displayed amount
        return format((Decimal(amount_raw) / (Decimal(10) ** decimals)).normalize(), "f")
