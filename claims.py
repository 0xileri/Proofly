"""Conservative detection of *completed* payment claims in an assistant reply.

This is deliberately narrow. A sentence counts as a success claim only when it has a completion
word AND a payment word, and none of: a negation or failure word, a hypothetical / future / request
form, reported speech, a question, a quote, or a code block. Anything unclear is left alone; the
caller appends a status note in a correlated payment context instead of rewriting.

It is only ever applied to replies that already have a payment tool call in the same turn. It is
never a global regex over all prose. No Hermes imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TX_HASH_IN_TEXT = re.compile(r"(?<![0-9a-zA-Z])0x[0-9a-fA-F]{64}(?![0-9a-zA-Z])")

_COMPLETION = re.compile(
    r"\b(sent|transferred|paid|completed?|succeeded|successful(?:ly)?|success|confirmed|went through|"
    r"processed|settled|delivered|executed|landed|finished)\b|✅", re.I)
_PAYMENT = re.compile(
    r"\b(payment|transfer|transaction|tx|txn|usdc|eth|ether|wei|funds|tokens?|money)\b|0x[0-9a-fA-F]{8,}"
    r"|\$\s?\d", re.I)
_NEGATION = re.compile(
    r"\b(not|never|failed|fails|failure|fail|unable|cannot|unsuccessful(?:ly)?|reverted|revert|pending|"
    r"unverified|unconfirmed|mismatch(?:ed)?|unclear|unknown|couldn't|didn't|wasn't|isn't|hasn't|"
    r"haven't|won't|aren't|doesn't|don't)\b|n't\b|❌|⚠", re.I)
_HYPOTHETICAL = re.compile(
    r"\b(should|would|could|can|will|shall|might|may|must|need(?:s)? to|want(?:s)? to|going to|"
    r"please|let's|lets|ready to|about to|plan to|trying to|try to)\s+(?:\w+\s+){0,3}?"
    r"(send|pay|transfer|sent|paid|transferred|be|confirm|process|complete|go)\b"
    r"|\b(if|once|when|whether|unless|after)\s+(?:the\s+|it\s+|you\s+|i\s+|we\s+)"
    r"|\b(to (send|pay|transfer))\b", re.I)
_REPORTED = re.compile(r"\b(said|says|claimed|claims|claiming|according to|reported|reports|told|tells|"
                       r"wrote|writes|stated|states|quote[sd]?)\b", re.I)
_SENTENCE = re.compile(r".+?(?:[.!?]+(?=\s|$)|$)")


@dataclass(frozen=True)
class Claim:
    start: int
    end: int
    text: str
    hashes: tuple[str, ...]


def _segments(text: str):
    """Yield ``(start, end, sentence, in_code_or_quote)`` over the reply, keeping offsets exact."""
    offset = 0
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        line_start = offset
        offset += len(line)
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        quoted = stripped.startswith(">")
        body = line.rstrip("\r\n")
        for match in _SENTENCE.finditer(body):
            sentence = match.group(0)
            if not sentence.strip():
                continue
            yield line_start + match.start(), line_start + match.end(), sentence, quoted


def _is_quoted(sentence: str) -> bool:
    s = sentence.strip()
    pairs = [('"', '"'), ("“", "”"), ("'", "'"), ("`", "`")]
    return any(s.startswith(a) and s.rstrip(".!?").endswith(b) and len(s) > 2 for a, b in pairs)


def is_success_claim(sentence: str) -> bool:
    s = sentence.strip()
    if not s or s.endswith("?"):
        return False
    if not _COMPLETION.search(s) or not _PAYMENT.search(s):
        return False
    if _NEGATION.search(s) or _HYPOTHETICAL.search(s) or _REPORTED.search(s) or _is_quoted(s):
        return False
    return True


def find_success_claims(text: str) -> list[Claim]:
    claims = []
    for start, end, sentence, quoted in _segments(text or ""):
        if quoted:
            continue
        if is_success_claim(sentence):
            hashes = tuple(h.lower() for h in TX_HASH_IN_TEXT.findall(sentence))
            claims.append(Claim(start, end, sentence, hashes))
    return claims


def tx_hashes_in(text: str, limit: int = 8) -> list[str]:
    """Distinct full tx hashes in order of appearance (bounded)."""
    seen: list[str] = []
    for h in TX_HASH_IN_TEXT.findall(text or ""):
        h = h.lower()
        if h not in seen:
            seen.append(h)
        if len(seen) >= limit:
            break
    return seen


def replace_spans(text: str, replacements: list[tuple[int, int, str]]) -> str:
    """Replace non-overlapping ``(start, end, new)`` spans; everything else is kept byte-for-byte."""
    out, cursor = [], 0
    for start, end, new in sorted(replacements):
        if start < cursor:
            continue
        segment = text[start:end]
        leading = segment[: len(segment) - len(segment.lstrip())] if new else ""
        out.append(text[cursor:start])
        out.append(leading + new)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)
