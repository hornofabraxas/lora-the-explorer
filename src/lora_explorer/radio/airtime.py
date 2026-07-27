"""Base-camp-wide airtime governor.

The per-player 35s survey cooldown (`SURVEY_MIN_INTERVAL_S`) already keeps a
single explorer's flood cadence responsible. This module adds the *aggregate*
protection that a per-player cap can't: several spyglasses reporting home at
once, or a mesh that's already congested for reasons that have nothing to do
with us. It turns the companion's cumulative airtime counters into a rolling
picture of how much of the channel we're using and how busy the channel is,
and exposes three gates that the adapter/engine consult before flooding.

Thresholds are source-backed (see docs/lora-mesh-airtime-review.md):

* MIN_FLOOD_SPACING_S — serialize floods base-camp-wide. Caps the game's own
  contribution no matter how many players are active. 20s is under the 35s
  per-player floor, so a lone explorer is never gated by it.
* TX_BUDGET_PCT — ceiling on our own rolling transmit duty. Meshtastic's
  firmware polices a node's own TX at half the regional duty cycle (5% in the
  strictest EU band); 1% is 5x stricter while still leaving room for ~3
  concurrent drivers at full cadence.
* BUSY_BACKOFF_PCT — channel-busy (TX+RX) level above which we double the
  per-player survey interval. 30% matches the "channel busy" warn line the
  dashboard already shows players, and sits under the 25%/40% community
  channel-utilization thresholds once our own share is excluded.
"""

import asyncio
import time

MIN_FLOOD_SPACING_S = 20.0
TX_BUDGET_PCT = 1.0
BUSY_BACKOFF_PCT = 30.0
# Rolling window the TX / channel-busy rates are measured over. Long enough to
# smooth out a single burst of surveys, short enough to react within a driving
# session.
AIRTIME_WINDOW_S = 600.0
# A rate needs at least this much elapsed airtime-counter span to be meaningful;
# below it we report "unknown" and don't gate.
MIN_RATE_SPAN_S = 30.0
# On-demand samples newer than this are reused rather than re-querying the
# companion before a flood.
SAMPLE_MAX_AGE_S = 15.0
# When congested, multiply the per-player survey interval by this.
CONGESTION_INTERVAL_MULT = 2.0


class AirtimeGovernor:
    """Rolling airtime accounting + flood gates. Not tied to MeshCore so it can
    be unit-tested with a fake clock and hand-fed samples.

    Feed it cumulative companion counters via `observe()`; consult `over_tx_budget`
    / `congested` / `reserve_flood` before transmitting.
    """

    def __init__(
        self,
        *,
        min_flood_spacing_s: float = MIN_FLOOD_SPACING_S,
        tx_budget_pct: float = TX_BUDGET_PCT,
        busy_backoff_pct: float = BUSY_BACKOFF_PCT,
        window_s: float = AIRTIME_WINDOW_S,
        clock=time.monotonic,
    ):
        self._min_flood_spacing_s = min_flood_spacing_s
        self._tx_budget_pct = tx_budget_pct
        self._busy_backoff_pct = busy_backoff_pct
        self._window_s = window_s
        self._clock = clock
        # (mono_ts, uptime_secs, tx_air_secs, rx_air_secs), oldest first.
        self._samples: list[tuple[float, float, float, float]] = []
        self._last_flood_at: float | None = None
        self._lock = asyncio.Lock()

    # --- ingest --------------------------------------------------------------

    def observe(self, uptime_secs, tx_air_secs, rx_air_secs) -> None:
        """Record one cumulative-counter snapshot. Ignores incomplete samples;
        a counter reset (companion reboot → uptime drops) clears history so a
        rate is never computed across the discontinuity."""
        if uptime_secs is None or tx_air_secs is None or rx_air_secs is None:
            return
        try:
            uptime = float(uptime_secs)
            tx = float(tx_air_secs)
            rx = float(rx_air_secs)
        except (TypeError, ValueError):
            return
        now = self._clock()
        if self._samples and uptime < self._samples[-1][1]:
            # Counters reset (reboot) — the running window is no longer coherent.
            self._samples.clear()
        self._samples.append((now, uptime, tx, rx))
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window_s
        # Keep everything within the window plus, if the last in-window sample
        # would leave us with <2 points, one older anchor so a rate still exists.
        while len(self._samples) > 2 and self._samples[1][0] < cutoff:
            self._samples.pop(0)

    # --- rolling rates -------------------------------------------------------

    def _rates(self) -> tuple[float | None, float | None]:
        """(tx_duty_pct, channel_busy_pct) over the window, or (None, None) when
        there isn't enough span to say."""
        if len(self._samples) < 2:
            return (None, None)
        t0, up0, tx0, rx0 = self._samples[0]
        t1, up1, tx1, rx1 = self._samples[-1]
        span = up1 - up0
        if span < MIN_RATE_SPAN_S:
            return (None, None)
        tx_pct = max(0.0, (tx1 - tx0)) / span * 100.0
        busy_pct = max(0.0, (tx1 - tx0) + (rx1 - rx0)) / span * 100.0
        return (tx_pct, busy_pct)

    def tx_duty_pct(self) -> float | None:
        return self._rates()[0]

    def channel_busy_pct(self) -> float | None:
        return self._rates()[1]

    def over_tx_budget(self) -> bool:
        pct = self.tx_duty_pct()
        return pct is not None and pct >= self._tx_budget_pct

    def congested(self) -> bool:
        pct = self.channel_busy_pct()
        return pct is not None and pct >= self._busy_backoff_pct

    def last_sample_age(self) -> float | None:
        if not self._samples:
            return None
        return self._clock() - self._samples[-1][0]

    # --- flood gate ----------------------------------------------------------

    async def reserve_flood(self) -> bool:
        """Gate one outbound flood. Returns False when we're over our rolling TX
        budget — the caller must NOT flood. Otherwise enforces the base-camp-wide
        minimum spacing (sleeping under a lock so concurrent floods serialize
        rather than bunch up) and returns True."""
        async with self._lock:
            if self.over_tx_budget():
                return False
            now = self._clock()
            if self._last_flood_at is not None:
                wait = self._last_flood_at + self._min_flood_spacing_s - now
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_flood_at = self._clock()
            return True
