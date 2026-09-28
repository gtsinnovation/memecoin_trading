"""Stage 3 smoke test, run INSIDE the web container (STAGE3_SETUP.md Part 3).

    docker compose exec web python stage3_smoke.py whoami
    docker compose exec web python stage3_smoke.py execute --token <MINT> --usd 5
    docker compose exec web python stage3_smoke.py order <client_order_id>

Why a script and not curl: every signer endpoint now requires an HMAC over
the request (signer_auth.py), and /execute additionally requires a ledger
reservation the signer can cross-check. This does exactly what the pipeline
does -- reserve, sign the request, settle the row to the outcome -- so the
smoke test exercises the real path rather than a side door.

`execute` removes its ledger row afterwards (pass --keep to leave it): a
devnet self-transfer is not a position the pipeline should go on marking.
The signer's order ledger and execution_audit_log keep the record.
"""
import argparse
import asyncio
import json
import sys

import httpx

import main as app
import signer_auth
from engine import db_connect


def _reserve(token: str, usd: float) -> str:
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO active_positions (token_symbol, token_address, allocated_usd, "
                    "entry_trigger, execution_status) VALUES ('SMOKE', %s, %s, 1, "
                    "'PENDING_EXECUTION') RETURNING client_order_id::text;", (token, usd))
                return cur.fetchone()[0]
    finally:
        conn.close()


def _drop(order_id: str) -> None:
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_positions WHERE client_order_id::text = %s;", (order_id,))
    finally:
        conn.close()


async def _get(path: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{app.SIGNER_SERVICE_URL}{path}",
                        headers=app._signed_headers("GET", path, b""), timeout=30.0)
        return r.status_code, r.text


async def _execute(token: str, usd: float, keep: bool) -> int:
    order_id = _reserve(token, usd)
    print(f"reserved ledger row, client_order_id={order_id}")
    data = {}
    body = json.dumps({"token_address": token, "token_symbol": "SMOKE", "requested_usd": usd,
                       "client_order_id": order_id}, separators=(",", ":")).encode()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{app.SIGNER_SERVICE_URL}/execute", content=body,
                             headers=app._signed_headers("POST", "/execute", body), timeout=90.0)
        try:
            data = r.json()
        except ValueError:
            data = {}
        fate = app.execution_fate(r.status_code, data)
        print(f"HTTP {r.status_code} -> {fate}\n{json.dumps(data, indent=2)}")
    except Exception as e:
        fate = "UNKNOWN"
        print(f"signer call failed: {type(e).__name__}: {e} -> UNKNOWN")
    app.apply_execution_fate(order_id, fate, data.get("tx_signature") if fate != "UNKNOWN" else None)
    if fate == "UNKNOWN":
        print(f"Outcome unknown; the row is held. Re-check with: python stage3_smoke.py order {order_id}")
    elif not keep:
        _drop(order_id)
        print("ledger row removed (signer_orders and execution_audit_log keep the record)")
    return 0 if fate == "EXECUTED" else 1


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    e = sub.add_parser("execute")
    e.add_argument("--token", required=True)
    e.add_argument("--usd", type=float, required=True)
    e.add_argument("--keep", action="store_true")
    o = sub.add_parser("order")
    o.add_argument("client_order_id")
    a = p.parse_args()
    if app.SIGNER_SHARED_SECRET is None:
        print(f"{signer_auth.SECRET_ENV} is not set in this container (root .env), or is shorter "
              f"than {signer_auth.MIN_SECRET_LEN} characters. It must match signer_service/.env.")
        return 2
    if a.cmd == "whoami":
        code, text = asyncio.run(_get("/whoami"))
        print(f"HTTP {code}\n{text}")
        return 0 if code == 200 else 1
    if a.cmd == "order":
        code, text = asyncio.run(_get(f"/orders/{a.client_order_id}"))
        print(f"HTTP {code}\n{text}")
        return 0 if code == 200 else 1
    return asyncio.run(_execute(a.token, a.usd, a.keep))


if __name__ == "__main__":
    sys.exit(main())
