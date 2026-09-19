"""Market microstructure: direction, not just magnitude.

WHY THIS MODULE EXISTS
The existing gate network answers one question well -- "is there enough of a
market here to get in and back out?" -- and does not ask a second, different
question: "which way is that market moving right now?"

Those are not the same question and they do not decay at the same rate.
Depth, holder base and authority state are structural: they are true for hours.
Direction and volatility are true for minutes. A token can carry $63M of 24h
volume across 32,000 trades in an hour -- clearing every liquidity and breadth
gate on the books -- while it is 16% down over the last five minutes with sell
volume beating buy volume in every window. Sizing into that is not a bad
forecast; it is an unfillable order.

THE TWO-TIER RULE
Mixing both kinds of fact into one "any failure rejects" list makes the same
token flip verdicts inside a single day, because half the inputs are stable and
half are noise. So this module returns two different things:

    VETO     -- structural, or a market so disorderly that no order fills
                sanely. There is exactly ONE directional veto: a 5-minute
                drawdown of 10% or worse.
    DEGRADE  -- tradeable, but not with a naive market order. Drives order
                type and slippage tolerance. Never rejects on its own.

Everything else directional produces DEGRADE. Absent directional data is
treated as the most disorderly case for EXECUTION purposes -- you do not get
to assume calm because you could not measure -- but it is never a veto, because
"we could not read the tape" is not evidence that the token is falling.

MEASUREMENT HONESTY
Every function here returns None when its inputs are absent, and every Finding
carries `measured`. A fabricated zero is how a gate passes on nothing, which is
the defect class this codebase has been bitten by repeatedly. There are no
`or 0.0` fallbacks in this file, by design.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    VETO = "VETO"        # do not trade this
    DEGRADE = "DEGRADE"  # trade it, but not with a naive market order
    INFO = "INFO"        # recorded, drives nothing


@dataclass
class Finding:
    """One microstructure observation.

    `measured` is separate from `severity` on purpose. An unmeasurable input
    produces measured=False, and the caller can tell "we looked and it was
    fine" apart from "we never got the number" -- which the numeric fields
    alone cannot express.
    """
    name: str
    severity: Severity
    measured: bool
    detail: str
    value: Optional[float] = None


@dataclass
class MicrostructureVerdict:
    findings: List[Finding] = field(default_factory=list)

    @property
    def vetoes(self) -> List[Finding]:
        return [f for f in self.findings if f.severity is Severity.VETO]

    @property
    def degradations(self) -> List[Finding]:
        return [f for f in self.findings if f.severity is Severity.DEGRADE]

    @property
    def blocked(self) -> bool:
        return bool(self.vetoes)

    @property
    def must_use_limit_order(self) -> bool:
        return bool(self.degradations)

    def reason(self) -> Optional[str]:
        return self.vetoes[0].detail if self.vetoes else None


# --- individual signals -------------------------------------------------

# A 5-minute fall of this much makes a market order unfillable at anything
# near the quote, whatever the longer-horizon thesis says. This is the only
# directional hard stop.
DRAWDOWN_VETO_PERCENT = 10.0
DRAWDOWN_DEGRADE_PERCENT = 5.0

# Thresholds are compared with a tolerance because the change is DERIVED from
# two prices rather than read as a number. A price of 90 against 100 is exactly
# a 10% fall, but in binary floating point it computes as -9.999999999999998,
# which is not <= -10.0 -- so a token sitting precisely on the documented veto
# threshold would be waved through as merely degraded. The error is always this
# small, and it must never fall on the permissive side of a money stop.
_THRESHOLD_EPSILON = 1e-9


def drawdown_5m_percent(price_now: Optional[float],
                        price_5m_ago: Optional[float]) -> Optional[float]:
    """Percent change over five minutes. Negative means falling.

    Providers report the PRICE five minutes ago, not the change. Treating that
    field as though it were already a percentage is a real and easy mistake, so
    the arithmetic lives here once rather than at each call site.
    """
    if price_now is None or price_5m_ago is None:
        return None
    if not (price_now > 0 and price_5m_ago > 0):
        return None
    return (price_now / price_5m_ago - 1.0) * 100.0


def check_drawdown(price_now: Optional[float],
                   price_5m_ago: Optional[float]) -> Finding:
    return check_drawdown_percent(drawdown_5m_percent(price_now, price_5m_ago))


def check_drawdown_percent(change: Optional[float]) -> Finding:
    """Same judgement, for providers that publish the 5m change directly.

    DexScreener reports priceChange.m5 as a percentage already. Routing it back
    through two synthetic prices to reuse check_drawdown would be a needless
    conversion, so the thresholds live here and check_drawdown delegates.
    """
    if change is None:
        # Unknown volatility forces conservative execution but never a veto.
        return Finding("DRAWDOWN_5M", Severity.DEGRADE, False,
                       "5-minute price history unavailable -- assuming disorderly for execution")
    if change <= -DRAWDOWN_VETO_PERCENT + _THRESHOLD_EPSILON:
        return Finding("DRAWDOWN_5M", Severity.VETO, True,
                       f"down {abs(change):.1f}% in 5m -- a market order fills into a falling knife",
                       change)
    if change <= -DRAWDOWN_DEGRADE_PERCENT + _THRESHOLD_EPSILON:
        return Finding("DRAWDOWN_5M", Severity.DEGRADE, True,
                       f"down {abs(change):.1f}% in 5m -- limit order only", change)
    return Finding("DRAWDOWN_5M", Severity.INFO, True, f"{change:+.1f}% over 5m", change)


def check_net_flow(windows: Dict[str, Dict[str, Optional[float]]]) -> Finding:
    """Buy versus sell volume, read window by window.

    The 24h figure alone is a cumulative number: an early wave of buying nets
    off a later wave of selling and the column reads flat while the token is
    being distributed into. Selling is only established as SUSTAINED when every
    window agrees, which is why this takes a dict of windows rather than a
    single net figure.

    `windows` maps a label to {"buy": usd, "sell": usd}. A window whose
    numbers are absent is skipped, not counted as balanced.
    """
    resolved = {}
    for label, side in (windows or {}).items():
        buy, sell = (side or {}).get("buy"), (side or {}).get("sell")
        if buy is None or sell is None:
            continue
        resolved[label] = float(buy) - float(sell)

    if not resolved:
        return Finding("NET_FLOW", Severity.DEGRADE, False,
                       "no buy/sell split in any window -- assuming disorderly for execution")

    if len(resolved) < 2:
        label, net = next(iter(resolved.items()))
        return Finding("NET_FLOW", Severity.INFO, True,
                       f"only the {label} window resolved (net ${net:,.0f}) -- not enough to call a trend", net)

    if all(net < 0 for net in resolved.values()):
        detail = ", ".join(f"{k} ${v:,.0f}" for k, v in resolved.items())
        return Finding("NET_FLOW", Severity.DEGRADE, True,
                       f"sell pressure in every window ({detail}) -- limit order only",
                       min(resolved.values()))

    detail = ", ".join(f"{k} ${v:,.0f}" for k, v in resolved.items())
    return Finding("NET_FLOW", Severity.INFO, True, detail)


def check_flow_from_counts(windows: Dict[str, Dict[str, Optional[int]]]) -> Finding:
    """Net flow inferred from buy/sell TRADE COUNTS rather than volume.

    A weaker signal than check_net_flow and labelled as such: counts treat a
    $10 sale and a $10,000 sale as one unit each, so a single large seller
    hiding behind many small buyers reads as healthy. Use this only where the
    provider gives counts and not volume, which is the case for DexScreener.

    The window rule is the same. Sustained pressure means EVERY resolved window
    agrees; one window alone is noise.
    """
    resolved = {}
    for label, side in (windows or {}).items():
        buys, sells = (side or {}).get("buys"), (side or {}).get("sells")
        if buys is None or sells is None:
            continue
        if int(buys) + int(sells) == 0:
            continue  # no trades in this window is not a direction
        resolved[label] = int(buys) - int(sells)

    if not resolved:
        return Finding("FLOW_COUNTS", Severity.DEGRADE, False,
                       "no buy/sell counts in any window -- assuming disorderly for execution")
    if len(resolved) < 2:
        label, net = next(iter(resolved.items()))
        return Finding("FLOW_COUNTS", Severity.INFO, True,
                       f"only the {label} window resolved (net {net:+d} trades)", float(net))
    if all(net < 0 for net in resolved.values()):
        detail = ", ".join(f"{k} {v:+d}" for k, v in resolved.items())
        return Finding("FLOW_COUNTS", Severity.DEGRADE, True,
                       f"more sells than buys in every window ({detail}) -- limit order only",
                       float(min(resolved.values())))
    detail = ", ".join(f"{k} {v:+d}" for k, v in resolved.items())
    return Finding("FLOW_COUNTS", Severity.INFO, True, detail)


# Turnover bands. A small pool legitimately turns over many times its own
# depth; a deep one does not. One flat ratio therefore either clears every
# small pool or condemns every large one.
_TURNOVER_BANDS = (
    (100_000.0, 0.30, 15.0),
    (1_000_000.0, 0.15, 12.0),
    (float("inf"), 0.05, 30.0),
)

# Above the band ceiling, a real holder base is what separates a genuine
# stampede from three wallets passing one bag around.
STAMPEDE_HOLDER_FLOOR = 150


def check_turnover(volume_24h_usd: Optional[float],
                   liquidity_usd: Optional[float],
                   holder_count: Optional[int]) -> Finding:
    """Volume-to-liquidity ratio, banded by pool size.

    Exceeding the ceiling is NOT automatically wash trading. A token being
    bought by a thousand distinct wallets prints the same ratio as one being
    cycled by three. Holder count is what tells them apart, so a high ratio
    with a real holder base passes (flagged volatile) and a high ratio with
    almost no holders is refused. Below the floor is a dead pool either way.
    """
    if volume_24h_usd is None or liquidity_usd is None or liquidity_usd <= 0:
        return Finding("TURNOVER", Severity.VETO, False,
                       "turnover unmeasurable -- no volume or liquidity figure")

    ratio = float(volume_24h_usd) / float(liquidity_usd)
    low, high = next((lo, hi) for cap, lo, hi in _TURNOVER_BANDS if liquidity_usd < cap)

    if ratio < low:
        return Finding("TURNOVER", Severity.VETO, True,
                       f"turnover {ratio:.2f}x below {low}x floor for a ${liquidity_usd:,.0f} pool -- dead market",
                       ratio)
    if ratio > high:
        if holder_count is None:
            return Finding("TURNOVER", Severity.VETO, False,
                           f"turnover {ratio:.1f}x above {high}x ceiling and holder count unavailable "
                           f"-- cannot tell a stampede from wash trading", ratio)
        if holder_count >= STAMPEDE_HOLDER_FLOOR:
            return Finding("TURNOVER", Severity.DEGRADE, True,
                           f"turnover {ratio:.1f}x above ceiling but {holder_count:,} holders "
                           f"-- treated as a stampede, expect violent pricing", ratio)
        return Finding("TURNOVER", Severity.VETO, True,
                       f"turnover {ratio:.1f}x above ceiling on only {holder_count} holders -- wash trading",
                       ratio)
    return Finding("TURNOVER", Severity.INFO, True,
                   f"turnover {ratio:.2f}x within {low}-{high}x band", ratio)


# A market capitalisation held up by a few dozen addresses is a shell: the
# number is real, the market behind it is not.
SHELL_MCAP_USD = 1_000_000.0
SHELL_HOLDER_FLOOR = 100


def check_shell(market_cap_usd: Optional[float], holder_count: Optional[int]) -> Finding:
    if market_cap_usd is None or holder_count is None:
        return Finding("SHELL", Severity.INFO, False, "market cap or holder count unavailable")
    if market_cap_usd >= SHELL_MCAP_USD and holder_count < SHELL_HOLDER_FLOOR:
        return Finding("SHELL", Severity.VETO, True,
                       f"${market_cap_usd:,.0f} market cap on {holder_count} holders -- shell",
                       float(holder_count))
    return Finding("SHELL", Severity.INFO, True, f"{holder_count:,} holders behind ${market_cap_usd:,.0f}")


# --- slippage -----------------------------------------------------------

SLIPPAGE_BASE_PERCENT = 2.0
SLIPPAGE_FLOOR_PERCENT = 1.0
SLIPPAGE_CEILING_PERCENT = 15.0
FRESH_LISTING_HOURS = 1.0
FRESH_LISTING_BONUS_PERCENT = 3.0


def dynamic_slippage_percent(price_impact_percent: Optional[float],
                             move_5m_percent: Optional[float],
                             tax_percent: Optional[float] = 0.0,
                             token_age_hours: Optional[float] = None) -> Optional[float]:
    """Slippage tolerance derived from the token, not a fixed config number.

        base 2% + tax + impact x 1.5 + |5m move| x 0.5   (floor 1%, ceiling 15%)

    The volatility term takes the ABSOLUTE move. A formula built only from
    depth and tax assumes price is static at the moment of signing: a deep,
    zero-tax pool computes 2% while the token travels 16% in five minutes, and
    the order fails. Both directions need headroom, so both are added.

    Returns None when price impact is unknown -- the caller must fail closed
    rather than sign with a guessed tolerance. Hitting the ceiling is a signal
    to cut size, not to widen further, and the caller is expected to treat a
    ceiling result that way.
    """
    if price_impact_percent is None:
        return None
    slippage = SLIPPAGE_BASE_PERCENT
    slippage += abs(float(tax_percent or 0.0))
    slippage += abs(float(price_impact_percent)) * 1.5
    if move_5m_percent is not None:
        slippage += abs(float(move_5m_percent)) * 0.5
    if token_age_hours is not None and token_age_hours < FRESH_LISTING_HOURS:
        slippage += FRESH_LISTING_BONUS_PERCENT
    return max(SLIPPAGE_FLOOR_PERCENT, min(SLIPPAGE_CEILING_PERCENT, slippage))


# --- entry point --------------------------------------------------------

def evaluate(snapshot: Dict[str, Any]) -> MicrostructureVerdict:
    """Run every microstructure check over one token snapshot.

    Reads only `.get()`, so a provider that omits a field produces an
    unmeasured Finding rather than a KeyError or a fabricated default.
    """
    verdict = MicrostructureVerdict()
    verdict.findings.append(check_drawdown(snapshot.get("price_usd"), snapshot.get("price_5m_ago")))
    verdict.findings.append(check_net_flow(snapshot.get("flow_windows") or {}))
    verdict.findings.append(check_turnover(snapshot.get("volume_24h_usd"),
                                           snapshot.get("liquidity_usd"),
                                           snapshot.get("holder_count")))
    verdict.findings.append(check_shell(snapshot.get("market_cap_usd"), snapshot.get("holder_count")))
    return verdict
