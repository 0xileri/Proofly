# Proofly build report

Date: 2026-11-10. Tested code commit: **`9fde65f5e8f5f5ab3ad6a5dc6a9848f119deb444`** on branch
`claude/zen-brahmagupta-k188oi` of https://github.com/0xileri/Proofly. Commits after it change
documentation only (this report, the draft catalog entry).

## Status

| Item | Result |
|---|---|
| Plugin loads in real Hermes v0.23.1 (current stable) | PASS |
| Plugin loads in real Hermes v0.22.6 (declared floor) | PASS |
| Hermes v0.22.5 rejects it (proves the floor) | PASS: `register_hook() got an unexpected keyword argument 'hold_stream'` |
| `hermes plugins doctor . --ci` (v0.23.1, v0.22.6) | PASS, exit 0 |
| `hermes plugins validate . --install-deps` (v0.23.1, v0.22.6) | PASS, security scan `safe`, no warnings |
| Install from GitHub: `hermes plugins install 0xileri/Proofly --ref 9fde65f... --enable` | PASS, `enabled git pinned@9fde65f5 1.0.0 proofly`, doctor OK on the installed copy |
| Unit tests, offline (`python -m unittest discover -s tests -v`, Python 3.13) | PASS: 119 run, 106 passed, 13 skipped (live + real-Hermes groups) |
| Full suite with live Base RPC inside Hermes' Python 3.14 venv, v0.23.1 and v0.22.6 | PASS: 119 run, 119 passed, 0 skipped, on each |
| Live RPC test against `https://mainnet.base.org` and `https://base-rpc.publicnode.com` | PASS (6/6 on each) |
| Fixes a false transfer claim (case A) in real Hermes | PASS |
| Refuses to green-check an unrelated successful tx (case B, and EXECUTED without intent) | PASS |
| Verifies a truly matching transfer (case C) | PASS |
| Draft catalog entry parses with Hermes' `load_catalog` and matches `plugin.yaml` at the pin | PASS |
| Draft PR on 0xileri/Proofly | BLOCKED: the repo had no commits, so this branch became its only and default branch; there is no base branch to open a PR against |
| Real Hermes chat session with a live model, demo video | NOT DONE: needs your model provider credentials and a screen recording |
| Catalog PR to NousResearch/hermes-agent, X post, Typeform submission | NOT DONE: outward-facing, under your account; steps below |

## Environment

- Cloud container, Linux, system Python 3.13.18; `uv` 0.11.32.
- `hermes` was not installed. With your approval, Hermes was installed from the official source,
  `github.com/NousResearch/hermes-agent`, at release tags into throwaway venvs in the session
  scratchpad (nothing outside it):
  - v0.23.1, commit `3b421578588243f920886e6fe79a6faa87ea026e` (released 2026-11-10, current stable)
  - v0.22.6, commit `f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`
  - v0.22.5, commit `bf0203a794a0...` (only to confirm the floor)
- Install method: `UV_PROJECT_ENVIRONMENT=<venv> uv sync --frozen --python 3.14 --no-dev` from each
  tag's own `uv.lock`. This deviates from the documented `install.sh` / `source ./activate` (PM
  toolchain with Node, pnpm and more); the lockfile route installs the same pinned Python dependency
  set for the CLI and plugin runtime. Every core dependency is gated on Python 3.14, so 3.14.6 was
  used. `hermes --version`: `Hermes Agent v0.23.1 (2026.9.24) · stable · local 3b4215785882`.
- PyPI's `hermes-agent` is stale (0.19.0) and was not used.
- Network: `mainnet.base.org` was blocked by the session's network policy (proxy 403). A personal
  cloud environment `proofly` was created (with your approval card) allowing only
  `mainnet.base.org` and `base-rpc.publicnode.com`.
- GitHub: authenticated as `0xileri` through the session's GitHub integration (the `gh` CLI is not
  authenticated for anything but search here). Push to `origin` succeeded.

## Hermes API findings (from the v0.23.1 source, rechecked on v0.22.6)

- `VALID_HOOKS` includes `post_tool_call`, `transform_tool_result`, `transform_llm_output`
  (`hermes_cli/plugins.py`). Payloads used:
  - tool hooks: `tool_name, args, result, task_id, session_id, conversation_id, tool_call_id, turn_id,
    api_request_id, duration_ms, status, error_type, error_message` (`model_tools.py`
    `_emit_post_tool_call_hook`, `_apply_transform_tool_result_hook`);
  - `transform_llm_output`: documented `response_text, session_id, model, platform`; the source also
    passes `turn_id` (`agent/turn_finalizer.py` `apply_llm_output_transform`). Proofly treats it as
    optional. Both tool hooks and this hook take `turn_id` from `agent._current_turn_id`, so per-turn
    correlation is exact.
- In the agent loop, `transform_tool_result` runs inside `handle_function_call` and the terminal
  `post_tool_call` is emitted afterwards by `agent/tool_executor.py`; the documented order is the
  reverse. Proofly handles both orders, keyed by `tool_call_id` (tested both ways).
- `PluginContext.register_hook(name, cb, *, hold_stream=False)` exists from v0.22.6. On a held turn a
  callback that raises or times out yields Hermes' "reply withheld" notice. All three hooks are bounded
  by `plugins.hook_callback_timeout` (30 s default) and a timed-out callback keeps running on a daemon
  thread, so Proofly bounds its own RPC time (max 20 s per hook call) and guards shared state with a lock.
- Storage: `plugins.plugin_storage.plugin_db(name)` (public API list) opens
  `<HERMES_HOME>/plugin-data/<name>/data.db`.
- `config_schema` types accepted: `str, int, float, bool, list, dict, secret` and aliases.
- The install scanner's `deception_hide` pattern matches instructions that tell the agent to keep
  something from the user. An early draft of the tool-result instruction was phrased as a negative
  "tell" instruction and matched it; it now states the verdict positively and the scan is `safe`.
- `hermes config set display.tool_progress off` prints a spurious "not a recognized config key"
  notice on v0.23.1; the CLI and gateway still read the key (`hermes_cli/cli_init_mixin.py`,
  `gateway/display_config.py`).

## Base RPC findings

- `mainnet.base.org` (Cloudflare) answers HTTP 403 to Python's default `Python-urllib` User-Agent.
  Proofly sends `User-Agent: proofly/1.0`.
- `base-rpc.publicnode.com` answers lookups of unknown hashes with an error ("Archive requests require
  a personal token", HTTP 403 via urllib) instead of `null`. Proofly reports that as
  `RPC_ERROR` (unverified), `mainnet.base.org` gives `null` and Proofly says `NOT_FOUND`. Neither is
  reported as a failure.

## Fixture provenance

Real Base mainnet transactions, fetched from both RPCs and asserted identical on every key field
(tx hash, from, to, value, input, block hash and number, type, receipt status and logs), block hash
checked against `eth_getBlockByNumber`. Full data: `tests/fixtures/chain/PROVENANCE.json`.

| Fixture | Tx | Block | Status | Use |
|---|---|---|---|---|
| reverted | `0x716a6f5fdf191d72233bd8c1b5ddb0ee1de6aae95cb5bfaba566150830cebe20` | 55530464 | 0x0 | case A |
| usdc_transfer_other | `0xbc124c469915c21441056b911c75bf359f3d7082461f2fd64850f62499e58044` | 55530464 | 0x1, 2.115636 USDC to 0xd8c9...b595 | case B |
| usdc_transfer_30 | `0xaea5caf798ef5492f22d078e96e5a563357188bc6962fcf5cc7069d6a4b19070` | 55530464 | 0x1, 30 USDC to 0x77b0...5ab2 | case C |
| eth_transfer | `0x2c151d1905b6bb523537c197aea4df1f89decd378377e0f71f53dc1de1636995` | 55530462 | 0x1, 1185524242299554 wei | native ETH |

## Command output (abridged, real)

```
$ hermes plugins doctor . --ci            # same result on v0.23.1 and v0.22.6
Plugin Doctor: /home/user/Proofly
  manifest: proofly 1.0.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 1 tool(s), 3 hook(s)
exit=0

$ hermes plugins validate . --install-deps   # same result on v0.23.1 and v0.22.6
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.22.6' parses
✓ config schema — shape valid
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no collisions
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
◆ Isolation — runs in the plugin host (plugins.isolation: host)
Validation passed.
exit=0

$ hermes plugins doctor . --ci            # v0.22.5, manifest floor temporarily lowered for this probe
  ERROR: Plugin registration failed: PluginContext.register_hook() got an unexpected keyword argument 'hold_stream'

$ PROOFLY_LIVE=1 <hermes-0.23.1-venv>/bin/python -m unittest discover -s tests -v   # verbatim excerpt
  [https://mainnet.base.org] usdc_transfer_30: VERIFIED_TRANSFER (transfer_matches_intent), block 55530464, 60338 confirmations
  [https://mainnet.base.org] eth_transfer: VERIFIED_TRANSFER (transfer_matches_intent), block 55530462, 60344 confirmations
  [https://mainnet.base.org] reverted: REVERTED (receipt_status_0), block 55530464, 60345 confirmations
  [https://mainnet.base.org] usdc_transfer_30: EXECUTED (no_trusted_intent), block 55530464, 60348 confirmations
  [https://mainnet.base.org] unknown hash: NOT_FOUND (not_found) This node has no such transaction right now. That is not proof it failed.
  [https://mainnet.base.org] usdc_transfer_other: MISMATCH (erc20_transfer_mismatch), block 55530464, 60352 confirmations
Ran 119 tests in 8.894s
OK
```

`tests/test_hermes_integration.py` (7 tests) runs Proofly inside Hermes: `discover_plugins` with a
throwaway `HERMES_HOME`, checks the registry, `stream_held()` is true, then dispatches the demo wallet
through `model_tools.handle_function_call` (firing the real tool hooks) and runs Hermes'
`apply_llm_output_transform` with the held-transform snapshot. Outputs are quoted in the README
("Sample output"). It also dispatches `/proofly` via `invoke_plugin_command` and checks the ledger is
at `<HERMES_HOME>/plugin-data/proofly/data.db` and nothing is written in the plugin directory. No
Hermes code is patched; the only test-side change is Proofly's own RPC transport (fixture node unless
`PROOFLY_LIVE=1`).

## What was built

`plugin.yaml`, `__init__.py` (registration), `verifier.py`, `intent.py`, `claims.py`, `render.py`,
`hooks.py`, `ledger.py`, `commands.py`, `schemas.py`, `demo.py`, `examples/demo-wallet/` (separate
simulated plugin, not in the catalog entry), `tests/` (7 test modules, fixtures), `README.md`,
`LICENSE` (MIT), `.gitignore`, `catalog/proofly.yaml` (draft entry). Standard library only.

Design choices worth a reviewer's attention:

- No `pre_tool_call`: tool arguments arrive with `post_tool_call` / `transform_tool_result`, so Proofly
  never sits in front of tool execution.
- `payment_tools` defaults to empty. Proofly never guesses which tools move money; until configured
  only `proofly_verify_tx` creates a payment context.
- Invalid `rpc_url` is kept and fails every check closed rather than silently using another host.
- `EXECUTED` or a match below the confirmation policy qualifies a claim in place; contradicted or
  missing proof replaces it; only `VERIFIED_TRANSFER` keeps it with a verified footer.
- Labels use a hyphen instead of the em dash in AGENT.md's examples (your writing preference); the
  distinguishing words are unchanged.

## Limitations

See README "Limitations and threat model". In short: no proof without hash plus trusted intent;
status 1 is not intent; RPC misses are not failures; only allowlisted tool flows are covered;
only the final reply is rewritten (hide tool progress and reasoning for a strict demo); the RPC
endpoint is trusted for data; payment-tool intent reflects the tool call, not the user's words;
confirmations are counted, finality is not checked.

## Remaining steps for you

1. **Default branch / PR.** The repo's only branch is `claude/zen-brahmagupta-k188oi`. Either rename
   it to `main` in GitHub settings, or create `main` and I can open a PR into it.
2. **Record the demo in a real Hermes session** with a model: follow README "Demo". Label the wallet
   as simulated.
3. **Catalog PR.** Fork `NousResearch/hermes-agent`, copy `catalog/proofly.yaml` to
   `plugin-catalog/proofly.yaml` (pin stays `9fde65f...`, which is in the branch history; if you
   squash-merge into a new `main`, re-run `hermes plugins validate` at the new commit and re-pin), and
   open a PR adding only that file. The PR body can reuse README "Privacy, storage and security
   disclosures" and the command output above.
4. **Share the demo (under 90 s) on X tagging @NousResearch**, then enter the catalog PR link and the
   X link in the organizer's form. The organizer brief gives the deadline as November 11, 2026,
   5:00 PM EST (11:00 PM Africa/Lagos); confirm it on the form.
   Organizer form: https://form.typeform.com/to/FwJLvOln

To reproduce the validation locally after installing Hermes:

```bash
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```
