"""The sale planner releasing energy that arbitrage bought to be sold.

Arbitrage decides *that* extra energy is bought for a sale. The sale planner
decides whether it may really leave the battery: only energy that is there,
only beyond the demand protected until sunrise, never beyond the power limits,
and never when the horizon coverage is broken.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.high_price_discharge import (
    ArbitrageExport,
    HorizonSlot,
    REASON_COVERAGE_GAP,
    REASON_NO_SURPLUS,
    REASON_NO_USABLE_ENERGY,
    STATUS_FAIL_SAFE,
    STATUS_NO_OPPORTUNITY,
    STATUS_PLANNED,
    plan_high_price_discharge,
)

DAY = datetime(2026, 10, 14, tzinfo=timezone.utc)
HORIZON = 12


def _slots(*, consumption=0.5, export=None, hours=HORIZON):
    export = export or {}
    return [
        HorizonSlot(
            start=DAY + timedelta(hours=h),
            end=DAY + timedelta(hours=h + 1),
            export_price=export.get(h, 0.20),
            import_price=0.20,
            consumption_kwh=consumption,
            solar_kwh=0.0,
        )
        for h in range(hours)
    ]


def _battery(*, soc=90.0, power=5000.0, floor=20.0, capacity=10.0):
    return BatterySnapshot("b", soc, capacity, 100.0, floor, power)


def _request(hour, kwh, hours=1.0):
    start = DAY + timedelta(hours=hour)
    return ArbitrageExport(start, start + timedelta(hours=hours), kwh)


def _plan(exports, *, slots=None, battery=None, **overrides):
    kwargs = dict(
        now=DAY,
        horizon_end=DAY + timedelta(hours=HORIZON),
        enabled=False,
        trigger_1_enabled=False,
        discharge_efficiency=1.0,
        chronological_coverage=False,
        arbitrage_exports=exports,
    )
    kwargs.update(overrides)
    return plan_high_price_discharge(
        slots or _slots(export={7: 0.50}), [battery or _battery()], **kwargs
    )


def test_exports_alone_turn_the_planner_on():
    plan = _plan([_request(7, 1.0)])
    assert plan.status == STATUS_PLANNED
    assert plan.total_allocated_kwh == pytest.approx(1.0)
    assert plan.arbitrage_kwh == pytest.approx(1.0)
    allocation = plan.allocations[0]
    assert allocation.start == DAY + timedelta(hours=7)
    assert allocation.export_price == 0.50
    assert allocation.arbitrage_kwh == pytest.approx(1.0)
    assert allocation.power_w == pytest.approx(1000.0)


def test_nothing_requested_and_nothing_enabled_stays_disabled():
    plan = _plan([])
    assert plan.status == "disabled"


def test_only_energy_beyond_the_protected_demand_may_be_sold():
    # 7 kWh usable, 6 kWh protected demand: 1 kWh is free to sell.
    plan = _plan([_request(7, 5.0)])
    assert plan.protected_demand_kwh == pytest.approx(6.0)
    assert plan.usable_energy_kwh == pytest.approx(7.0)
    assert plan.total_allocated_kwh == pytest.approx(1.0)


def test_the_safety_margin_is_kept_back():
    plan = _plan([_request(7, 5.0)], safety_margin_kwh=0.4)
    assert plan.total_allocated_kwh == pytest.approx(0.6)


def test_a_battery_that_never_received_the_energy_sells_nothing():
    plan = _plan([_request(7, 3.0)], battery=_battery(soc=60.0))
    assert plan.status == STATUS_NO_OPPORTUNITY
    assert plan.reason == REASON_NO_SURPLUS
    assert plan.allocations == ()


def test_a_battery_at_its_floor_reports_no_usable_energy():
    plan = _plan([_request(7, 3.0)], battery=_battery(soc=20.0))
    assert plan.status == STATUS_NO_OPPORTUNITY
    assert plan.reason == REASON_NO_USABLE_ENERGY


def test_the_bought_energy_is_sold_once_it_has_arrived():
    asked = [_request(7, 3.0)]
    before = _plan(asked, battery=_battery(soc=60.0))
    after = _plan(asked, battery=_battery(soc=100.0))
    assert before.total_allocated_kwh == 0.0
    # 8 kWh usable - 6 kWh protected = 2 kWh may go; the rest is held back.
    assert after.total_allocated_kwh == pytest.approx(2.0)


def test_a_short_budget_goes_to_the_dearest_period():
    slots = _slots(export={6: 0.40, 8: 0.55})
    plan = _plan([_request(6, 2.0), _request(8, 2.0)], slots=slots)
    assert [a.start.hour for a in plan.allocations] == [8]
    assert plan.total_allocated_kwh == pytest.approx(1.0)


def test_a_large_budget_spreads_over_every_requested_period():
    slots = _slots(export={6: 0.40, 8: 0.55})
    plan = _plan([_request(6, 1.0), _request(8, 1.0)], slots=slots, battery=_battery(soc=100.0))
    assert [a.start.hour for a in plan.allocations] == [6, 8]
    assert plan.total_allocated_kwh == pytest.approx(2.0)


def test_the_sale_is_limited_by_battery_power_after_the_house_has_its_share():
    # 2 kW of battery power, the house takes 0.5 kW: 1.5 kWh left in the hour.
    plan = _plan(
        [_request(7, 5.0)],
        battery=_battery(soc=100.0, power=2000.0),
    )
    assert plan.allocations[0].energy_kwh == pytest.approx(1.5)
    assert plan.allocations[0].power_w == pytest.approx(1500.0)


def test_power_comes_from_the_battery_not_from_a_constant():
    small = _plan([_request(7, 5.0)], battery=_battery(soc=100.0, power=1200.0))
    large = _plan([_request(7, 5.0)], battery=_battery(soc=100.0, power=4000.0))
    assert small.allocations[0].energy_kwh < large.allocations[0].energy_kwh


def test_a_request_spanning_part_of_a_period_is_allocated_pro_rata():
    request = ArbitrageExport(
        DAY + timedelta(hours=7), DAY + timedelta(hours=9), 2.0
    )  # 1 kWh per hour
    slots = _slots(export={7: 0.50, 8: 0.50})
    plan = _plan([request], slots=slots, battery=_battery(soc=100.0))
    assert [a.start.hour for a in plan.allocations] == [7, 8]
    assert [a.energy_kwh for a in plan.allocations] == pytest.approx([1.0, 1.0])


@pytest.mark.parametrize("price", [None, 0.0, -0.05, float("nan")])
def test_a_period_without_a_usable_export_price_is_never_sold_into(price):
    slots = _slots(export={7: price})
    plan = _plan([_request(7, 1.0)], slots=slots, battery=_battery(soc=100.0))
    assert plan.allocations == ()


@pytest.mark.parametrize(
    "bad",
    [
        ArbitrageExport(DAY.replace(tzinfo=None), DAY.replace(tzinfo=None) + timedelta(hours=1), 1.0),
        ArbitrageExport(DAY + timedelta(hours=7), DAY + timedelta(hours=7), 1.0),
        ArbitrageExport(DAY + timedelta(hours=7), DAY + timedelta(hours=8), 0.0),
        ArbitrageExport(DAY + timedelta(hours=7), DAY + timedelta(hours=8), float("nan")),
        ArbitrageExport(DAY + timedelta(hours=7), DAY + timedelta(hours=8), -1.0),
    ],
)
def test_malformed_requests_are_ignored(bad):
    plan = _plan([bad], battery=_battery(soc=100.0))
    assert plan.allocations == ()


def test_a_period_in_the_past_is_not_sold_into():
    plan = _plan(
        [_request(-3, 2.0)],
        slots=_slots(export={7: 0.50}),
        battery=_battery(soc=100.0),
    )
    assert plan.allocations == ()


def test_a_coverage_gap_still_fails_safe():
    slots = _slots(export={7: 0.50})
    del slots[4]
    plan = _plan([_request(7, 1.0)], slots=slots, battery=_battery(soc=100.0))
    assert plan.status == STATUS_FAIL_SAFE
    assert plan.reason == REASON_COVERAGE_GAP
    assert plan.allocations == ()


def test_invalid_horizon_still_fails_safe():
    plan = _plan([_request(7, 1.0)], horizon_end=DAY - timedelta(hours=1))
    assert plan.status == STATUS_FAIL_SAFE
    assert plan.allocations == ()


def test_sales_never_dip_below_the_floor_whatever_is_asked():
    for soc in (20.0, 25.0, 40.0, 70.0, 100.0):
        plan = _plan([_request(7, 50.0)], battery=_battery(soc=soc))
        usable = max(0.0, (soc - 20.0) / 100.0 * 10.0)
        assert plan.total_allocated_kwh <= usable + 1e-9
        assert plan.total_allocated_kwh <= max(0.0, usable - plan.protected_demand_kwh) + 1e-9


def test_efficiency_shrinks_what_may_be_sold():
    full = _plan([_request(7, 9.0)], battery=_battery(soc=100.0), discharge_efficiency=1.0)
    lossy = _plan([_request(7, 9.0)], battery=_battery(soc=100.0), discharge_efficiency=0.8)
    assert lossy.usable_energy_kwh == pytest.approx(0.8 * full.usable_energy_kwh)
    assert lossy.total_allocated_kwh < full.total_allocated_kwh


def test_trigger_2_and_arbitrage_exports_share_one_budget():
    # Trigger 2 on: a dear period with a later deficit it can buy back.
    slots = [
        HorizonSlot(
            start=DAY + timedelta(hours=h),
            end=DAY + timedelta(hours=h + 1),
            export_price=0.60 if h == 3 else 0.25,
            import_price=0.20,
            consumption_kwh=0.5,
            solar_kwh=0.0,
        )
        for h in range(HORIZON)
    ]
    only_t2 = plan_high_price_discharge(
        slots, [_battery(soc=100.0)], now=DAY, horizon_end=DAY + timedelta(hours=HORIZON),
        enabled=True, discharge_efficiency=1.0, chronological_coverage=False,
    )
    both = plan_high_price_discharge(
        slots, [_battery(soc=100.0)], now=DAY, horizon_end=DAY + timedelta(hours=HORIZON),
        enabled=True, discharge_efficiency=1.0, chronological_coverage=False,
        arbitrage_exports=[_request(3, 5.0)],
    )
    assert both.total_allocated_kwh >= only_t2.total_allocated_kwh
    # Never more than the battery holds above its floor.
    assert both.total_allocated_kwh <= both.usable_energy_kwh + 1e-9
