import asyncio

import pytest

from lora_explorer.radio.airtime import AirtimeGovernor


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _gov(**kw):
    clock = FakeClock()
    gov = AirtimeGovernor(clock=clock, **kw)
    return gov, clock


def test_no_rate_without_enough_span():
    gov, clock = _gov()
    gov.observe(uptime_secs=100, tx_air_secs=1, rx_air_secs=1)
    # Only one sample → unknown, and nothing is gated.
    assert gov.tx_duty_pct() is None
    assert gov.channel_busy_pct() is None
    assert gov.over_tx_budget() is False
    assert gov.congested() is False


def test_tx_duty_and_budget():
    gov, clock = _gov(tx_budget_pct=1.0)
    gov.observe(uptime_secs=1000, tx_air_secs=10, rx_air_secs=50)
    clock.advance(120)
    # +120s uptime, +3s TX → 2.5% TX duty, over the 1% budget.
    gov.observe(uptime_secs=1120, tx_air_secs=13, rx_air_secs=60)
    assert gov.tx_duty_pct() == pytest.approx(2.5)
    assert gov.over_tx_budget() is True


def test_under_budget_not_flagged():
    gov, clock = _gov(tx_budget_pct=1.0)
    gov.observe(uptime_secs=1000, tx_air_secs=10, rx_air_secs=50)
    clock.advance(600)
    # +600s uptime, +3s TX → 0.5% TX duty, under budget.
    gov.observe(uptime_secs=1600, tx_air_secs=13, rx_air_secs=90)
    assert gov.tx_duty_pct() == pytest.approx(0.5)
    assert gov.over_tx_budget() is False


def test_congestion_from_channel_busy():
    gov, clock = _gov(busy_backoff_pct=30.0)
    gov.observe(uptime_secs=1000, tx_air_secs=10, rx_air_secs=100)
    clock.advance(100)
    # +100s uptime, +2s TX +38s RX → 40% busy, over the 30% backoff line.
    gov.observe(uptime_secs=1100, tx_air_secs=12, rx_air_secs=138)
    assert gov.channel_busy_pct() == pytest.approx(40.0)
    assert gov.congested() is True


def test_counter_reset_clears_history():
    gov, clock = _gov()
    gov.observe(uptime_secs=5000, tx_air_secs=100, rx_air_secs=200)
    clock.advance(60)
    # Uptime drops → companion rebooted; the running window must reset so no
    # bogus (negative-then-clamped) rate spans the discontinuity.
    gov.observe(uptime_secs=30, tx_air_secs=0, rx_air_secs=0)
    assert gov.tx_duty_pct() is None
    clock.advance(60)
    gov.observe(uptime_secs=90, tx_air_secs=1, rx_air_secs=1)
    assert gov.tx_duty_pct() == pytest.approx(1 / 60 * 100)


def test_window_evicts_old_samples():
    gov, clock = _gov(window_s=600)
    gov.observe(uptime_secs=1000, tx_air_secs=0, rx_air_secs=0)
    # Walk forward well past the window with steady low TX.
    for i in range(1, 20):
        clock.advance(120)
        gov.observe(uptime_secs=1000 + i * 120, tx_air_secs=i * 1, rx_air_secs=i * 5)
    # Rate reflects only the in-window tail, not boot.
    pct = gov.tx_duty_pct()
    assert pct == pytest.approx(1 / 120 * 100, abs=0.2)


def test_reserve_flood_blocks_over_budget():
    async def go():
        gov, clock = _gov(tx_budget_pct=1.0)
        gov.observe(uptime_secs=1000, tx_air_secs=10, rx_air_secs=50)
        clock.advance(60)
        gov.observe(uptime_secs=1060, tx_air_secs=13, rx_air_secs=60)  # 5% TX
        assert gov.over_tx_budget() is True
        assert await gov.reserve_flood() is False
    asyncio.run(go())


def test_reserve_flood_enforces_spacing():
    # Real event loop + real sleep, but with a tiny spacing so the test is fast.
    async def go():
        gov = AirtimeGovernor(min_flood_spacing_s=0.05)
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        assert await gov.reserve_flood() is True  # first is immediate
        assert await gov.reserve_flood() is True  # second waits ~spacing
        elapsed = loop.time() - t0
        assert elapsed >= 0.05
    asyncio.run(go())
