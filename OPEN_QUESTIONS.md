# Open questions and unresolved defects

Written 2026-09-23, after the first 16-hour collection run on a clean data
boundary. Ordered by what would change a decision, not by effort.

Everything here is either an unanswered empirical question or a known defect.
Items that are merely "could be better" are not in this file.

---

## 1. THE LIVENESS CONFOUND — does the agent have an edge, or just a pulse?

**Status: unresolved. This is the question the project turns on.**

The 16-hour run showed what looked like an edge. Token-level medians:

| horizon | APPROVED | REJECTED | up-rate approved | up-rate rejected |
|---------|----------|----------|------------------|------------------|
| 30m     | 0.79     | 0.09     | 71%              | 51%              |
| 60m     | 1.06     | 0.00     | 69%              | 47%              |
| 120m    | 2.31     | 0.00     | 75%              | 47%              |

Monotonic, widening with horizon, and a 70% up-rate against a coin flip.

**Then section 5b reverses it.** Restricted to tokens with >= 50 txns/hour, at
30 minutes the REJECTED median is **2.40** against APPROVED's **0.96**. At 60
and 120 minutes the two are level. The apparent edge does not survive
controlling for whether the token was trading at all.

Section 4 supplies the mechanism: the REJECTED cohort carries **10% zero
returns against APPROVED's 1%**. A dead token's median return is 0.00 by
construction. So a gate that mostly selects *live* tokens will look like alpha
and is not one.

This is the failure `stage2_check.sql` warns about in its own section-5
comment -- "a gap present in mean but absent in net is a liquidity filter, not
alpha" -- in a variant nobody had written down: a liveness filter.

**How it gets answered:** section 10c runs the rank test inside activity bands
(<50, 50-500, 500+ txns/hour). If `p_outrank` stays above 0.5 WITHIN a band,
the gates find something beyond liveness. If it collapses toward 0.50 in every
band, they do not -- and no threshold tuning changes that, because the gates
would be measuring a property the control arm lacks rather than a property
that predicts return.

**Do not tune any gate threshold until this is resolved.** Tuning against a
confounded comparison optimises the confound.

**Corollary if confirmed:** the honest next step is not better gates but a
better control arm -- comparing approved tokens against rejected tokens
*matched on activity*, so liveness is held constant by construction rather
than adjusted for afterwards.

---

## 2. Extreme marks: real runners or broken prints?

**Status: partially diagnosed, not fixed.**

The top returns in the run:

```
MUSEBOOK  +13,054,508%  basis 0.00001172 -> 1.53     depth $11,650   txns_h1 20
DOAI       +1,021,102%  basis 0.00003726 -> 0.3805   depth $21,772   txns_h1 3795
AMCB           +6,711%  identical at 30, 60 AND 120 minutes
MINECOIN       +4,814%  identical at 60 and 120
ShinyHunter    +3,083%  identical at 60 and 120
```

MUSEBOOK had **twenty transactions in an hour**. A token with twenty trades did
not genuinely move 130,000x; that is a bad price, most likely the wrong side of
a pair or a second pool. The repeated-identical returns are prices that moved
once and froze, then got re-marked at each horizon -- the mark is real, the
independence is not.

The lower end of the list (10x, 31x, 49x on tokens with 4,000-6,000 txns/hour)
looks like genuine memecoin behaviour.

**There is no clean rule separating the two**, which is why section 10 stopped
using means rather than trying to trim. But two things are still worth doing:

- Check whether `fetch_current_prices_sync` (the MARK path) can pick a
  different pool than `fetch_dex_pair_data` (the EVALUATION path) for the same
  token. `price_at_evaluation` and `h.price` come from different functions.
  The side-resolution bug was fixed in both, but "deepest pool wins" can still
  disagree between two calls minutes apart.
- Consider recording, per mark, whether the price CHANGED since the previous
  mark. An unchanged price across three horizons is not three observations.

---

## 3. Mint and freeze authority are free and unused

**Status: not started. Highest-value unexploited signal.**

Jupiter's `/tokens/v2` rows carry an `audit` block with mint and freeze
authority status, and `holderCount`, on every row at no extra cost. Nothing in
the pipeline reads them.

A live mint authority means the supply can be inflated to zero at the
founder's discretion. That is a structural fact, true for hours, and belongs in
the hard-veto tier of the two-tier design. It is currently unavailable for ~57%
of tokens (RugCheck coverage) and gated on by nothing.

`holderCount` is in the same position: it currently comes only from RugCheck at
43% coverage, and `market_microstructure.check_shell()` and `check_turnover()`
both need it to adjudicate.

Before wiring: dump one full `/tokens/v2/recent` record and read the `audit`
sub-object's real field names. Do not guess them.

---

## 4. Exit policy is fixed regardless of volatility

**Status: not started. Probably worth more than the entry gates.**

Every token gets the same 7.53% stop and 2:1 target. A coin swinging 40% an
hour and one swinging 4% do not deserve the same stop distance -- the first is
stopped out by noise before the thesis has a chance, and that shows up as a
losing gate rather than a losing exit rule.

`market_microstructure.dynamic_slippage_percent()` already scales tolerance by
realised 5m movement. The same input could scale the stop. This has had the
least attention of any component and gates nothing today, so it is safe to
experiment with in the paper arm.

---

## 5. Smaller known items

- **`price_change_m5` populates on only 81% of rows** (section 2). Everything
  else is at 100%. Worth knowing whether DexScreener omits it for young tokens,
  since the m5 features are the ones section 6 keeps flagging as `maybe`.
- **DexScreener indexes ~8.3% of new pools** at 30 minutes. Accepted, not a
  defect: a token DexScreener cannot price is one the pipeline cannot evaluate.
  Recorded here so it is not rediscovered as a surprise.
- **`tests/test_discovery.py` and `tests/test_prices.py` use
  `asyncio.get_event_loop()`**, which raises on Python 3.11 once any suite has
  called `asyncio.run()`. They pass only because they run before
  `test_holder_concentration`. Reordering suites breaks them. `test_pen.py`
  uses `asyncio.run()` and does not care.
- **Stage 3 has never been exercised.** `signer_service/tests/run.py`,
  `verify_solders.py` and the four refusal checks have not been run once. Not
  urgent while `ENABLE_STAGE3_EXECUTION=false`, but it is the gap between
  "paper works" and "real money is safe".

---

## The standing rule this file exists to serve

Nothing above gets acted on by inference. Each item names the measurement that
would settle it. The pattern this project keeps hitting is a plausible number
that turns out to describe the instrument rather than the market -- the
fabricated 0.93 entry, the 67% "no data" rejections, the 4-second pen drain,
the mean blinded by one print. Every one looked like a finding first.
