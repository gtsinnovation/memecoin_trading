# Regression suite

Guards the fourteen defects found in the September 2026 audit. These are not
"does the code work" tests — they exist so a change made later, in a file that
looks unrelated, cannot quietly undo one of them.

Most of the fourteen produced a **plausible wrong number** rather than a crash:
a gate that passed when it should have refused, a cohort statistic borrowed
from the wrong cohort, a correlation coefficient that was not the coefficient
it claimed to be. Nothing about the running system looks broken when one
regresses. That is what makes them worth pinning down.

## Running

```
docker compose exec web python -m tests.run
docker compose --profile stage3 exec signer python -m tests.run
```

Exit code 0 means every guarded fix still holds. Non-zero names what broke.

Two runners because the two services have different dependencies: the signer
image has `solders` and the Turnkey stamper; the web image has `psycopg2` and
the pipeline modules. Neither can run the other's tests.

## Your live data is never touched

The pipeline suite creates a throwaway database, applies `schema.sql`, runs
there, and drops it. `harness.assert_is_test_db()` refuses outright to run
against any database whose name doesn't end in `_test`, and the runner
self-tests that guard before doing anything else.

There is deliberately no override flag. The one moment someone would reach for
it is the one moment it matters.

The signer suite needs no database and no Turnkey credentials — the DB- and
network-facing calls are replaced with recorders, because what is under test is
the service's control flow: which paths sign, which refuse, and which leave an
audit row behind.

## What is covered

| # | Fix | Suite |
|---|---|---|
| 1 | G_ANCHOR fails closed on unmeasured slippage | `test_gates` |
| 2 | F_ATLAS fails closed on unmeasured concentration | `test_gates` |
| 3 | Capital read raises rather than returning 0.0 | `test_gates` |
| 4 | `position_logged` reflects whether a row was written | `test_gates` |
| 5 | Kill-switch threshold of 0 means "unconfigured" | `test_gates` |
| 6 | Missing-data flags reach the gates | `test_gates` |
| 7 | Unpriceable trades abandoned, not censored | `test_paper` |
| 8 | Fill statistics attributed per cohort | `test_paper` |
| 9 | Unmeasured slippage stored NULL, not 0 | `test_paper` |
| 10 | Spearman uses mid-ranks | `test_paper` |
| 11 | Staleness medians not fanned out by the join | `test_paper` |
| 12 | NaN rejected; inputs length-bounded | `test_signer` |
| 13 | Network resolved from host, not URL substring | `test_signer` |
| 14 | Signed transaction verified against what was sent | `test_signer` |
| — | Audit trail survives confirmation/Turnkey/DB failures | `test_signer` |

Fixes 1–6 also assert the *inverse*: a genuinely measured 0% still passes. A
gate that refuses everything is not fixed, it is broken differently.

Fix 10 uses a fixed 40-row fixture in which ties are the only structure.
Correct Spearman gives −0.074; the `RANK()` implementation gave +0.173 — a sign
flip conjured from nothing but how tied values were numbered. The test asserts
the first and rejects the second.
