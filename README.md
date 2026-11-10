# Proofly

**An agent can say a transfer succeeded. Proofly checks the chain.**

Proofly is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that checks an AI
agent's claimed Base mainnet transfers against onchain evidence. When a payment tool returns a
"success" message, or the model tells the user "sent 10 USDC", Proofly asks Base what actually
happened: did the transaction exist, did it execute, and did it move the right token, to the right
recipient, for the right raw amount. If the evidence does not support the claim, Proofly corrects the
tool result before the model reads it and corrects the final reply before the user sees it.

The core rule: `receipt.status == 1` is evidence of successful execution. It is **not** evidence that
the payment the user asked for happened. Proofly only says `VERIFIED TRANSFER ON BASE` when a trusted,
structured description of the expected transfer matches the chain exactly.

Proofly is read-only. It never holds keys, signs, sends, approves or blocks a transaction. It cannot
undo a transfer that was already submitted; it reports what the chain shows.

## What it checks

| Supported | How |
|---|---|
| Chain | Base mainnet only, chain ID 8453. `eth_chainId` must return `0x2105` on every check. |
| Native ETH | Direct transfers only: receipt status 1, empty calldata, `tx.to` equals the expected recipient, `tx.value` equals the expected wei, `tx.from` equals the sender if one is expected. |
| ERC-20 | Standard `Transfer(address,address,uint256)` log (`topic0 = 0xddf252ad...3b3ef`) emitted by the expected token contract (`log.address`), recipient from `topic2`, sender from `topic1` if expected, amount decoded from the 32-byte `data` in raw base units. Decimals are never guessed. |
| Hash only | Without a trusted intent Proofly reports execution state (executed, reverted, pending, not found) and never upgrades it to a verified payment. |

## Verdicts

| Verdict | Label shown | Meaning |
|---|---|---|
| `VERIFIED_TRANSFER` | VERIFIED TRANSFER ON BASE | Successful execution, a matching transfer for the trusted intent, and at least `min_confirmations` blocks (default 3, counting the tx's own block). |
| `EXECUTED` | ONCHAIN EXECUTED - INTENT NOT VERIFIED | Receipt status 1, but no trusted intent was available, so it is not proof of the requested payment. |
| `REVERTED` | TRANSACTION REVERTED | Receipt status 0. Nothing was transferred. |
| `PENDING` | PENDING / NOT YET VERIFIED | Transaction known but no receipt yet, or a matching transfer below the confirmation policy. |
| `NOT_FOUND` | NOT FOUND ON BASE - UNVERIFIED | Neither transaction nor receipt on the node at check time. Not proof of failure. |
| `MISMATCH` | MISMATCH - CLAIM NOT SUPPORTED | The transaction executed but contradicts the intent: wrong recipient, token, raw amount or sender, with the exact observed difference. |
| `RPC_ERROR` | UNVERIFIED - RPC UNAVAILABLE | Timeout, HTTP error, rate limit, redirect, malformed or truncated JSON, wrong chain ID, or responses that disagree with each other (hash or block mismatch). |
| `INVALID_HASH` | INVALID TX HASH - UNVERIFIED | Not `0x` + exactly 64 hex characters. Never reported as an onchain failure. |
| `UNSUPPORTED` | UNSUPPORTED - NOT VERIFIED | Something Proofly will not guess at: a contract call carrying ETH, several matching Transfer logs to the recipient (split or batch), a malformed Transfer log, a deposit (`0x7e`) transaction, a non-Base chain, no tx hash, or several different hashes. |

Proofly never says "finalized". It reports inclusion and a confirmation count only.

## How it plugs into Hermes

```
payment tool call ──► post_tool_call ──────────► record intent (from the tool's arguments) + tx hash,
       │                (observer)                keyed by session_id + turn_id + tool_call_id
       ▼
tool result ───────► transform_tool_result ───► check Base, give the model a result that carries
       │                                          the verdict (only for allowlisted payment tools)
       ▼
model's final reply ► transform_llm_output ───► correct or qualify unsupported success claims,
                       (hold_stream=True)         append the Proofly check; untouched otherwise
```

| Surface | Registered as | Notes |
|---|---|---|
| `post_tool_call` | hook | Observer only, returns nothing. Captures watched tool calls. |
| `transform_tool_result` | hook | Returns `None` for every tool not in `payment_tools`. |
| `transform_llm_output` | hook, `hold_stream=True` | Returns `None` unless the same session and turn called a watched tool. |
| `proofly_verify_tx` | tool (toolset `proofly`) | Read-only check of one hash, with an optional `expected_transfer`. |
| `/proofly` | slash command | Status, session history, explicit `check`, `clear`. |

No `pre_tool_call` hook is registered: the tool arguments arrive with `post_tool_call` and
`transform_tool_result`, so Proofly never sits in front of tool execution. No privileged capability
(`tools.override` or similar) is requested.

Source layout: `verifier.py` (RPC and proof logic, no Hermes imports), `intent.py` (trusted intent and
argument adapters), `claims.py` (claim detection), `render.py` (labels and messages), `hooks.py`
(callbacks and per-turn correlation), `ledger.py` (SQLite), `commands.py` (`/proofly`),
`schemas.py`, `__init__.py` (registration only).

### Where trusted intent comes from

Proofly builds the expected transfer only from explicit, structured input:

1. the arguments an allowlisted payment tool was invoked with (`payment_tools` setting),
2. the `expected_transfer` object passed to `proofly_verify_tx`,
3. `key=value` arguments typed by the user after `/proofly check <hash>`.

It never builds one from the assistant's reply, from a tool's success message, or from a hash that
just appears in the chat. Payment-tool intent describes what the tool was asked to do; Proofly checks
that the chain matches it, not that the tool call matched what the user meant in plain words.

### Payment tool argument convention

A tool listed in `payment_tools` gets an intent when its arguments use these names:

| Field | Accepted keys | Values |
|---|---|---|
| recipient | `to_address`, `to`, `recipient`, `recipient_address` | `0x` + 40 hex |
| amount | `amount_raw`, or `amount` + `decimals` | integer base units (decimal string or int); `amount` + `decimals` is converted exactly or refused, never rounded |
| token | `token_address`, `token`, `asset` | an ERC-20 address, `"USDC"` (Circle's Base USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, the only symbol mapped), or `"ETH"` / `"native"` / absent for native ETH |
| sender (optional) | `from_address`, `from`, `sender` | `0x` + 40 hex |
| chain (optional) | `chain_id` or `chain` | must be 8453 / `base` if present |

The tx hash is read from structured result fields (`tx_hash`, `txHash`, `transactionHash`, `txid`,
top-level `hash`), or from prose fields such as `message`. Several different hashes are treated as
ambiguous. A tool whose arguments do not fit still gets an execution-level check, and the reply notes
that the transfer details could not be checked.

### Correlation and replay safety

Tool calls are recorded per `session_id` and Hermes `turn_id` (the same `agent._current_turn_id` is
passed to tool hooks and to `transform_llm_output`). A reply only sees calls from its own session and
turn; a new turn drops older pending calls; each reply consumes its calls once. A verified hash from
session A or from an earlier turn can never back a claim in session B or in a later turn. Duplicate
deliveries of one tool call (same `tool_call_id`, or the same content without one) are counted once.
Verification results are cached for 60 seconds only for verdicts the chain will not flip in that
window; `PENDING`, `NOT_FOUND` and `RPC_ERROR` are always re-checked.

## Output behaviour

**enforce** (default): a sentence that claims a completed payment is replaced with a factual
correction when the evidence contradicts it or is missing, qualified in place when execution is proven
but the transfer is not (`EXECUTED`, or a match still below the confirmation policy), and kept when the
transfer is verified. Unrelated text is kept byte for byte. A Proofly check block is appended.

**warn**: the reply is kept as written and a prominent warning block is appended.

Claim detection is deliberately narrow: a sentence needs a completion word and a payment word, and is
skipped if it is negated, hypothetical or future ("you should send", "once it is confirmed"), a
question, reported speech ("the tool said..."), quoted, or inside a code block. It only ever runs on
replies whose turn called a watched tool; there is no global regex over normal conversation. Anything
it cannot classify is left alone, and the appended check still states the verdict.

If Proofly fails internally inside a payment context it returns the reply plus an explicit
"treat payment claims as UNVERIFIED" note. It never returns a success label on error. The words
"blocked transfer" are never used. A Basescan link is offered for inspection; it is not the proof.

## Install

Current release: **Proofly 1.0.0** (`version: "1.0.0"` in `plugin.yaml` and in the catalog entry).

Tested on Hermes **v0.23.1** (current stable, commit `3b421578588243f920886e6fe79a6faa87ea026e`) and
**v0.22.6** (commit `f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`), Python 3.14. v0.22.6 is the first
release whose `register_hook` accepts `hold_stream`; on v0.22.5 registration fails with
`register_hook() got an unexpected keyword argument 'hold_stream'`, hence `requires_hermes: ">=0.22.6"`.

```bash
hermes plugins install 0xileri/proofly --enable
```

Tell Proofly which tools send Base payments (only these are checked):

```bash
hermes config set plugins.entries.proofly.settings.payment_tools '["your_send_tool"]'
```

All settings, under `plugins.entries.proofly.settings` in `config.yaml` (also editable in the Desktop
app's plugin settings form):

```yaml
plugins:
  enabled: [proofly]
  entries:
    proofly:
      settings:
        rpc_url: https://mainnet.base.org   # HTTPS only, no user:pass@, must report chain 8453
        mode: enforce                       # or: warn
        payment_tools: [your_send_tool]
        min_confirmations: 3                # 1..1000
        timeout_seconds: 6                  # per RPC request, 1..20
        annotate_tool_results: true         # false: only the final reply is checked
```

An invalid `rpc_url` is not silently replaced: every check then reports `UNVERIFIED - RPC URL
REJECTED` until it is fixed, and `/proofly status` shows the warning. Plain `http://` endpoints,
including localhost, are refused. Redirects are refused. One hook call spends at most
`min(20, 2.5 x timeout + 1)` seconds on RPC, well inside Hermes' 30 second hook timeout.

The public endpoint `https://mainnet.base.org` needs no key and is rate limited. Providers differ on
unknown hashes: `mainnet.base.org` returns `null` (Proofly says `NOT_FOUND`), while
`base-rpc.publicnode.com` answers with an error (Proofly says `UNVERIFIED - RPC UNAVAILABLE`). Both are
reported as unverified, never as failed.

### Visibility: what hold_stream does and does not cover

`transform_llm_output` is registered with `hold_stream=True`, so in every session of a profile that
loads Proofly the user sees each reply only after Proofly has checked it (a typing or thinking
indicator, then the final text). This applies to all replies in that profile, not only payment ones.
If the hook ever raised or timed out, Hermes would show a short "reply withheld" notice instead of the
original; Proofly catches its own errors and bounds its RPC time so that path should not trigger.

Tool progress lines, reasoning, and commentary between tool calls are separate surfaces. Hermes drops
commentary on held turns, but tool progress and reasoning keep their own settings. For a strict "no
premature claims" setup turn them off too:

```yaml
display:
  tool_progress: "off"
  show_reasoning: false
```

(`hermes config set display.tool_progress off` works but v0.23.1 prints a spurious "not a recognized
config key" notice; the CLI and gateway do read the key.)

## Using it

`/proofly` inside a session:

```
/proofly                 recent verdicts for this chat + settings
/proofly status          settings and verdict counts
/proofly last            the most recent verdict in this chat, in full
/proofly history [n]     up to n (max 20) recent verdicts in this chat
/proofly check <hash>    execution-only check of a Base tx hash
/proofly check <hash> to=0x... amount_raw=N [token=0x...|usdc|eth] [from=0x...]
/proofly clear           delete this chat's Proofly history
```

The model can call `proofly_verify_tx` itself:

```json
{"tx_hash": "0xaea5caf798ef5492f22d078e96e5a563357188bc6962fcf5cc7069d6a4b19070",
 "expected_transfer": {"type": "erc20", "to_address": "0x77b0d999195a301477494de535720b58fd085ab2",
                       "amount_raw": "30000000", "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}}
```

It returns JSON with `verdict`, `label`, `summary`, `execution_status`, `transfer_match`,
`block_number`, `confirmations`, `evidence`, `explorer_url`, `checked_at` and `rpc_endpoint_label`.

## Sample output

Captured from `tests/test_hermes_integration.py` running inside Hermes v0.23.1: a real
`handle_function_call` of the simulated demo wallet, then Hermes' own held `transform_llm_output`
path. Chain data in that run came from the recorded fixtures; the same test with `PROOFLY_LIVE=1`
against `https://mainnet.base.org` returned the same verdicts (block 55530464 had 58,222
confirmations at that time). The model sentences are scripted worst cases.

Case A: the wallet said "Payment sent successfully" with a hash that actually reverted.
Model reply: `Done! I've sent 5 USDC to 0x77b0...5ab2. The payment was successful, transaction 0x716a...be20.`
What the user sees:

```
Done! [Proofly correction: this payment is NOT verified: tx 0x716a...be20 reverted onchain, so nothing was transferred.]

---
Proofly check (Base mainnet): the payment in this reply is NOT verified.
- TRANSACTION REVERTED: tx 0x716a6f5f...cebe20. Receipt status 0 in block 55530464: nothing was transferred.
  Inspect: https://basescan.org/tx/0x716a6f5fdf191d72233bd8c1b5ddb0ee1de6aae95cb5bfaba566150830cebe20
```

Case B: a real successful USDC transfer, but not the one requested (10 USDC to `0x77b0...5ab2`):

```
- MISMATCH - CLAIM NOT SUPPORTED: tx 0xbc124c46...e58044. Executed onchain, but it is not the expected
  transfer: recipient expected 0x77b0d999195a301477494de535720b58fd085ab2, observed
  0xd8c9baf0908df28ed858e3b636b174c0c543b595; amount expected 10 USDC (raw 10000000), observed
  2.115636 USDC (raw 2115636).
```

Case C: the matching transfer:

```
I've sent 30 USDC to 0x77b0d999195a301477494de535720b58fd085ab2. Transaction hash: 0xaea5caf798ef5492f22d078e96e5a563357188bc6962fcf5cc7069d6a4b19070.

---
Proofly check (Base mainnet): verified against chain evidence.
- VERIFIED TRANSFER ON BASE: tx 0xaea5caf7...b19070. 30 USDC (raw 30000000) from 0x1a42650a7a4c4fbbd7e1c0879259caf73ffc1ff9 to 0x77b0d999195a301477494de535720b58fd085ab2 (token 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913), block 55530464, 101 confirmations (policy: at least 3).
  Inspect: https://basescan.org/tx/0xaea5caf798ef5492f22d078e96e5a563357188bc6962fcf5cc7069d6a4b19070
```

`/proofly` afterwards:

```
Proofly: recent verdicts in this chat
2026-11-10T10:54:58+00:00  VERIFIED_TRANSFER match  tx 0xaea5caf7...b19070  via demo_send_payment  (transfer_matches_intent)
2026-11-10T10:54:58+00:00  MISMATCH no-match  tx 0xbc124c46...e58044  via demo_send_payment  (erc20_transfer_mismatch)
2026-11-10T10:54:58+00:00  REVERTED no-match  tx 0x716a6f5f...cebe20  via demo_send_payment  (receipt_status_0)
```

All three hashes are real historical Base mainnet transactions from block 55530462/55530464
(2026-11-10), cross-checked identical on `mainnet.base.org` and `base-rpc.publicnode.com`. Block
hashes and timestamps: [`tests/fixtures/chain/PROVENANCE.json`](tests/fixtures/chain/PROVENANCE.json).

## Demo

`python demo.py` replays the three cases through the real hook callbacks without a model or Hermes
(`--live` sends every check to Base mainnet). It labels what is simulated: the payment tool and the
model replies. The hashes, hooks and verdicts are real.

`examples/demo-wallet/` is a separate, clearly simulated plugin (`proofly-demo-wallet`) with one tool,
`demo_send_payment`. It moves no funds. For 5, 10 or 30 USDC to `0x77b0...5ab2` it returns
"success" with the reverted hash, the wrong-payment hash, or the correct hash. It is not part of the
catalog entry.

Recording the official demo in a real Hermes session (target 65 to 80 seconds, hard cap 89):

Linux / macOS:

```bash
hermes plugins install 0xileri/proofly --enable
git clone https://github.com/0xileri/proofly /tmp/proofly
cp -r /tmp/proofly/examples/demo-wallet ~/.hermes/plugins/proofly-demo-wallet
hermes plugins enable proofly-demo-wallet
hermes config set plugins.entries.proofly.settings.payment_tools '["demo_send_payment"]'
hermes config set tools.tool_search.enabled off
hermes config set display.tool_progress off
hermes config set display.show_reasoning false
hermes gateway stop
hermes
```

Windows PowerShell:

```powershell
hermes plugins install 0xileri/proofly --enable
git clone https://github.com/0xileri/proofly "$env:TEMP\proofly"
$h = Split-Path -Parent (hermes config path | Select-Object -Last 1)
Copy-Item -Recurse "$env:TEMP\proofly\examples\demo-wallet" "$h\plugins\proofly-demo-wallet"
hermes plugins enable proofly-demo-wallet
hermes config set plugins.entries.proofly.settings.payment_tools [demo_send_payment]
hermes config set tools.tool_search.enabled off
hermes config set display.tool_progress off
hermes config set display.show_reasoning false
hermes gateway stop
hermes
```

Notes on these steps:

- `tools.tool_search.enabled off`: by default Hermes' Tool Search defers non-core plugin tools behind
  `tool_search` / `tool_call`, so the model would have to search before it can use the wallet (extra
  model round trips). Turning it off puts `demo_send_payment` and `proofly_verify_tx` directly in the
  model's tool list. Proofly also works when a tool is called through the bridge.
- `hermes gateway stop`: Proofly reads its settings when the plugin loads, and Hermes keeps a background
  gateway running between launches. Stopping it makes the next `hermes` start a fresh gateway that loads
  the settings above. Run it again after any later change to Proofly's settings.
- Windows: `hermes config path` prints the active `config.yaml`; its folder is the Hermes home (normally
  `%LOCALAPPDATA%\hermes\` on native Windows). Windows PowerShell 5.1 strips inner double quotes from
  arguments to native programs, so the list is written as `[demo_send_payment]`; Hermes stores it as a
  list either way. The Hermes commands were checked on Linux; the PowerShell lines were not run on
  Windows.
- `display.tool_progress off` prints a spurious "not a recognized config key" notice on v0.23.1; the key
  is still read.

Then type:

1. (0 to 8 s) title card: "An agent can say a transfer succeeded. Proofly checks the chain."
2. `Send 5 USDC to 0x77b0d999195a301477494de535720b58fd085ab2 with the demo wallet.` Case A: reverted.
3. `Now send 10 USDC to the same address.` Case B: mismatch.
4. `Now send 30 USDC to the same address.` Case C: verified, open the Basescan link briefly.
5. `/proofly` to show the ledger, then the repo and `hermes plugins list` for a few seconds.

To show the final-reply rewrite on its own (the model believes the tool and claims success), also set
`annotate_tool_results: false` before recording. Say on screen that the wallet is simulated and the
chain checks are real.

## Tests

```bash
python -m unittest discover -s tests -v                         # offline, deterministic, no network
PROOFLY_LIVE=1 python -m unittest discover -s tests -p test_integration.py -v   # live Base RPC
<hermes-venv>/bin/python -m unittest discover -s tests -v        # adds the real-Hermes tests
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

119 tests: verifier (hash/URL validation, every verdict, native and ERC-20 matching, look-alike
emitter, malformed logs, split transfers, confirmation policy, chain ID mismatch, timeout and retry,
HTTP 429, bad and truncated JSON, inconsistent responses, refused redirects, size limits, a real HTTP
transport against a local server), intent parsing, hooks (session and turn isolation, replay,
idempotency, both hook orders, warn vs enforce, hypotheticals and quotes, error fallbacks), ledger
(isolation, retention, restart, schema versioning, sanctioned path), `/proofly`, manifest vs
registration, the demo wallet, live Base checks and the real-Hermes path. Results are in
[`BUILD_REPORT.md`](BUILD_REPORT.md).

## Limitations and threat model

1. **No hash or no trusted intent means no proof.** Without both, Proofly cannot prove that a
   particular requested transfer happened. A successful hash found elsewhere is reported as executed,
   never as the verified payment.
2. **`receipt.status == 1` proves execution on Base, not intent.** Only the exact match against
   trusted structured intent gives `VERIFIED_TRANSFER`.
3. **An RPC miss is not a failure.** Unavailable or missing data is `NOT_FOUND`, `PENDING` or
   `UNVERIFIED`, never "the transfer failed".
4. **Coverage is limited to supported flows.** Only tools in `payment_tools` and `proofly_verify_tx`
   are watched. Transfers made through generic terminal or network calls, other agents, other chains,
   internal transfers inside contract calls, and non-standard tokens (fee-on-transfer, rebasing) are
   outside what Proofly can prove. It is not a wallet, an executor, a sandbox, an indexer, an oracle,
   a general truth detector or insurance.
5. **Only the final reply is rewritten.** Tool progress and reasoning are displayed separately; hide
   them for a strict demo. `hold_stream` holds all replies in the profile, not just payment ones.
6. **The RPC endpoint is trusted for data.** Proofly checks the chain ID and internal consistency
   (tx vs receipt hash and block) but a malicious endpoint could still lie consistently. Use an
   endpoint you trust; cross-check important payments on a second provider.
7. **Intent is only as good as the tool call.** Payment-tool intent is the structured arguments the
   tool executed with. If the model called the tool with the wrong recipient, Proofly verifies that
   wrong recipient. Use `/proofly check ... to=... amount_raw=...` to check against your own numbers.
8. **Confirmations, not finality.** Proofly counts blocks; it does not check L1 finality or the
   `finalized` tag. A deep reorg could change an included transaction.

## Privacy, storage and security disclosures

- **Network:** sends read-only Ethereum JSON-RPC requests (`eth_chainId`,
  `eth_getTransactionByHash`, `eth_getTransactionReceipt`, `eth_blockNumber`) to the configured Base RPC
  endpoint only. The endpoint operator can observe the queried tx hashes and your IP address. Proofly
  sets only the `Content-Type`, `Accept` and `User-Agent: proofly/1.0` headers; Python's HTTP client
  adds the standard `Host`, `Content-Length`, `Connection` and `Accept-Encoding` headers. No cookies,
  no Authorization, no Hermes credentials. The system `HTTPS_PROXY` / `NO_PROXY` settings are honoured.
- **Storage:** stores a local verdict ledger in Hermes profile-scoped plugin data only
  (`<HERMES_HOME>/plugin-data/proofly/data.db`, via `plugins.plugin_storage.plugin_db`). Per row: an
  HMAC of the session id and of the turn id (never the raw ids), source, tx hash, chain ID, verdict,
  reason code, block and confirmation counts, expected and observed transfer fields, the RPC endpoint
  as scheme and host only, mode, and a timestamp. No conversation text, no raw tool output, no RPC URL
  path or query. The newest 300 rows are kept. `/proofly clear` deletes only the calling chat's rows.
  Proofly's code writes nothing inside the plugin's install directory. The only files that appear
  there are Python's own `__pycache__/` bytecode cache (written by the interpreter on import) and, for
  catalog installs, Hermes' `.hermes-catalog.json` install record.
- **Credentials:** does not access keys, sign transfers, create wallet transactions, access credential
  stores, or read `.env` files. It declares no `requires_env` and no secrets.
- **Processes:** no background services, shell subprocesses, self-update, binaries or telemetry.
  The only dependency is the Python standard library.
- **Visibility:** verifies only supported payment tool flows and visible claims, not all behaviour;
  other Hermes progress and reasoning surfaces may display before final-answer rewriting.

## Sources

- Hermes plugin guide: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- Hook reference: https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks
- Catalog submission: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission
- Catalog schema and admission rules: https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md
- Base `eth_chainId`: https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_chainId

## Known issues

- Load-balanced public RPCs can briefly report a head behind the receipt's block. Proofly then counts 0
  confirmations and reports `PENDING` until the next check.
- `display.tool_progress` triggers a spurious "not a recognized config key" notice from
  `hermes config set` on v0.23.1.
- Claim detection is English-only and conservative; a missed claim still gets the appended check.

## License

MIT, see [LICENSE](LICENSE).
