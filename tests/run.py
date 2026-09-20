# tests/run.py
"""Regression runner. Guards audit fixes 1-11 (see tests/harness.py).

    docker compose exec web python -m tests.run

Creates a throwaway database, applies schema.sql, runs every suite, drops it.
Your live data is never touched -- the harness refuses to run against a
database whose name doesn't end in `_test`.

Exit code 0 = every fix still holds. Non-zero = something regressed, and the
output names which.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import harness
from tests.harness import Suite


def main() -> int:
    try:
        import psycopg2
    except ImportError:
        print("psycopg2 is not installed. Run this inside the web container:")
        print("  docker compose exec web python -m tests.run")
        return 2

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    schema_path = os.path.join(project_root, "schema.sql")
    if not os.path.exists(schema_path):
        print(f"schema.sql not found at {schema_path}")
        return 2

    print("=" * 68)
    print("REGRESSION SUITE -- audit fixes + discovery + microstructure/rails")
    print("=" * 68)

    # Self-test the SAFETY GUARD before trusting it. Every suite below
    # truncates tables; the only thing standing between that and the live
    # experiment is assert_is_test_db. A guard that silently stopped working
    # would be discovered the expensive way, so it is checked first.
    try:
        harness.assert_is_test_db("postgresql://postgres@db:5432/memecoin_trading")
        print("\nFATAL: the live-database guard did not refuse a live DSN. Aborting.")
        return 2
    except RuntimeError:
        pass
    harness.assert_is_test_db("postgresql://postgres@db:5432/memecoin_trading_test")
    print("safety guard verified: refuses non-_test databases")
    try:
        dsn = harness.build_test_database(psycopg2, schema_path)
    except Exception as e:
        print(f"\nCould not build the test database: {e}")
        traceback.print_exc()
        return 2
    print(f"test database ready: {harness._dbname_of(dsn)}")

    # Point the engine at the test database BEFORE importing it -- engine.py
    # reads DATABASE_URL at import time into a module constant.
    live_dsn = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn

    suites = []
    try:
        import engine
        import paper_trading
        import token_discovery
        from tests import test_gates, test_paper, test_discovery, test_prices, test_microstructure, test_tx_verify, test_fill_accounting, test_dpulse, test_direction_agents, test_hardening, test_main

        suites.append(test_gates.run(psycopg2, engine, dsn))
        suites.append(test_paper.run(psycopg2, paper_trading, dsn))
        suites.append(test_discovery.run(token_discovery))
        import market_data
        suites.append(test_prices.run(market_data))
        # Pure-logic suite: no database, no network, no fixtures.
        suites.append(test_microstructure.run())
        suites.append(test_tx_verify.run())
        suites.append(test_fill_accounting.run())
        suites.append(test_dpulse.run(engine))
        suites.append(test_direction_agents.run(engine))
        suites.append(test_hardening.run())
        import main
        suites.append(test_main.run(main))
    except Exception as e:
        print(f"\nSUITE CRASHED: {type(e).__name__}: {e}")
        traceback.print_exc()
        failed = Suite("crashed")
        failed.failures.append(str(e))
        suites.append(failed)
    finally:
        if live_dsn is not None:
            os.environ["DATABASE_URL"] = live_dsn
        try:
            harness.drop_test_database(psycopg2)
        except Exception as e:
            print(f"(could not drop the test database: {e})")

    print("\n" + "=" * 68)
    total_pass = sum(s.passes for s in suites)
    total_fail = sum(len(s.failures) for s in suites)
    for s in suites:
        mark = "OK  " if s.ok() else "FAIL"
        print(f"  {mark}  {s.name}: {s.passes} passed, {len(s.failures)} failed")
    print("=" * 68)
    if total_fail:
        print(f"\n{total_fail} REGRESSION(S) -- an audit fix has been undone:\n")
        for s in suites:
            for f in s.failures:
                print(f"  - [{s.name}] {f}")
        return 1
    print(f"\nAll {total_pass} assertions passed. Every guarded fix still holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
