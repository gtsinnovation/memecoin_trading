# signer_service/tests/run.py
"""Signer regression runner. Guards audit fixes 12-14 and the audit gaps.

    docker compose --profile stage3 exec signer python -m tests.run

Needs no database and no Turnkey credentials: the DB-facing and network-facing
calls are replaced with recorders, because what is being tested is the
service's own control flow -- which paths sign, which refuse, and which leave
an audit row behind.

Exit 0 = every guarded fix still holds.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    print("=" * 68)
    print("SIGNER REGRESSION SUITE -- audit fixes 12-14 + audit-trail gaps")
    print("=" * 68)
    # main.py reads these at import time and _require_config() needs them
    # non-empty. Values are never used -- nothing here calls Turnkey.
    os.environ.setdefault("TURNKEY_ORGANIZATION_ID", "test-org")
    os.environ.setdefault("TURNKEY_API_PUBLIC_KEY", "02" + "a" * 64)
    os.environ.setdefault("TURNKEY_API_PRIVATE_KEY", "b" * 64)
    os.environ.setdefault("TURNKEY_SOLANA_WALLET_ADDRESS",
                          "So11111111111111111111111111111111111111112")
    try:
        from tests import test_signer
        suite = test_signer.run()
    except Exception as e:
        print(f"\nSUITE CRASHED: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 2

    print("\n" + "=" * 68)
    mark = "OK  " if suite.ok() else "FAIL"
    print(f"  {mark}  {suite.name}: {suite.passes} passed, {len(suite.failures)} failed")
    print("=" * 68)
    if not suite.ok():
        print(f"\n{len(suite.failures)} REGRESSION(S) -- a signer fix has been undone:\n")
        for f in suite.failures:
            print(f"  - {f}")
        return 1
    print(f"\nAll {suite.passes} assertions passed. Every guarded signer fix still holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
