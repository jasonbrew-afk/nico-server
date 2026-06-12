# Nico → Robinhood MCP migration runbook

Status: **code complete, not yet live.** Everything is feature-flagged and inert
until the env vars below are set. Default behaviour (Alpaca) is unchanged.

Deploy = `git push origin main` → Railway redeploys the services from
`github.com/jasonbrew-afk/nico-server`.

---

## The pipeline (park & release, human-approved)

```
run_live (cron)                                    nico-bot (always-on)
  red-day trigger
  → build order + rationale
  → POST /pending  ─────────────►  nico-server  ◄── Postgres (source of truth)
  → POST approval  ──► Imago            ▲                 single writer of budget
  → exit (no trade)      │              │
                    you approve         │
                         └──► POST /execute ──► place via Robinhood MCP adapter
                                              → POST /placed (advance spend/triggers)
                                              → post fill to #lab-brew
```

---

## One-time setup (in order)

### 1. Robinhood Agentic account + token  ⟵ **only you can do this**
The Agentic account is **real money**; there is no paper mode.
```bash
cd nico-core
pip install -e '.[robinhood]'          # installs the `mcp` lib
python -m platforms.robinhood.mcp_adapter authorize
```
- Opens Robinhood onboarding in a **desktop browser**, creates the Agentic account,
  captures the redirect on `localhost:8765`.
- Writes tokens to `~/.config/nico/robinhood-mcp-tokens.json` (gitignored, `0600`).
- Prints the server's real `tools/list`.

**Then reconcile `TOOL_MAP`** in `nico-core/platforms/robinhood/mcp_adapter.py`
to the real tool names, and confirm whether `place_order` wants share `qty` or
dollar `notional` (and any Agentic-account id). Until this is done the adapter
**cannot place an order** — the names are placeholders.

### 2. Provision Postgres  ⟵ **only you can do this**
- Add the Railway Postgres plugin to the **nico-server** service.
- Set `DATABASE_URL` on nico-server **only** (it is the sole DB-cred holder).
- Schema is created automatically on nico-server startup.

### 3. Wire Imago
- Confirm Imago accepts an HTTP webhook (`IMAGO_APPROVAL_URL` + `IMAGO_API_TOKEN`).
  If it speaks MCP/CLI instead, only `approval._send_to_imago` changes.
- Imago must, on your approval, call the bot: `POST {bot}/execute` with
  `Authorization: Bearer $IMAGO_EXECUTE_TOKEN` and body
  `{request_id, order:{side,symbol,qty,notional_usd,price}, rationale}`.

### 4. Set env vars (per service — see `.env.example`)
| Var | server | bot | cron (run_live) |
|---|:---:|:---:|:---:|
| `DATABASE_URL` | ✓ | | |
| `NICO_STATE_TOKEN` | ✓ | ✓ | ✓ |
| `NICO_STATE_API_URL` (= nico-server URL) | | ✓ | ✓ |
| `NICO_EXECUTION_BACKEND=robinhood_mcp` | | | ✓ |
| `ROBINHOOD_REQUIRE_APPROVAL=1` (default) | | | ✓ |
| `IMAGO_APPROVAL_URL`, `IMAGO_API_TOKEN` | | | ✓ |
| `IMAGO_EXECUTE_TOKEN` | | ✓ | |
| token file `~/.config/nico/robinhood-mcp-tokens.json` | | ✓ | ✓ |

(`.env` is local-dev only; Railway reads its own Variables.)

### 5. Flip the backend
Set `NICO_EXECUTION_BACKEND=robinhood_mcp` on the cron. Done.

---

## Rollback
- **Instant, no redeploy:** set `NICO_EXECUTION_BACKEND=alpaca` (or
  `ROBINHOOD_REQUIRE_APPROVAL=0` is *not* a rollback — that removes the gate).
  Unset `NICO_STATE_API_URL` to drop back to local state.
- **Code:** every change is additive/flagged; `git revert` the feature commits if needed.

---

## Known blockers / not-yet-verified
- **`TOOL_MAP` placeholders** — hard blocker; needs the `authorize` output (step 1).
- **Live Postgres untested** — the HTTP contract + idempotency were verified with an
  in-memory store; the asyncpg `FOR UPDATE` path needs a real DB.
- **Imago transport** unconfirmed (assumed HTTP webhook).
- **Repo hygiene** — `nico-core/nico_core/alpaca_execution.py` and several strategy
  files are currently **untracked / uncommitted**; the live cron imports
  `alpaca_execution`. These must be committed before the cron can deploy cleanly
  (see the deploy notes / chips).
```
