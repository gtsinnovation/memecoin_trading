# Solana Meme-Token Signal and Paper-Trading Experiment

A FastAPI + Postgres application that evaluates Solana token candidates with
a LangGraph signal pipeline, records paper-trading and measurement data, and
streams operator metrics to a dashboard. The project includes risk gates,
capital/run-duration controls, a watchdog, pause controls, and a separate
devnet-only signing service.

> **This is not a live trading product.** Candidate data and post-entry marks
> come from market-data providers, but the paper engine models fills from
> quote/path observations; it does not submit or confirm token swaps. The
> signer can only perform a small devnet self-transfer. Paper P&L is an
> experiment result with material fill, cost, sampling, and data-availability
> limitations. Read [Caveats](#caveats-read-this) before relying on it.

## How it works

### The pieces

- **`engine.py`** — the signal, risk, and paper-position logic: a LangGraph
  pipeline (`agent_network`), the Postgres read/write functions each node
  uses, and the operator-facing controls (capital cap, kill-switch,
  watchdog, run status).
- **`main.py`** — the FastAPI web server: the background loop that drives
  the pipeline every few seconds, the WebSocket broadcast to the dashboard,
  the settings REST API, the sign-in gate, and the dashboard's HTML/JS
  (all served from a single in-memory string — there's no separate
  frontend build step).
- **`schema.sql`** — the Postgres schema, auto-applied by the `db`
  container the first time its data volume is created.
- **`migrate.sql`** — the same schema changes expressed as `ALTER
  TABLE`/`CREATE TABLE IF NOT EXISTS` statements, safe to run by hand
  against a database that already has data in it (see
  [Applying schema changes later](DEPLOYMENT.md#applying-schema-changes-later)
  in the deployment guide).
- **`docker-compose.yml`** / **`Dockerfile`** — a two-container stack:
  `db` (Postgres 15) and `web` (this app, via Uvicorn).

### The agent pipeline

Every few seconds, `pipeline_executor_worker()` in `main.py` evaluates a
candidate from `WATCHLIST_TOKEN_ADDRESSES` when a watchlist is configured;
otherwise, with `ENABLE_TOKEN_DISCOVERY=true`, it draws from discovery
sources and the new-listing queue. It fetches a market snapshot and runs it
through `agent_network`, the LangGraph pipeline in `engine.py`:

| Node | Role |
|---|---|
| `A_ORBIT` | Starts evaluation and records the candidate context. |
| `B_SENTINEL` | Rejects missing or insufficient one-sided tradeable pool depth. |
| `C_VECTOR` | Evaluates short-window price movement and execution degradation. |
| `D_PULSE` | Sets a spot-based paper entry, stop, and 2:1 target; it does not wait for the named pullback. |
| `E_BREADTH` | Tests participant breadth against capital/volume activity; it is not a social-hype gate. |
| `F_ATLAS` | Applies the configured holder-concentration rule and refuses missing measurements. |
| `G_ANCHOR` | Refuses missing or excessive impact estimates and sizes from tradeable depth and stressed stop loss. |
| `H_FUSE` | Compiles a human-readable setup briefing. |
| `I_ACCOUNTANT` | Applies run/capital controls and records a paper position; the optional signer path is separate. |
| `Z_CLOSER` | Records the decision and rejection reason in `trading_sessions`. |

A rejection at any gate short-circuits straight to `I_ACCOUNTANT` (which
just logs the rejection) via that node's conditional router — the pipeline
never opens a position for a setup that failed a gate.

Every node is wrapped by `with_watchdog(name)`, which runs it in a worker
thread with a timeout (`agent_timeout_seconds`, configurable in Settings).
If a node doesn't return in time, it raises `AgentUnresponsiveError` and
the configured **agent-unresponsive action** (Settings → "If an Agent Is
Unresponsive") kicks in — either restart the pipeline's DB pool and keep
looping (`RESTART_ALL`), or stop the background loop entirely while
leaving the web server/dashboard up (`SHUTDOWN`; see
[Caveats](#caveats-read-this)).

### Positions and P&L

`I_ACCOUNTANT` records accepted positions in `active_positions`. The legacy
position ledger is marked against current provider prices by
`evaluate_open_positions()`; the Stage 2 experiment is maintained separately
by `paper_trading.py` and writes `paper_trades`, horizon returns, and sampled
price paths. These are quote-marked/modelled outcomes, not exchange fills.
When Stage 3 is enabled, a reserved position is not treated as filled until
the signer outcome is reconciled. Pausing blocks new entries but does not
liquidate existing positions.

### Real market data (Stage 1)

By default, `main.py` selects the composite `free_market_data.py` provider
for the token `pipeline_executor_worker()` is about to evaluate. It combines
free, keyless sources:

| Field | Source | Notes |
|---|---|---|
| Price, liquidity, 1h/24h volume | [DexScreener](https://docs.dexscreener.com/api/reference) | Only covers tokens with an indexed trading pair — a brand-new pre-graduation pump.fun token may not show up yet. |
| Estimated slippage (`estimated_slippage_percent`) | [Jupiter's quote endpoint](https://dev.jup.ag/docs/swap-api/get-quote) | Price impact for a hypothetical `SLIPPAGE_PROBE_USD`-sized swap, not your actual position size. |
| Top-10 holder concentration and rug signals | RugCheck | Provider-derived metrics; the legacy DexScreener provider uses Solana JSON-RPC for holder concentration instead. |
| Social/hype volume (`social_volume_score`) | Not used by current pipeline gate | `free_market_data.py` supplies a placeholder score; current `E_BREADTH` does not use it. Do not interpret it as measured social activity. |

When the selected DexScreener and Jupiter prices differ by more than
`PRICE_DISAGREEMENT_REJECT_PCT` (default 100% symmetric difference, meaning
one is over twice the other), `A_ORBIT` records a price-integrity rejection
with both raw prices and the selected pool address. This is an entry-time
guard; the rejected paper row is retained as `INVALID_DATA` without a fill
or return label. Periodic position and horizon marks still need pool identity
checks.

Position sizing applies the lesser of 1% of one-sided tradeable depth, the
notional cap, and a loss-budget cap. Defaults are $1,000 reference equity,
0.5% equity risk, a 7.53% stop, and 6.5% stressed round-trip costs; that
limits notional to about $35.63 per position before gap risk. Set the
`REFERENCE_EQUITY_USD`, `MAX_TRADE_RISK_PERCENT`,
`STRESSED_ROUND_TRIP_COST_PERCENT`, and `MAX_POSITION_NOTIONAL_USD` values
to match the paper experiment. This is a sizing guard, not a guarantee of
maximum loss: gaps and unavailable exits can exceed it.

`WATCHLIST_TOKEN_ADDRESSES` is an optional comma-separated override. With
the default `ENABLE_TOKEN_DISCOVERY=true`, discovery supplies candidates
when the watchlist is empty. If discovery is disabled and the watchlist is
empty, the pipeline idles. See `.env.example` for tunable env vars such as
`SOLANA_RPC_URL`, `JUPITER_API_BASE`, `DEXSCREENER_API_BASE`, and
`SLIPPAGE_PROBE_USD`.

If a watchlisted token has no indexed DexScreener pair yet (or a lookup
fails outright), that tick is skipped for that token rather than
inventing fake numbers — you'll see it logged, not silently substituted.

### Paper experiment (Stage 2)

`paper_trading.py` records approved and rejected candidates and compares an
immediate-entry model with a pullback-limit model. The limit model counts a
fill when a sampled quote reaches the trigger; it does not verify that a
specific executable order filled. Exit transaction activity is recorded as
confirmation evidence, but entry fills remain modelled. The experiment also
deducts configured fees and estimated/assumed slippage, records fixed-horizon
returns and dropout, and samples ordered quote paths for exit-policy replay.

Use `stage2_check.sql` to inspect data coverage, distinct-token counts,
rejections by reason, staleness, dropout, cost assumptions, confirmation, and
cohort returns. Read sample sizes and data-quality sections before interpreting
any average. The approved/rejected comparison is observational; it is not a
randomized control or proof of causal impact. The replay is limited by provider
quote quality and `PATH_SAMPLE_SECONDS` resolution. No Stage 2 result by itself
demonstrates that live fills or live P&L will match.

#### Switching to the GMGN provider

`MARKET_DATA_PROVIDER=gmgn` (with `GMGN_API_KEY` set) swaps the three
sources above for GMGN's OpenAPI, via `gmgn_market_data.py`. Both
providers return identical keys, so nothing else in the pipeline changes.
What you get for it: price, liquidity, volume **and** top-10 holder
concentration from one request instead of three (no Solana RPC call at
all), plus a real search-heat attention score replacing the social
placeholder. What you give up: a keyless setup, and single-vendor
independence — which is why DexScreener stays the default and remains a
one-variable fallback.

**This integration is read-only by construction.** It calls only GMGN's
unsigned read endpoints, which need nothing but an API key — no
request-signing key, no wallet binding, no deposit, no custody. GMGN's
*trading* API is a different thing entirely: GMGN generates and holds the
wallet key, it cannot be exported, and their API layer has no spending
caps or token allowlists, so any request the key signs executes against
the full balance. That's the opposite of the guarantees in
[Real signing](#real-signing-stage-3-devnet-only), which is why execution
stays on our own signer and why nothing in this codebase calls
`/v1/trade/*`. If you extend `gmgn_market_data.py`, keep it that way —
one trading call would quietly convert a read-only integration into a
custodial one.

Two honesty notes on the attention score. It measures searches **on
GMGN's platform** — real signal from real users, and a large improvement
on a hardcoded constant, but not off-platform social volume; there's no
Twitter/X post count or sentiment in it. And a token absent from the
ranking scores 0 with `_social_data_missing` set, because the list is
capped at the top 500 chain-wide and most tokens are absent most of the
time — absence is unknown, not measured-zero.

Because this could not be tested against the live API from where it was
built (same constraint as every other integration here), **run
`smoke_test_gmgn.py` before trusting it**:

```
docker compose run --rm --no-deps web python3 smoke_test_gmgn.py
```

It needs no database and no watchlist (`--no-deps` skips starting
Postgres), so a failure is unambiguously a data-layer failure. It checks
config, authentication, clock skew against GMGN's server (a >5s drift
fails every request and is miserable to diagnose without being told),
response shape, each field mapping, the attention score, and finally an
end-to-end snapshot through the real dispatcher — calling the actual
functions in `gmgn_market_data.py`, not a reimplementation. It
auto-discovers a currently-top-ranked token to test against, or takes
`--token <mint address>`.

Read the values it prints rather than just the PASS lines: a field can
parse cleanly and still be wrong. The one most worth eyeballing is
top-10 holder concentration, which should be a percent (e.g. `17.83`) —
if it shows `0.1783`, the fraction-to-percent conversion has broken and
the `F_ATLAS` gate would read a dangerously concentrated token as safe.

The script maps GMGN's error codes to specific fixes. Two worth knowing
in advance: `AUTH_IP_BLOCKED` means your key's IP whitelist doesn't
include the machine you're running on (a server's egress IP is usually
not your laptop's), and any `RATE_LIMIT_` error means stop and wait —
GMGN extends the ban for each request made before the reset time, so
re-running makes it longer, not shorter.

**This is still not wallet integration.** Stage 1 only replaces the
*data the agents look at*; nothing in this stage holds a private key,
signs a transaction, or submits a trade. That's Stage 3, below.

### Real signing (Stage 3, devnet only)

A separate service, `signer_service/`, can sign and broadcast a real
Solana transaction via [Turnkey](https://turnkey.com) — but only a tiny
devnet self-transfer used to prove the plumbing works, never a real
trade. Full setup walkthrough: **[STAGE3_SETUP.md](STAGE3_SETUP.md)**.

Three independent layers decide whether anything actually gets signed:

1. **The pipeline's own gate** (`I_ACCOUNTANT` in `engine.py`, unchanged
   from earlier stages) — decides whether to open a position at all.
2. **The signer service's own re-validation**
   (`signer_service/policy_guard.py`) — authenticates the caller (an HMAC
   over every request, `signer_auth.py`), refuses any `client_order_id` it
   has seen before (its own `signer_orders` ledger), checks the pipeline
   really reserved this exact order, verifies the RPC's genesis hash, and
   applies caps from **its own environment** — per trade, total deployed,
   24h notional and 24h order count, plus the pre-signature rails in
   `execution_rails.py`. The dashboard's capital cap can only tighten
   those, never raise them. Every attempt, approved or refused, is written
   to `execution_audit_log`, and signed rows are never pruned.
3. **Turnkey's own policy engine**, enforced inside its enclave,
   configured directly in your Turnkey org (program allowlist,
   destination allowlist, amount caps) — independent of every line of
   code in this repo.

This is deliberately a **separate container** from `web`
(`docker-compose.yml`'s `signer` service, gated behind the `stage3`
Compose profile so it doesn't start with a plain `docker compose up`)
with its **own** `.env` (`signer_service/.env.example`) — the main app
never has Turnkey credentials, only the signer's internal URL and an
explicit `ENABLE_STAGE3_EXECUTION` switch (default `false`). With that
switch off — the shipped default — `main.py`'s
`maybe_execute_via_signer()` returns immediately and nothing about the
simulated pipeline changes at all.

`SIGNER_MODE=devnet_transfer_test` is the only implemented mode: a
self-transfer of a few thousand lamports, not a purchase of any token —
because Jupiter (the swap router used for the actual buy/sell logic) has
no devnet, so there is no such thing as a real devnet trade to execute
(see [Real market data](#real-market-data-stage-1) above on Jupiter's
mainnet-only nature). `SIGNER_MODE=mainnet_jupiter_swap` exists as a
named placeholder and is refused outright — that's unbuilt Stage 4 work,
real money, and a deliberately separate decision from this one.

### Stage 4 prerequisites (unbuilt — read before considering mainnet)

Three requirements that a comparison against a production trading venue
(GMGN, which does ~$5B/month) made obvious, and that Stage 4 must not
skip:

**MEV protection is mandatory, not optional.** An unprotected Solana swap
is a standing invitation to sandwich attacks. This is measured, not
theoretical: in one 30-day window roughly 31% of all Solana sandwich
attacks targeted a single venue's users, costing ~23,600 SOL, and that
venue's response was to ship MEV protection and default it *on*. Our
current Stage 4 sketch has nothing here. Route through Jito bundles or a
private mempool before a single mainnet trade, and treat "MEV protection
disabled" as an invalid configuration rather than a tuning option.

**Something must close positions when this app is down.** Take-profit and
stop-loss currently live in `evaluate_open_positions()` — a loop inside
our container. On devnet that's fine; on mainnet it means a crash, an OOM
kill, or a failed deploy while holding a position leaves that position
open and unmanaged with real money in it. Exchange-grade venues attach
condition orders that live server-side and survive the client
disconnecting. Stage 4 needs an equivalent: on-chain or venue-side
conditional exits, or at minimum an independent watchdog process that can
close positions when the main pipeline stops heartbeating. Until that
exists, mainnet exposure is only as reliable as our container's uptime.

**A human-approval threshold for large trades.** GMGN's CLI has one
genuinely good idea worth copying: it reads trade confirmation from
`/dev/tty` rather than stdin, specifically so that an agent driving it
over a pipe cannot approve its own trades — their code names the threat
as an agent that "read a prompt-injection payload out of token metadata."
That exact attack surface exists here (see the security note in
[Caveats](#caveats-read-this)). A containerized service has no TTY, so
the direct analog doesn't port; the equivalent is a signer-side rule that
refuses any trade above a configured size until a human approves it
out-of-band. For fully unattended operation the honest alternative is not
"skip the gate" but "set `MAX_TRADE_USD` low enough that you can afford
to lose it without a human in the loop."

### Pause, resume, and the kill-switch

New position entries can be blocked without touching anything that's
already open, in three ways — all of them just set `app_settings.run_status`
and are all read by the same gate in `I_ACCOUNTANT`:

- **Manual pause** — click "Pause Trading" on the dashboard
  (`PAUSED_MANUAL`). Click "Resume Trading" to lift it.
- **Kill-switch** — `check_kill_switch()` runs every tick and pauses
  trading (`PAUSED_KILL_SWITCH`) if realized P&L breaches a configured max
  loss ($), max drawdown (%), or a run of consecutive losing trades — all
  three are optional and off (`NULL`) by default.
- **Run duration elapsed** — if a run duration is configured, trading
  pauses (`PAUSED_DURATION_ELAPSED`) once it's up.

In every case, this **only blocks new entries.** Positions already open
keep resolving normally through `evaluate_open_positions()` — pausing (by
any of these three paths) never force-closes anything. "Resume Trading"
clears whichever pause is active and restarts the run-duration timer.

### Settings

The dashboard's Settings modal (⚙) reads/writes a single-row
`app_settings` table via `GET`/`POST /api/settings`:

- **Capital allocation limit** — blocks a new entry once total deployed
  capital (sum of `active_positions.allocated_usd`) would exceed it.
- **Run duration** — minutes/hours before new entries auto-pause.
- **Capital / P&L wallet labels** — free-text fields for your own
  bookkeeping only. **There is no wallet connection or transaction signing
  anywhere in this project** — these do not move funds or read a real
  balance.
- **Show real-time balances** — toggles a CSS blur over the capital/P&L
  figures; also togglable ad hoc from the header without opening Settings.
- **Agent-unresponsive action** and **timeout** — see the pipeline section
  above.
- **Kill-switch thresholds** — max drawdown %, max loss $, max consecutive
  losses; each has an "Off" checkbox to disable it.

### Sign-in

`AUTHORIZED_GOOGLE_EMAIL` (an environment variable) is the one address
allowed in. Visiting the dashboard while signed out redirects to
`/auth/login`, a plain form asking for that email address — **this is a
simple text-match gate, not real Google OAuth.** Nothing verifies the
visitor actually controls that Gmail account; treat the configured address
itself as a shared secret. See the comment at the top of the
"Authentication" section in `main.py` for the full rationale, and
[Caveats](#caveats-read-this) below.

### Database schema

| Table | Purpose |
|---|---|
| `trading_sessions` | One row per pipeline run (approved or rejected), for the funnel/rejection charts. |
| `active_positions` | Currently open simulated positions. |
| `closed_positions` | Quote-marked P&L ledger — positions move here after a provider mark crosses take-profit or stop-loss; this is not a confirmed exchange fill. |
| `system_alerts` | The console/alerts feed on the dashboard. |
| `app_settings` | Single-row (`id = 1`) operator configuration — see Settings above. |
| `authorized_users` | Present for a possible future multi-user allowlist; **not read by the current sign-in check**, which only compares against `AUTHORIZED_GOOGLE_EMAIL`. |

## Running it locally

```
cp .env.example .env
# edit .env: set AUTHORIZED_GOOGLE_EMAIL, SESSION_SECRET_KEY, POSTGRES_PASSWORD;
# optionally set WATCHLIST_TOKEN_ADDRESSES (see "Real market data" below)
# For localhost HTTP only, also set SESSION_COOKIE_INSECURE=1.
docker compose up --build
```

Then open `http://localhost:8000` and sign in with the configured email.
`SESSION_COOKIE_INSECURE=1` is only for localhost HTTP; never set it on a
public host. This starts only `db` and `web` — the Stage 3 signer
service does not start with a plain `docker compose up` (see [Real
signing](#real-signing-stage-3-devnet-only) above); that's entirely
optional and separately documented in
**[STAGE3_SETUP.md](STAGE3_SETUP.md)**.

For deploying this to a public server instead of running it locally, see
**[DEPLOYMENT.md](DEPLOYMENT.md)**.

## Caveats (read this)

- **No real trading, and only partial real market data.** Candidate-side
  numbers (price, liquidity, volume, holder concentration, estimated
  slippage) are provider observations as described in [Real market
  data](#real-market-data-stage-1). Social volume is a placeholder and is
  not used by the current gate. Paper results use sampled quotes and
  modeled entries/exits, not confirmed fills; provider marks can be stale
  or missing. Nothing in this app places a real token trade. Don't make
  real financial decisions from this dashboard.
- **No wallet integration in the main app, ever.** The wallet label
  fields are metadata only, and Stage 1's market-data fetching never
  touches a private key, signs anything, or submits a transaction. Real
  signing exists ONLY in the separate `signer_service` (Stage 3, see
  above), is off by default (`ENABLE_STAGE3_EXECUTION=false`, and the
  `signer` container itself doesn't start without opting into the
  `stage3` Compose profile), and even when turned on only ever executes
  a harmless devnet self-transfer — never a real trade, never mainnet,
  never money. Read [STAGE3_SETUP.md](STAGE3_SETUP.md) in full before
  touching any of it.
- **Sign-in is a shared-secret email match, not Google identity
  verification.** See the Sign-in section above.
- **Token metadata is attacker-controlled input, and is handled as such.**
  A token's symbol is chosen by whoever minted it; we read it off a public
  indexer and it flows into logs, Postgres, the agent's reasoning text and
  the dashboard. It is scrubbed of control, zero-width and bidi-override
  characters on the way in (`sanitize_external_text()` in `market_data.py`)
  and HTML-escaped on the way out at every `innerHTML` site in the
  dashboard (`esc()`). Both layers are deliberate: neither is redundant.
  Earlier versions escaped neither, which meant a token minted with a
  symbol like `<img src=x onerror=...>` could run script in the operator's
  authenticated dashboard session — a session that can change capital
  limits and pause or resume trading. If you add a new field to the
  broadcast payload or a new `innerHTML` site, escape it.
- **Kill-switch and manual pause never force-close positions** — by
  design (see the Pause/resume section above) — so a paused run still has
  open exposure until those positions individually hit their own
  take-profit/stop-loss.
- **Watchdog `SHUTDOWN` halts new entries, not the loop.** It sets
  `run_status = SHUTDOWN_WATCHDOG`, which blocks entries exactly like a
  pause, while open positions keep being marked and closed at their own
  take-profit/stop-loss. (It used to stop the whole loop, which also stopped
  position monitoring until the container was restarted.) Resume from the
  dashboard as for any pause.
- **A stalled or dead pipeline restarts itself.** A watchdog thread exits
  the process when no pipeline iteration has started for
  `WATCHDOG_EXIT_AFTER_S` (default 900s), or when a background worker has
  died; `restart: unless-stopped` brings the container back. Set
  `WATCHDOG_EXIT_ENABLED=false` to only report (`/health` still goes 503).
