# Stage 3 setup: real signing on Solana devnet

This is Stage 3 of the DEX/wallet integration path described in
README.md. It adds a **separate signing microservice** (`signer_service/`)
that can actually sign and broadcast a Solana transaction, via
[Turnkey](https://turnkey.com), a managed/enclave-based custody provider —
but only on **devnet** (test network, no real money), and only a tiny
self-transfer used to prove the plumbing works, not a real trade. Read
[Caveats](README.md#caveats-read-this) in the README before doing any of
this, and read this whole document once before running any command in
it — several steps are one-way or security-relevant.

**What this stage does NOT do:** buy or sell any token, touch mainnet, or
give the main pipeline app any signing capability at all. The pipeline
can only *ask* the signer service to act, and the signer independently
re-checks that request before ever calling Turnkey. See README.md's
"Real market data / DEX / wallet architecture" section for the full
three-layer design (pipeline gate → signer's own `policy_guard.py` →
Turnkey's own policy engine).

**I cannot do the steps in Part 1 for you.** Creating a Turnkey
organization and API key pair requires an account only you control, and
the private key half of that pair should never be pasted into a chat
with me or anyone else — you'll generate it yourself and put it directly
into a file on your own machine/server.

---

## Part 1: Create your Turnkey organization and credentials

1. **Sign up.** Go to [turnkey.com](https://turnkey.com) and create an
   account. This creates your organization automatically — note your
   **Organization ID** from the dashboard (Settings, or wherever the
   current dashboard surfaces it).

2. **Generate an API key pair.** In the Turnkey dashboard, find the API
   keys section for your organization and generate a new key pair
   (P-256 is the default curve). The dashboard will show you the public
   key and either show or download the private key **once** — save both
   immediately somewhere safe (a password manager, not this chat, not a
   world-readable file). Turnkey does not retain a copy of your private
   key; if you lose it, you generate a new pair.

3. **Create a Solana wallet account inside your org.** Either through
   the dashboard (add a wallet → add a Solana address to it) or via the
   API (`ACTIVITY_TYPE_CREATE_WALLET_ACCOUNTS`, curve `CURVE_ED25519`,
   address format `ADDRESS_FORMAT_SOLANA`). The result is a normal
   base58 Solana address (e.g. `9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM`) —
   note it down.

4. **Configure a Turnkey policy scoping what this key is allowed to
   sign.** This is layer 3 of the defense-in-depth design, enforced
   inside Turnkey's own enclave, independent of every line of code in
   this repo. In the dashboard's policy section, write a policy (or a
   few) that, at minimum:
   - Restricts signing to Solana transactions from the wallet address
     above.
   - Allowlists only the programs a self-transfer needs: the System
     Program (`11111111111111111111111111111111111111111`) and
     `ComputeBudget111111111111111111111111111111`.
   - Caps the transfer amount well below anything that matters — this is
     devnet SOL (worthless), but writing the cap now is the right habit
     for when Stage 4 points this at mainnet.

   Turnkey's own docs on this are the source of truth for current UI/API
   shape — see
   [docs.turnkey.com/features/policy-engine](https://docs.turnkey.com/features/policy-engine)
   and their [Solana policy engine post](https://www.turnkey.com/blog/introducing-solana-policy-engine).
   Their own guidance: write policies around **invariants** (allowed
   programs, mint allowlists, max amounts, destination allowlists), not
   an exact instruction sequence — a real swap transaction's exact shape
   varies.

5. **Fund the wallet with devnet SOL** (free, not real money):
   ```
   solana airdrop 1 <your-solana-address> --url https://api.devnet.solana.com
   ```
   If that's rate-limited (common), try
   [faucet.solana.com](https://faucet.solana.com/),
   [faucet.quicknode.com/solana/devnet](https://faucet.quicknode.com/solana/devnet),
   or wait ~15 minutes and retry. You only need enough for transaction
   fees (a fraction of one SOL covers thousands of test transactions).

---

## Part 2: Configure the signer service

```
cp signer_service/.env.example signer_service/.env
```

Edit `signer_service/.env` and fill in everything Part 1 produced:
`TURNKEY_ORGANIZATION_ID`, `TURNKEY_API_PUBLIC_KEY`,
`TURNKEY_API_PRIVATE_KEY`, `TURNKEY_SOLANA_WALLET_ADDRESS`. Also set:

- `ALLOWED_EXECUTION_TOKENS` — for this devnet smoke test, any
  placeholder value works (e.g. the wallet address itself) since
  `SIGNER_MODE=devnet_transfer_test` doesn't actually look at token
  liquidity — but the field must be non-empty or every request is
  refused by `policy_guard.py` before it gets anywhere near Turnkey.
- `MAX_TRADE_USD` — any positive number (e.g. `10`) for the smoke test.

Leave `SIGNER_MODE=devnet_transfer_test` and
`SOLANA_RPC_URL=https://api.devnet.solana.com` as shipped.

This file (`signer_service/.env`) is **separate** from the project root's
`.env` on purpose — the main `web` service's container never has these
values, by design (see `signer_service/main.py`'s module docstring).
Never commit it.

### Before you trust `solana_tx.py`: verify the solders API yourself

`signer_service/solana_tx.py` was written against `solders`' well-
established API shape, but this was built without network access to
actually pip-install and run `solders`, so its exact calls have not been
executed even once before reaching you (see the file's own docstring).
Before running the smoke test below, verify the two calls it depends on
match your installed version:

```
docker compose --profile stage3 run --rm signer python3 -c "
from solders.transaction import Transaction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.system_program import transfer, TransferParams
pk = Pubkey.from_string('11111111111111111111111111111111111111111')
bh = Hash.from_string('11111111111111111111111111111111111111111')
ix = transfer(TransferParams(from_pubkey=pk, to_pubkey=pk, lamports=1000))
msg = Message.new_with_blockhash([ix], pk, bh)
tx = Transaction.new_unsigned(msg)
raw = bytes(tx)
print('OK -- serialized', len(raw), 'bytes')
print(Transaction.from_bytes(raw))
"
```

If this errors, the installed `solders` version's API has drifted from
what `solana_tx.py` assumes — check the error against
[solders' current docs](https://kevinheavey.github.io/solders/) and
adjust `build_sol_transfer_tx()`/`reassemble_signed_sol_transfer()`
accordingly before going further.

---

## Part 2b (recommended): give the signer its own least-privilege DB role

`signer_service/.env`'s `DATABASE_URL` defaults to the `postgres`
superuser, which works but hands a service that talks to a signing API
full read/write access to every table. The signer needs far less than
that. Creating a scoped role costs one command and means a compromised
signer container cannot alter positions, settings, or P&L.

**What it actually needs** (this is every statement it issues — grep
`signer_service/*.py` to confirm):

| Table | Access | Why |
|---|---|---|
| `app_settings` | SELECT | re-check `run_status` and the capital cap |
| `active_positions` | SELECT | sum currently-deployed capital |
| `execution_audit_log` | INSERT | record every attempt, including refusals |

Note it is **not** purely read-only: it must be able to append to the
audit log. An audit trail a service cannot write to is not an audit
trail. It needs no UPDATE or DELETE anywhere, so a bad actor with the
signer's credentials can add audit rows but never erase them.

```sql
-- Run once, as the postgres superuser:
--   docker compose exec db psql -U postgres -d memecoin_trading

CREATE ROLE signer_svc LOGIN PASSWORD 'choose-a-strong-password-here';

GRANT CONNECT ON DATABASE memecoin_trading TO signer_svc;
GRANT USAGE   ON SCHEMA public              TO signer_svc;

GRANT SELECT ON app_settings, active_positions TO signer_svc;
GRANT INSERT ON execution_audit_log            TO signer_svc;

-- SERIAL columns need the sequence too, or every INSERT fails with
-- "permission denied for sequence execution_audit_log_id_seq".
GRANT USAGE, SELECT ON SEQUENCE execution_audit_log_id_seq TO signer_svc;
```

Then point the signer at it:

```
DATABASE_URL=postgresql://signer_svc:choose-a-strong-password-here@db:5432/memecoin_trading
```

Verify the limits actually bind, rather than assuming:

```sql
-- as signer_svc: these must SUCCEED
SELECT run_status FROM app_settings WHERE id = 1;
SELECT COALESCE(SUM(allocated_usd), 0) FROM active_positions;

-- and these must FAIL with "permission denied"
UPDATE app_settings SET run_status = 'RUNNING';
DELETE FROM active_positions;
SELECT * FROM closed_positions;
```

If the UPDATE succeeds, the grant did not apply and you have a
superuser with extra steps.

---

## Part 3: Run the smoke test

```
docker compose --profile stage3 up -d --build
```

This starts `signer` alongside `db` and `web` (it's excluded from a plain
`docker compose up` — see docker-compose.yml's comments). Then, from the
host:

**1. Confirm authentication works, without signing anything:**
```
curl http://localhost:8000/  # unrelated -- just confirms "web" is up
docker compose exec web curl -s http://signer:8100/whoami
```
(The signer has no port published to the host on purpose, so this is
called from inside the `web` container's network, not `curl localhost`.)
A successful response echoes back your organization/user identity. An
error here means the Turnkey credentials in `signer_service/.env` are
wrong before you even get to signing — fix that first.

**2. Attempt a real (devnet) sign + broadcast:**
```
docker compose exec web curl -s -X POST http://signer:8100/execute \
  -H "Content-Type: application/json" \
  -d '{"token_address": "<whatever you put in ALLOWED_EXECUTION_TOKENS>", "requested_usd": 5}'
```
A success response looks like:
```json
{"executed": true, "confirmed": true, "tx_signature": "...", "network": "devnet", "reason": "broadcast and confirmed"}
```
Look up `tx_signature` on
[explorer.solana.com/?cluster=devnet](https://explorer.solana.com/?cluster=devnet)
or [solscan.io](https://solscan.io) (devnet toggle) — you should see a
real, tiny, self-to-self SOL transfer from your Turnkey wallet. That
transaction landing on-chain is the actual proof this works: real
signing, inside Turnkey's enclave, broadcast to a real (if valueless)
network, independently re-validated by `policy_guard.py` before any of
it happened.

**3. Check the audit trail:**
```
docker compose exec db psql -U postgres -d memecoin_trading -c "SELECT * FROM execution_audit_log ORDER BY id DESC LIMIT 5;"
```
You should see the attempt recorded with `outcome = 'SIGNED_BROADCAST'`
and the same `tx_signature`.

**4. Confirm the refusal paths actually refuse.** Try a request for a
token NOT in `ALLOWED_EXECUTION_TOKENS`, and one with `requested_usd`
above `MAX_TRADE_USD` — both should come back `"executed": false` with a
`403`, and show up in `execution_audit_log` as `REFUSED_POLICY`. Also try
pausing trading from the dashboard (the existing Pause Trading button)
and confirm `/execute` then refuses with `run_status is 'PAUSED_MANUAL'`
— this is the same kill-switch/pause control from earlier stages now
reaching all the way through to the signer, independent of whether the
main pipeline itself is even looking at it.

If all four checks pass, the signing plumbing is proven end-to-end on
devnet.

---

## Part 4: Wiring into the pipeline (optional, and still devnet-only)

Everything above works standalone, without the main pipeline calling it
at all. To let `pipeline_executor_worker()` itself call the signer after
`I_ACCOUNTANT` approves a position (still only ever the devnet self-
transfer test, still gated by every check above), set in the project
root's `.env`:
```
ENABLE_STAGE3_EXECUTION=true
```
then `docker compose up -d --build` (restart `web`; `signer` is already
running from Part 3). See README.md's Stage 3 section for exactly what
changes in the pipeline's behavior when this is on — it stays entirely
additive (an extra call after a position is already recorded the way it
always was) and never blocks or slows the simulated flow if the signer
is unreachable or refuses.

## What's next (Stage 4, not built yet)

Stage 4 is real trading on mainnet: `SIGNER_MODE=mainnet_jupiter_swap`,
which `signer_service/main.py` currently refuses outright (a clear
501, not a half-implementation). That means wiring in the real Jupiter
swap-transaction flow (`solana_tx.decode_jupiter_swap_transaction()` is
already written and unit-tested against mocked Jupiter responses, but
never exercised against a real network — see market_data.py's Stage 1
tests for the same pattern), pointing `SOLANA_RPC_URL` at mainnet, and —
critically — tightening every policy in Part 1 step 4 with real money in
mind. Do not attempt this without deliberately deciding to, the same way
every prior stage here has been explicitly requested rather than
assumed.
