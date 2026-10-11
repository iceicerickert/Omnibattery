"""Engine and controller tests for price arbitrage charging.

The pure planner is covered in ``test_arbitrage_planner.py``. These exercise the
glue: how the plan becomes typed slots next to deficit and negative-price slots,
how a slot is authorised and sized at runtime, how it is withdrawn, and what the
sensors report.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    CONF_ARBITRAGE_CHARGING_ENABLED,
    CONF_ARBITRAGE_WEAR_COST,
    DEFAULT_ARBITRAGE_CHARGING_ENABLED,
    DEFAULT_ARBITRAGE_WEAR_COST,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
)
from custom_components.omnibattery.number import ArbitrageWearCostNumber
from custom_components.omnibattery.pricing import (
    CurtailmentPlan,
    DynamicPricingSchedule,
    PriceSlot,
    SLOT_PURPOSE_ARBITRAGE,
    SLOT_PURPOSE_COMBINED,
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
)
from custom_components.omnibattery.pricing.arbitrage import (
    REASON_NEEDS_DISCHARGE_HOLD,
    REASON_NO_FLEET,
)
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)
from custom_components.omnibattery.sensor import ArbitragePlanSensor
from custom_components.omnibattery.switch import ArbitrageChargingSwitch


class _Battery:
    def __init__(
        self,
        name: str,
        soc: float,
        capacity_kwh: float,
        *,
        max_soc: float = 100.0,
        min_soc: float = 10.0,
        available: bool = True,
    ) -> None:
        self.name = name
        self.max_soc = max_soc
        self.min_soc = min_soc
        self.is_available = available
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


def _decision(*, should_charge: bool = False, deficit: float = 0.0) -> dict:
    return {
        "should_charge": should_charge,
        "avg_soc": 30.0,
        "avg_consumption_kwh": 4.0,
        "energy_deficit_kwh": deficit,
        "planned_grid_charge_kwh": deficit,
        "solar_forecast_kwh": None,
        "usable_energy_kwh": 2.0,
        "total_available_kwh": 2.0,
        "days_in_history": 0,
    }


def _reserve_stub(provider):
    return SimpleNamespace(
        _demand_and_surplus_by_slot=provider,
        mark_stale=lambda _reason="": None,
        clear=lambda _reason="": None,
        async_rebuild_plan=_noop,
    )


def _demand_by_price(threshold: float = 0.30, kwh: float = 1.0):
    """A reserve-manager stand-in: dear slots carry household demand."""

    def provider(_pricing, slots, _now, _end):
        return {s: (kwh if s.price >= threshold else 0.0) for s in slots}, {}

    return _reserve_stub(provider)


def _controller(
    batteries: list[_Battery],
    *,
    arbitrage: bool = True,
    hold: bool = True,
    decision: dict | None = None,
    demand=None,
) -> SimpleNamespace:
    async def should_activate():
        return decision or _decision()

    ctrl = SimpleNamespace(
        coordinators=batteries,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        predictive_charging_overridden=False,
        negative_price_charging_enabled=False,
        arbitrage_charging_enabled=arbitrage,
        arbitrage_wear_cost=0.0,
        discharge_reserve_enabled=hold,
        price_discharge_control=False,
        smart_predischarge_enabled=False,
        max_contracted_power=4000,
        max_charge_capacity=4000,
        max_discharge_capacity=4000,
        max_price_threshold=None,
        min_arbitrage_margin=None,
        round_trip_efficiency=DEFAULT_ROUND_TRIP_EFFICIENCY,
        price_integration_type="nordpool",
        price_sensor="sensor.price",
        _discharge_reserve_mgr=demand or _demand_by_price(),
        _should_activate_grid_charging=should_activate,
        _last_decision_data=None,
        _dynamic_pricing_schedule=None,
        _dynamic_pricing_evaluated_date=None,
        _dp_eval_retry_count=0,
        _dp_last_eval_soc=None,
        _dp_arbitrage_ceiling=None,
        _dp_last_arbitrage_plan=None,
        _dp_daily_avg_price=None,
        _dp_pre_evaluated_slots={},
        _dp_pre_evaluated_purposes={},
        _dp_completed_slots=set(),
        _current_price_slot_active=False,
        _active_dynamic_slot_purpose=None,
        _active_dynamic_price_slot=None,
        _predictive_charge_target_soc=None,
        _predictive_deficit_target_soc=None,
        grid_charging_active=False,
        _grid_charging_initialized=False,
        previous_power=0,
        previous_error=0,
        first_execution=True,
    )
    ctrl._compute_arbitrage_target_soc = lambda slot=None: (
        ChargeDischargeController._compute_arbitrage_target_soc(ctrl, slot)
    )
    return ctrl


async def _noop(*_args, **_kwargs):
    return None


def _manager(ctrl: SimpleNamespace, slots: list[PriceSlot]) -> PricingManager:
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._parse_price_data = lambda horizon_end=None, **_kwargs: slots
    manager.get_future_price_slots = lambda horizon_end=None: slots
    manager.energy_horizon_end = lambda now: now + timedelta(hours=40)
    manager._send_dynamic_pricing_notification = _noop
    manager._build_curtailment_plan = lambda *_args, **_kwargs: CurtailmentPlan(
        status="no_risk", reason="none"
    )
    return manager


def _evaluate(ctrl: SimpleNamespace, slots: list[PriceSlot]) -> PricingManager:
    manager = _manager(ctrl, slots)
    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )
    return manager


def _future_slots(prices: list[float], minutes: int = 60, offset_h: float = 2) -> list[PriceSlot]:
    start = datetime.now() + timedelta(hours=offset_h)
    return [
        PriceSlot(
            start=start + timedelta(minutes=minutes * i),
            end=start + timedelta(minutes=minutes * (i + 1)),
            price=price,
        )
        for i, price in enumerate(prices)
    ]


# cheap now, dear later
PRICES = [0.10, 0.10, 0.12, 0.25, 0.40, 0.40]


def _arbitrage_slots(schedule) -> list[PriceSlot]:
    return [s for s in schedule.selected_slots if schedule.purpose_for(s) == SLOT_PURPOSE_ARBITRAGE]


# ---------------------------------------------------------------------------
# Defaults and scope
# ---------------------------------------------------------------------------


def test_feature_is_opt_in_with_no_wear_by_default():
    assert DEFAULT_ARBITRAGE_CHARGING_ENABLED is False
    assert DEFAULT_ARBITRAGE_WEAR_COST == 0.0


def test_disabled_feature_leaves_the_schedule_alone():
    ctrl = _controller([_Battery("b1", 20, 10)], arbitrage=False)
    _evaluate(ctrl, _future_slots(PRICES))
    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.charging_needed is False
    assert schedule.arbitrage_charging_needed is False
    assert not _arbitrage_slots(schedule)
    assert ctrl._dp_last_arbitrage_plan is None


def test_scope_is_dynamic_pricing_only():
    ctrl = _controller([_Battery("b1", 20, 10)])
    manager = PricingManager(SimpleNamespace(), ctrl)
    assert manager._arbitrage_feature_enabled() is True
    ctrl.predictive_charging_mode = "realtime_price"
    assert manager._arbitrage_feature_enabled() is False
    ctrl.predictive_charging_mode = PREDICTIVE_MODE_DYNAMIC_PRICING
    ctrl.predictive_charging_enabled = False
    assert manager._arbitrage_feature_enabled() is False


# ---------------------------------------------------------------------------
# Building the calendar
# ---------------------------------------------------------------------------


def test_cheap_slots_become_arbitrage_slots_for_a_dear_evening():
    ctrl = _controller([_Battery("b1", 20, 10)])
    slots = _future_slots(PRICES)

    _evaluate(ctrl, slots)

    schedule = ctrl._dynamic_pricing_schedule
    arbitrage = _arbitrage_slots(schedule)
    assert arbitrage
    assert all(slot.price <= 0.12 for slot in arbitrage)
    assert set(schedule.slot_purposes.values()) == {SLOT_PURPOSE_ARBITRAGE}
    assert schedule.schedule_type == SLOT_PURPOSE_ARBITRAGE
    assert schedule.charging_needed is True
    assert schedule.arbitrage_charging_needed is True
    assert schedule.deficit_charging_needed is False
    assert schedule.negative_price_charging_needed is False
    assert schedule.arbitrage_energy_kwh == pytest.approx(
        sum(schedule.slot_arbitrage_kwh.values())
    )
    assert set(schedule.slot_arbitrage_kwh) == set(arbitrage)
    assert schedule.arbitrage_plan.expected_gain > 0
    assert ctrl._dp_last_arbitrage_plan is schedule.arbitrage_plan


def test_estimated_cost_prices_the_energy_actually_bought():
    ctrl = _controller([_Battery("b1", 20, 10)])
    _evaluate(ctrl, _future_slots(PRICES))
    schedule = ctrl._dynamic_pricing_schedule
    plan = schedule.arbitrage_plan
    assert schedule.estimated_cost == pytest.approx(
        sum(charge.grid_kwh * charge.price for charge in plan.charges)
    )


def test_energy_is_bounded_by_the_demand_it_can_replace():
    ctrl = _controller([_Battery("b1", 20, 20)])  # lots of room
    _evaluate(ctrl, _future_slots(PRICES))
    plan = ctrl._dynamic_pricing_schedule.arbitrage_plan
    # Two dear hours of 1 kWh each (and the 0.25 hour has none).
    assert plan.total_stored_kwh <= 2.0 / (DEFAULT_ROUND_TRIP_EFFICIENCY**0.5) + 1e-6


def test_nothing_is_planned_without_a_worthwhile_spread():
    ctrl = _controller([_Battery("b1", 20, 10)])
    _evaluate(ctrl, _future_slots([0.30, 0.30, 0.32, 0.31]))
    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.arbitrage_charging_needed is False
    assert not _arbitrage_slots(schedule)
    assert ctrl._dp_last_arbitrage_plan.reason == "no_profitable_spread"


def test_wear_cost_can_make_a_marginal_spread_unprofitable():
    prices = [0.10, 0.10, 0.20, 0.20]
    slots = _future_slots(prices)
    cheap = _controller([_Battery("b1", 20, 10)], demand=_demand_by_price(0.19))
    _evaluate(cheap, slots)
    assert _arbitrage_slots(cheap._dynamic_pricing_schedule)

    worn = _controller([_Battery("b1", 20, 10)], demand=_demand_by_price(0.19))
    worn.arbitrage_wear_cost = 0.08
    _evaluate(worn, slots)
    assert worn._dp_last_arbitrage_plan.planned is False


def test_minimum_margin_is_honoured():
    slots = _future_slots([0.10, 0.10, 0.20, 0.20])
    ctrl = _controller([_Battery("b1", 20, 10)], demand=_demand_by_price(0.19))
    ctrl.min_arbitrage_margin = 0.08
    _evaluate(ctrl, slots)
    assert ctrl._dp_last_arbitrage_plan.planned is False


def test_static_price_ceiling_limits_where_arbitrage_buys():
    ctrl = _controller([_Battery("b1", 20, 10)])
    ctrl.max_price_threshold = 0.105
    _evaluate(ctrl, _future_slots(PRICES))
    slots = _arbitrage_slots(ctrl._dynamic_pricing_schedule)
    assert slots and all(slot.price <= 0.105 for slot in slots)


def test_a_full_battery_buys_nothing():
    ctrl = _controller([_Battery("b1", 100, 10)])
    _evaluate(ctrl, _future_slots(PRICES))
    assert ctrl._dp_last_arbitrage_plan.planned is False
    assert ctrl._dynamic_pricing_schedule.arbitrage_charging_needed is False


def test_energy_already_stored_reduces_what_is_bought():
    empty = _controller([_Battery("b1", 10, 10)])
    half = _controller([_Battery("b1", 40, 10)])
    slots = _future_slots(PRICES)
    _evaluate(empty, slots)
    _evaluate(half, slots)
    assert (
        half._dp_last_arbitrage_plan.total_stored_kwh
        < empty._dp_last_arbitrage_plan.total_stored_kwh
    )


def test_no_discharge_hold_means_no_plan():
    ctrl = _controller([_Battery("b1", 20, 10)], hold=False)
    _evaluate(ctrl, _future_slots(PRICES))
    assert ctrl._dp_last_arbitrage_plan.reason == REASON_NEEDS_DISCHARGE_HOLD
    assert not _arbitrage_slots(ctrl._dynamic_pricing_schedule)


def test_price_based_discharge_also_counts_as_a_hold():
    ctrl = _controller([_Battery("b1", 20, 10)], hold=False)
    ctrl.price_discharge_control = True
    _evaluate(ctrl, _future_slots(PRICES))
    assert _arbitrage_slots(ctrl._dynamic_pricing_schedule)


def test_no_readable_battery_means_no_plan():
    ctrl = _controller([_Battery("b1", 20, 10, available=False)])
    _evaluate(ctrl, _future_slots(PRICES))
    assert ctrl._dp_last_arbitrage_plan.reason == REASON_NO_FLEET


def test_failing_demand_forecast_plans_nothing_instead_of_raising():
    def boom(*_args):
        raise RuntimeError("no profile")

    ctrl = _controller(
        [_Battery("b1", 20, 10)],
        demand=_reserve_stub(boom),
    )
    _evaluate(ctrl, _future_slots(PRICES))
    assert not _arbitrage_slots(ctrl._dynamic_pricing_schedule)


def test_fleet_energy_respects_floor_ceiling_and_unreadable_batteries():
    good = _Battery("good", 50, 10, min_soc=10, max_soc=90)
    gone = _Battery("gone", 50, 10, available=False)
    broken = _Battery("broken", 50, 10)
    broken.data["battery_soc"] = None
    manager = PricingManager(SimpleNamespace(), _controller([good, gone, broken]))
    usable, free = manager._arbitrage_fleet_energy()
    assert usable == pytest.approx(4.0)  # (50 - 10)% of 10 kWh
    assert free == pytest.approx(4.0)  # (90 - 50)% of 10 kWh


def test_quarter_hour_prices_work():
    ctrl = _controller([_Battery("b1", 20, 10)])
    slots = _future_slots(
        [0.10] * 8 + [0.25] * 8 + [0.40] * 8, minutes=15, offset_h=1
    )
    _evaluate(ctrl, slots)
    assert _arbitrage_slots(ctrl._dynamic_pricing_schedule)


# ---------------------------------------------------------------------------
# Next to the other purposes
# ---------------------------------------------------------------------------


def test_deficit_slots_are_never_offered_to_arbitrage():
    ctrl = _controller(
        [_Battery("b1", 20, 10)],
        decision=_decision(should_charge=True, deficit=3.0),
    )
    slots = _future_slots(PRICES)
    _evaluate(ctrl, slots)
    schedule = ctrl._dynamic_pricing_schedule
    purposes = list(schedule.slot_purposes.values())
    assert SLOT_PURPOSE_COMBINED not in purposes
    assert len(set(schedule.selected_slots)) == len(schedule.selected_slots)
    deficit = [s for s in schedule.selected_slots if schedule.purpose_for(s) == SLOT_PURPOSE_DEFICIT]
    assert deficit
    assert not set(deficit) & set(_arbitrage_slots(schedule))
    assert schedule.deficit_charging_needed is True


def test_informational_cheap_calendar_is_dropped_when_arbitrage_is_the_only_job():
    ctrl = _controller([_Battery("b1", 20, 10)])
    _evaluate(ctrl, _future_slots(PRICES))
    schedule = ctrl._dynamic_pricing_schedule
    assert SLOT_PURPOSE_DEFICIT not in schedule.slot_purposes.values()


def test_negative_price_slot_keeps_its_purpose_beside_arbitrage():
    ctrl = _controller([_Battery("b1", 20, 20)])
    ctrl.negative_price_charging_enabled = True
    slots = _future_slots([-0.10, 0.10, 0.10, 0.12, 0.40, 0.40])
    _evaluate(ctrl, slots)
    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.purpose_for(slots[0]) == SLOT_PURPOSE_NEGATIVE_PRICE
    assert slots[0] not in schedule.slot_arbitrage_kwh


def test_solar_risk_window_removes_arbitrage_slots_from_the_calendar():
    ctrl = _controller([_Battery("b1", 20, 10)])
    slots = _future_slots(PRICES)
    manager = _manager(ctrl, slots)
    manager._build_curtailment_plan = lambda *_args, **_kwargs: CurtailmentPlan(
        status="planned", reason="solar_risk", risk_slots=[slots[0], slots[1]]
    )
    asyncio.run(
        manager._evaluate_dynamic_pricing(horizon=DynamicPricingEvaluationHorizon.DAILY)
    )
    schedule = ctrl._dynamic_pricing_schedule
    chosen = _arbitrage_slots(schedule) if schedule else []
    assert slots[0] not in chosen and slots[1] not in chosen
    if schedule:
        assert set(schedule.slot_arbitrage_kwh) == set(chosen)


def test_schedule_type_labels():
    mgr = PricingManager(SimpleNamespace(), _controller([]))
    f = mgr._schedule_type_from_purposes
    assert f([SLOT_PURPOSE_ARBITRAGE]) == SLOT_PURPOSE_ARBITRAGE
    assert f([SLOT_PURPOSE_ARBITRAGE, SLOT_PURPOSE_ARBITRAGE]) == SLOT_PURPOSE_ARBITRAGE
    assert f([SLOT_PURPOSE_ARBITRAGE, SLOT_PURPOSE_DEFICIT]) == SLOT_PURPOSE_COMBINED
    assert f([SLOT_PURPOSE_ARBITRAGE, SLOT_PURPOSE_NEGATIVE_PRICE]) == SLOT_PURPOSE_COMBINED
    assert f([SLOT_PURPOSE_DEFICIT]) == SLOT_PURPOSE_DEFICIT
    assert f([SLOT_PURPOSE_NEGATIVE_PRICE]) == SLOT_PURPOSE_NEGATIVE_PRICE


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _arb_schedule(slot: PriceSlot, kwh: float, *, deficit_slots=()) -> DynamicPricingSchedule:
    slots = [slot, *deficit_slots]
    purposes = {slot: SLOT_PURPOSE_ARBITRAGE}
    purposes.update({s: SLOT_PURPOSE_DEFICIT for s in deficit_slots})
    return DynamicPricingSchedule(
        hours_needed=1.0,
        selected_slots=slots,
        average_price=slot.price,
        estimated_cost=0.0,
        total_available_slots=len(slots),
        evaluation_time=datetime.now(),
        energy_deficit_kwh=0.0,
        charging_needed=True,
        slot_purposes=purposes,
        schedule_type=SLOT_PURPOSE_ARBITRAGE,
        deficit_charging_needed=bool(deficit_slots),
        arbitrage_charging_needed=True,
        arbitrage_energy_kwh=kwh,
        slot_arbitrage_kwh={slot: kwh},
    )


def _live_slot(price: float = 0.10) -> PriceSlot:
    now = datetime.now()
    return PriceSlot(now - timedelta(minutes=10), now + timedelta(minutes=50), price)


def _runtime(ctrl: SimpleNamespace, slot: PriceSlot, kwh: float, commands: list) -> PricingManager:
    ctrl._dynamic_pricing_schedule = _arb_schedule(slot, kwh)
    ctrl._dynamic_pricing_evaluated_date = datetime.now().date()
    ctrl._dp_evening_reevaluated_date = datetime.now().date()
    ctrl.is_charge_blocked = lambda: False

    async def set_power(battery, charge, discharge):
        commands.append((battery.name, charge, discharge))

    async def handle():
        # What the physical handler does at slot start: size the target once.
        if ctrl._predictive_charge_target_soc is None:
            ctrl._predictive_charge_target_soc = (
                ChargeDischargeController._compute_predictive_target_soc(ctrl)
            )
            ctrl._grid_charging_initialized = True

    ctrl._set_battery_power = set_power
    ctrl._handle_predictive_grid_charging = handle
    manager = PricingManager(SimpleNamespace(), ctrl)
    manager._maybe_refresh_service_prices = _noop
    manager._check_dp_pre_slot_reevaluation = _noop
    manager._is_evening_reevaluation_time = lambda: False
    manager._is_dp_soc_drop_reeval = lambda: False
    manager._is_price_publication_reeval = lambda _now: False
    manager._get_current_price = lambda: slot.price
    manager._slot_overlaps_curtailment_risk = lambda _slot: False
    manager._send_dynamic_pricing_slot_start_notification = _noop
    return manager


def test_slot_is_entered_with_a_target_sized_from_its_planned_energy():
    battery = _Battery("b1", 50, 10)
    ctrl = _controller([battery])
    slot = _live_slot()
    manager = _runtime(ctrl, slot, 1.5, [])

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert ctrl._current_price_slot_active is True
    assert ctrl._active_dynamic_slot_purpose == SLOT_PURPOSE_ARBITRAGE
    assert ctrl._active_dynamic_price_slot == slot
    assert ctrl._predictive_charge_target_soc[battery] == pytest.approx(65.0)


def test_slot_stops_when_its_planned_energy_has_been_delivered():
    battery = _Battery("b1", 50, 10)
    ctrl = _controller([battery])
    slot = _live_slot()
    commands: list = []
    manager = _runtime(ctrl, slot, 1.0, commands)

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl._current_price_slot_active is True

    battery.data["battery_soc"] = 60.0

    async def stopped():
        ctrl.grid_charging_active = False

    ctrl._handle_predictive_grid_charging = stopped
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert ctrl._current_price_slot_active is False
    assert slot.start in ctrl._dp_completed_slots
    assert ("b1", 0, 0) in commands

    # The slot is spent: a dip in SOC must not buy it again.
    battery.data["battery_soc"] = 55.0
    ctrl._handle_predictive_grid_charging = _noop
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl._current_price_slot_active is False


def test_slot_is_not_entered_when_the_battery_is_already_full():
    ctrl = _controller([_Battery("b1", 100, 10)])
    slot = _live_slot()
    manager = _runtime(ctrl, slot, 2.0, [])
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl._current_price_slot_active is False
    assert ctrl._predictive_charge_target_soc is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: setattr(c, "arbitrage_charging_enabled", False),
        lambda c: setattr(c, "predictive_charging_mode", "realtime_price"),
        lambda c: (setattr(c, "discharge_reserve_enabled", False)),
    ],
)
def test_slot_is_not_entered_once_the_feature_or_its_hold_is_gone(mutate):
    ctrl = _controller([_Battery("b1", 50, 10)])
    slot = _live_slot()
    manager = _runtime(ctrl, slot, 2.0, [])
    mutate(ctrl)
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl._current_price_slot_active is False


def test_a_slot_is_not_authorised_when_the_target_cannot_be_computed():
    ctrl = _controller([_Battery("b1", 50, 10)])
    slot = _live_slot()
    manager = _runtime(ctrl, slot, 2.0, [])
    del ctrl._compute_arbitrage_target_soc
    assert manager._arbitrage_slot_authorised(slot) is False


def test_a_slot_with_negligible_energy_is_not_authorised():
    ctrl = _controller([_Battery("b1", 50, 10)])
    slot = _live_slot()
    manager = _runtime(ctrl, slot, 0.01, [])
    assert manager._arbitrage_slot_authorised(slot) is False


def test_a_dismissed_pre_evaluation_blocks_the_slot():
    ctrl = _controller([_Battery("b1", 50, 10)])
    slot = _live_slot()
    manager = _runtime(ctrl, slot, 2.0, [])
    ctrl._dp_pre_evaluated_purposes[slot.start] = None
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert ctrl._current_price_slot_active is False


def test_arbitrage_target_is_split_by_gap_and_capped_at_the_ceiling():
    large = _Battery("large", 20, 10, max_soc=80)  # gap 6 kWh
    small = _Battery("small", 60, 5, max_soc=80)  # gap 1 kWh
    ctrl = _controller([large, small])
    slot = _live_slot()
    _runtime(ctrl, slot, 3.5, [])
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_ARBITRAGE
    ctrl._active_dynamic_price_slot = slot

    targets = ChargeDischargeController._compute_predictive_target_soc(ctrl)

    delivered = sum(
        (targets[b] - b.data["battery_soc"]) / 100 * b.data["battery_total_energy"]
        for b in (large, small)
    )
    assert delivered == pytest.approx(3.5)
    assert targets[large] <= 80 and targets[small] <= 80
    # Share follows the gap: 6/7 and 1/7 of 3.5 kWh.
    assert (targets[large] - 20) / 100 * 10 == pytest.approx(3.0)
    assert (targets[small] - 60) / 100 * 5 == pytest.approx(0.5)


def test_arbitrage_target_never_exceeds_the_room_that_exists():
    battery = _Battery("b1", 90, 10, max_soc=95)
    ctrl = _controller([battery])
    slot = _live_slot()
    _runtime(ctrl, slot, 5.0, [])
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_ARBITRAGE
    ctrl._active_dynamic_price_slot = slot
    targets = ChargeDischargeController._compute_predictive_target_soc(ctrl)
    assert targets[battery] == pytest.approx(95.0)


def test_a_lost_plan_targets_the_current_soc_instead_of_the_ceiling():
    battery = _Battery("b1", 50, 10)
    ctrl = _controller([battery])
    slot = _live_slot()
    _runtime(ctrl, slot, 5.0, [])
    ctrl._dynamic_pricing_schedule.slot_arbitrage_kwh.clear()
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_ARBITRAGE
    ctrl._active_dynamic_price_slot = slot
    targets = ChargeDischargeController._compute_predictive_target_soc(ctrl)
    assert targets[battery] == 50
    ctrl._dynamic_pricing_schedule = None
    assert ChargeDischargeController._compute_predictive_target_soc(ctrl)[battery] == 50


def test_arbitrage_target_skips_a_battery_that_cannot_take_part():
    ok = _Battery("ok", 50, 10)
    off = _Battery("off", 50, 10, available=False)
    ctrl = _controller([ok, off])
    slot = _live_slot()
    _runtime(ctrl, slot, 1.0, [])
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_ARBITRAGE
    ctrl._active_dynamic_price_slot = slot
    targets = ChargeDischargeController._compute_predictive_target_soc(ctrl)
    assert off not in targets
    assert targets[ok] == pytest.approx(60.0)


def test_arbitrage_target_returns_none_without_any_battery():
    ctrl = _controller([])
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_ARBITRAGE
    assert ChargeDischargeController._compute_predictive_target_soc(ctrl) is None


# ---------------------------------------------------------------------------
# Re-planning an hour ahead
# ---------------------------------------------------------------------------


def _future_arb_slot(hours: float = 1.0) -> PriceSlot:
    start = datetime.now() + timedelta(hours=hours)
    return PriceSlot(start, start + timedelta(hours=1), 0.10)


def test_pre_slot_replan_keeps_a_slot_that_is_still_worth_it():
    ctrl = _controller([_Battery("b1", 20, 10)])
    cheap = _future_arb_slot()
    dear = [
        PriceSlot(cheap.end + timedelta(hours=3 + i), cheap.end + timedelta(hours=4 + i), 0.40)
        for i in range(2)
    ]
    ctrl._dynamic_pricing_schedule = _arb_schedule(cheap, 9.9)
    manager = _manager(ctrl, [cheap, *dear])

    asyncio.run(manager._reevaluate_slot_purpose(cheap, datetime.now()))

    assert ctrl._dp_pre_evaluated_purposes[cheap.start] == SLOT_PURPOSE_ARBITRAGE
    assert ctrl._dp_pre_evaluated_slots[cheap.start] is True
    # Re-sized from the live battery, not left at the morning figure.
    assert ctrl._dynamic_pricing_schedule.slot_arbitrage_kwh[cheap] != 9.9
    assert 0 < ctrl._dynamic_pricing_schedule.slot_arbitrage_kwh[cheap] < 9.9


def test_pre_slot_replan_drops_a_slot_the_sun_has_made_pointless():
    battery = _Battery("b1", 100, 10)
    ctrl = _controller([battery])
    cheap = _future_arb_slot()
    dear = PriceSlot(cheap.end + timedelta(hours=3), cheap.end + timedelta(hours=4), 0.40)
    ctrl._dynamic_pricing_schedule = _arb_schedule(cheap, 2.0)
    manager = _manager(ctrl, [cheap, dear])

    asyncio.run(manager._reevaluate_slot_purpose(cheap, datetime.now()))

    assert ctrl._dp_pre_evaluated_purposes[cheap.start] is None
    assert ctrl._dp_pre_evaluated_slots[cheap.start] is False
    assert manager._effective_slot_purpose(cheap) is None


def test_pre_slot_replan_drops_the_slot_when_the_feature_was_turned_off():
    ctrl = _controller([_Battery("b1", 20, 10)], arbitrage=False)
    cheap = _future_arb_slot()
    ctrl._dynamic_pricing_schedule = _arb_schedule(cheap, 2.0)
    manager = _manager(ctrl, [cheap])
    asyncio.run(manager._reevaluate_slot_purpose(cheap, datetime.now()))
    assert ctrl._dp_pre_evaluated_purposes[cheap.start] is None


def test_pre_slot_replan_counts_deficit_slots_still_ahead_as_committed():
    ctrl = _controller([_Battery("b1", 20, 10)])
    ctrl._last_decision_data = _decision(should_charge=True, deficit=20.0)
    cheap = _future_arb_slot()
    deficit_slot = PriceSlot(cheap.end, cheap.end + timedelta(hours=1), 0.09)
    dear = [
        PriceSlot(cheap.end + timedelta(hours=3 + i), cheap.end + timedelta(hours=4 + i), 0.40)
        for i in range(2)
    ]
    schedule = _arb_schedule(cheap, 5.0, deficit_slots=[deficit_slot])
    ctrl._dynamic_pricing_schedule = schedule
    manager = _manager(ctrl, [cheap, deficit_slot, *dear])

    asyncio.run(manager._reevaluate_slot_purpose(cheap, datetime.now()))

    # 20 kWh are already coming for the deficit and fill the battery, so there
    # is nothing left for arbitrage to add.
    assert ctrl._dp_pre_evaluated_purposes[cheap.start] is None


# ---------------------------------------------------------------------------
# Clean-up
# ---------------------------------------------------------------------------


def test_clearing_withdraws_only_arbitrage_slots():
    ctrl = _controller([_Battery("b1", 20, 10)])
    arb = _future_arb_slot()
    deficit_slot = PriceSlot(arb.end, arb.end + timedelta(hours=1), 0.09)
    ctrl._dynamic_pricing_schedule = _arb_schedule(arb, 2.0, deficit_slots=[deficit_slot])
    ctrl._dp_pre_evaluated_purposes[arb.start] = SLOT_PURPOSE_ARBITRAGE
    manager = PricingManager(SimpleNamespace(), ctrl)

    manager.clear_arbitrage_runtime("test")

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.selected_slots == [deficit_slot]
    assert schedule.slot_arbitrage_kwh == {}
    assert schedule.arbitrage_charging_needed is False
    assert schedule.arbitrage_plan is None
    assert schedule.charging_needed is True  # the deficit still needs its slot
    assert arb.start not in ctrl._dp_pre_evaluated_purposes


def test_clearing_an_arbitrage_only_schedule_removes_it():
    ctrl = _controller([_Battery("b1", 20, 10)])
    arb = _future_arb_slot()
    ctrl._dynamic_pricing_schedule = _arb_schedule(arb, 2.0)
    PricingManager(SimpleNamespace(), ctrl).clear_arbitrage_runtime("test")
    assert ctrl._dynamic_pricing_schedule is None


def test_clearing_stops_a_running_arbitrage_charge_but_not_a_deficit_one():
    ctrl = _controller([_Battery("b1", 20, 10)])
    slot = _live_slot()
    ctrl._dynamic_pricing_schedule = _arb_schedule(slot, 2.0)
    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_ARBITRAGE
    ctrl._active_dynamic_price_slot = slot
    ctrl._current_price_slot_active = True
    ctrl.grid_charging_active = True
    ctrl._predictive_charge_target_soc = {"x": 60}
    manager = PricingManager(SimpleNamespace(), ctrl)

    manager.clear_arbitrage_runtime("disabled")

    assert ctrl._current_price_slot_active is False
    assert ctrl.grid_charging_active is False
    assert ctrl._active_dynamic_slot_purpose is None
    assert ctrl._predictive_charge_target_soc is None

    ctrl._active_dynamic_slot_purpose = SLOT_PURPOSE_DEFICIT
    ctrl._current_price_slot_active = True
    ctrl._predictive_charge_target_soc = {"x": 60}
    manager.clear_arbitrage_runtime("again")
    assert ctrl._current_price_slot_active is True
    assert ctrl._predictive_charge_target_soc == {"x": 60}


def test_clearing_without_a_schedule_is_harmless():
    ctrl = _controller([])
    PricingManager(SimpleNamespace(), ctrl).clear_arbitrage_runtime("test")
    assert ctrl._dynamic_pricing_schedule is None


def test_pruning_finished_opportunities_keeps_arbitrage_slots():
    ctrl = _controller([_Battery("b1", 20, 10)])
    arb = _future_arb_slot()
    neg = PriceSlot(arb.end, arb.end + timedelta(hours=1), -0.2)
    schedule = _arb_schedule(arb, 2.0)
    schedule.selected_slots.append(neg)
    schedule.slot_purposes[neg] = SLOT_PURPOSE_NEGATIVE_PRICE
    ctrl._dynamic_pricing_schedule = schedule
    manager = PricingManager(SimpleNamespace(), ctrl)

    manager._prune_completed_opportunities()

    assert schedule.selected_slots == [arb]
    assert schedule.charging_needed is True
    assert schedule.schedule_type == SLOT_PURPOSE_ARBITRAGE


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_status_reports_plan_charges_and_discharges_and_is_json_safe():
    ctrl = _controller([_Battery("b1", 20, 10)])
    manager = _evaluate(ctrl, _future_slots(PRICES))

    info = manager.arbitrage_status()

    assert info["enabled"] is True and info["active"] is True
    assert info["status"] == "planned"
    assert info["discharge_hold_active"] is True
    assert info["expected_gain"] > 0
    assert info["charges"] and info["discharges"]
    assert {"start", "end", "price", "stored_kwh", "grid_kwh"} <= set(info["charges"][0])
    assert info["total_stored_kwh"] == pytest.approx(
        sum(c["stored_kwh"] for c in info["charges"]), abs=0.01
    )
    json.dumps(info)  # must be recordable as a state attribute


def test_status_without_a_plan_or_with_the_feature_off():
    ctrl = _controller([_Battery("b1", 20, 10)], arbitrage=False)
    info = PricingManager(SimpleNamespace(), ctrl).arbitrage_status()
    assert info["enabled"] is False
    assert info["status"] == "no_plan"
    assert info["charges"] == [] and info["discharges"] == []
    json.dumps(info)


def test_status_explains_why_nothing_was_planned():
    ctrl = _controller([_Battery("b1", 20, 10)], hold=False)
    manager = _evaluate(ctrl, _future_slots(PRICES))
    info = manager.arbitrage_status()
    assert info["status"] == "nothing_profitable"
    assert info["reason"] == REASON_NEEDS_DISCHARGE_HOLD
    assert info["discharge_hold_active"] is False


def test_plan_sensor_reports_state_and_attributes():
    ctrl = _controller([_Battery("b1", 20, 10)])
    ctrl._pricing_mgr = _evaluate(ctrl, _future_slots(PRICES))
    sensor = ArbitragePlanSensor(SimpleNamespace(), SimpleNamespace(), ctrl)
    assert sensor.native_value == "planned"
    attrs = sensor.extra_state_attributes
    assert "status" not in attrs
    assert attrs["charges"] and attrs["expected_gain"] > 0
    assert sensor.entity_id.endswith("arbitrage_plan")


def test_plan_sensor_says_disabled_when_the_switch_is_off():
    ctrl = _controller([_Battery("b1", 20, 10)], arbitrage=False)
    ctrl._pricing_mgr = PricingManager(SimpleNamespace(), ctrl)
    sensor = ArbitragePlanSensor(SimpleNamespace(), SimpleNamespace(), ctrl)
    assert sensor.native_value == "disabled"


def test_plan_sensor_survives_a_missing_pricing_manager():
    sensor = ArbitragePlanSensor(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(_pricing_mgr=None)
    )
    assert sensor.native_value == "disabled"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def _entry_and_hass():
    entry = SimpleNamespace(entry_id="entry", data={})

    def update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update_entry))
    return entry, hass


def test_switch_persists_and_withdraws_the_plan_when_turned_off():
    calls: list = []

    class _Pricing:
        def clear_arbitrage_runtime(self, reason):
            calls.append(reason)

    controller = SimpleNamespace(
        arbitrage_charging_enabled=False,
        _pricing_mgr=_Pricing(),
        _schedule_arbitrage_reevaluation=lambda: calls.append("replan"),
    )
    entry, hass = _entry_and_hass()
    entity = ArbitrageChargingSwitch(hass, entry, controller)
    entity.async_write_ha_state = lambda: None

    assert entity.is_on is False
    asyncio.run(entity.async_turn_on())
    assert entry.data[CONF_ARBITRAGE_CHARGING_ENABLED] is True
    assert controller.arbitrage_charging_enabled is True
    assert entity.is_on is True
    assert calls == ["replan"]
    calls.clear()

    asyncio.run(entity.async_turn_off())
    assert entry.data[CONF_ARBITRAGE_CHARGING_ENABLED] is False
    assert controller.arbitrage_charging_enabled is False
    assert calls == ["disabled"]


def test_wear_cost_number_persists_and_defaults_to_zero():
    entry, hass = _entry_and_hass()
    entity = ArbitrageWearCostNumber(hass, entry)
    entity.async_write_ha_state = lambda: None
    assert entity.native_value == DEFAULT_ARBITRAGE_WEAR_COST
    asyncio.run(entity.async_set_native_value(0.035))
    assert entry.data[CONF_ARBITRAGE_WEAR_COST] == 0.035
    assert entity.native_value == 0.035
    assert entity.native_min_value == 0.0


def test_controller_reevaluation_helper_replans_and_withdraws_on_failure():
    created: list = []
    ran: list = []

    class _Pricing:
        async def _evaluate_dynamic_pricing(self, *, horizon, extended_horizon=False):
            ran.append((horizon, extended_horizon))
            if len(ran) == 2:
                raise RuntimeError("boom")

        def clear_arbitrage_runtime(self, reason):
            ran.append(("clear", reason))

    ctrl = SimpleNamespace(
        hass=SimpleNamespace(async_create_task=lambda coro: created.append(coro)),
        _pricing_mgr=_Pricing(),
    )
    ctrl.arbitrage_charging_enabled = True
    ctrl.predictive_charging_enabled = True
    ctrl.predictive_charging_mode = PREDICTIVE_MODE_DYNAMIC_PRICING
    ChargeDischargeController._schedule_arbitrage_reevaluation(ctrl)
    asyncio.run(created.pop())
    assert ran == [(DynamicPricingEvaluationHorizon.REMAINING, True)]

    ChargeDischargeController._schedule_arbitrage_reevaluation(ctrl)
    asyncio.run(created.pop())
    assert ran[-1] == ("clear", "evaluation_error")

    # No hass to schedule on: nothing happens, nothing raises.
    ChargeDischargeController._schedule_arbitrage_reevaluation(SimpleNamespace())

    # Feature off, predictive charging off, or another mode: nothing is planned.
    for attr, value in (
        ("arbitrage_charging_enabled", False),
        ("predictive_charging_enabled", False),
        ("predictive_charging_mode", "realtime_price"),
    ):
        other = SimpleNamespace(**vars(ctrl))
        setattr(other, attr, value)
        ChargeDischargeController._schedule_arbitrage_reevaluation(other)
        assert created == []


# ---------------------------------------------------------------------------
# Notifications and diagnostics
# ---------------------------------------------------------------------------


def _planned_schedule_and_plan():
    ctrl = _controller([_Battery("b1", 20, 10)])
    _evaluate(ctrl, _future_slots(PRICES))
    return ctrl, ctrl._dynamic_pricing_schedule, ctrl._dp_last_arbitrage_plan


def _format(schedule, plan):
    from custom_components.omnibattery.pricing import notifications

    return notifications.format_dynamic_pricing_notification(
        _decision(),
        schedule,
        unit="€/kWh",
        max_price_threshold=None,
        discharge_price_threshold=None,
        arbitrage_plan=plan,
        max_contracted_power=4000,
        max_charge_capacity=4000,
    )


def test_notification_for_an_arbitrage_only_schedule_explains_the_trade():
    _ctrl, schedule, plan = _planned_schedule_and_plan()
    title, message = _format(schedule, plan)
    assert title.startswith("Price arbitrage:")
    assert "Cheap slots bought for dearer hours" in message
    assert "Arbitrage: buy" in message
    assert "Expected gain" in message
    assert "[arbitrage]" in message
    # Each slot shows how much it buys.
    assert " kWh [arbitrage]" in message


def test_notification_says_why_nothing_was_planned():
    ctrl = _controller([_Battery("b1", 20, 10)], hold=False)
    _evaluate(ctrl, _future_slots(PRICES))
    _title, message = _format(ctrl._dynamic_pricing_schedule, ctrl._dp_last_arbitrage_plan)
    assert "Arbitrage: nothing planned" in message
    assert "discharge reserve" in message


def test_notification_is_unchanged_when_the_feature_is_off():
    from custom_components.omnibattery.pricing import notifications

    ctrl = _controller([_Battery("b1", 20, 10)], arbitrage=False)
    _evaluate(ctrl, _future_slots(PRICES))
    with_none = _format(ctrl._dynamic_pricing_schedule, None)
    assert "Arbitrage" not in with_none[1]
    disabled = notifications.format_arbitrage_lines(None, unit="€/kWh")
    assert disabled == ""


def test_slot_start_notification_names_the_purpose_and_energy():
    from custom_components.omnibattery.pricing import notifications

    slot = _live_slot()
    schedule = _arb_schedule(slot, 1.25)
    title, message = notifications.format_slot_start_notification(
        slot, schedule, unit="€/kWh", max_contracted_power=4000
    )
    assert title.startswith("Price arbitrage STARTED")
    assert "1.25 kWh" in message

    plain = DynamicPricingSchedule(
        hours_needed=1,
        selected_slots=[slot],
        average_price=0.1,
        estimated_cost=0,
        total_available_slots=1,
        evaluation_time=datetime.now(),
        energy_deficit_kwh=1,
    )
    title, message = notifications.format_slot_start_notification(
        slot, plain, unit="€/kWh", max_contracted_power=4000
    )
    assert title.startswith("Predictive Charging STARTED")
    assert "Buying" not in message


def test_combined_notification_lists_arbitrage_slots_with_their_energy():
    ctrl = _controller(
        [_Battery("b1", 20, 10)],
        decision=_decision(should_charge=True, deficit=3.0),
    )
    _evaluate(ctrl, _future_slots(PRICES))
    schedule = ctrl._dynamic_pricing_schedule
    _title, message = _format(schedule, ctrl._dp_last_arbitrage_plan)
    assert "[deficit]" in message or "[flexible]" in message or "before" in message
    if schedule.arbitrage_charging_needed:
        assert "[arbitrage]" in message


def test_the_daily_timeline_leaves_arbitrage_slots_out_of_the_deficit_projection():
    from zoneinfo import ZoneInfo

    from custom_components.omnibattery.pricing.daily_timeline import (
        BatteryProjectionInput,
        ProjectionIntervalInput,
    )

    now = datetime(2026, 8, 24, 1, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    deficit_slot = PriceSlot(now, now + timedelta(hours=1), 0.09)
    arbitrage_slot = PriceSlot(now + timedelta(hours=1), now + timedelta(hours=2), 0.10)
    schedule = _arb_schedule(arbitrage_slot, 2.0, deficit_slots=[deficit_slot])
    calls: list = []

    def build_projection(**kwargs):
        calls.append(kwargs)
        plan = SimpleNamespace(
            intervals=[ProjectionIntervalInput(now, now + timedelta(minutes=15))],
            allocations=[],
        )
        return SimpleNamespace(plan=plan, diagnostics={})

    controller = SimpleNamespace(
        _daily_operation_mode=lambda: "dynamic_pricing",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _daily_operation_battery_inputs=lambda: [
            BatteryProjectionInput("a", 0, 10, 50, 100, 4000, 4000)
        ],
        _consumption_tracker=object(),
        _pricing_mgr=SimpleNamespace(build_extended_chronological_projection=build_projection),
        _last_decision_data={},
        _last_chronological_diagnostics={},
        _dynamic_pricing_schedule=schedule,
        predictive_charging_enabled=True,
        charge_delay_enabled=False,
        _daily_operation_delay_active=lambda: False,
        _daily_operation_delay_unlock=lambda _now: None,
        _delay_soc_setpoint_enabled=False,
        _delay_setpoint_reached=False,
        _charge_delay_unlocked=False,
        _charge_delay_status={},
        max_price_threshold=None,
        manual_mode_enabled=False,
        enable_system_power_limits=False,
    )

    ChargeDischargeController._daily_operation_build_projection(controller, now)

    assert [slot.start for slot in calls[0]["slots"]] == [deficit_slot.start]


# ---------------------------------------------------------------------------
# Configuration reload
# ---------------------------------------------------------------------------


def _reload(old_enabled: bool, old_wear: float, data: dict):
    from unittest.mock import MagicMock

    controller = MagicMock()
    controller.config_entry = SimpleNamespace(data=data, entry_id="entry", title="t")
    controller.arbitrage_charging_enabled = old_enabled
    controller.arbitrage_wear_cost = old_wear
    controller.min_arbitrage_margin = None
    controller.round_trip_efficiency = DEFAULT_ROUND_TRIP_EFFICIENCY
    controller.predictive_charging_mode = PREDICTIVE_MODE_DYNAMIC_PRICING
    controller.predictive_charging_enabled = True
    ChargeDischargeController.update_pd_parameters(controller)
    return controller


def _data(**overrides):
    base = {
        "predictive_charging_mode": PREDICTIVE_MODE_DYNAMIC_PRICING,
        "min_arbitrage_margin": None,
        CONF_ARBITRAGE_CHARGING_ENABLED: True,
        CONF_ARBITRAGE_WEAR_COST: 0.0,
    }
    base.update(overrides)
    return base


def test_reload_replans_when_arbitrage_is_enabled_or_its_cost_changes():
    enabled = _reload(False, 0.0, _data())
    assert enabled._schedule_arbitrage_reevaluation.called
    enabled._pricing_mgr.clear_arbitrage_runtime.assert_called_once()

    worn = _reload(True, 0.0, _data(**{CONF_ARBITRAGE_WEAR_COST: 0.04}))
    assert worn._schedule_arbitrage_reevaluation.called
    assert worn.arbitrage_wear_cost == 0.04


def test_reload_leaves_the_plan_alone_when_nothing_arbitrage_related_moved():
    same = _reload(True, 0.02, _data(**{CONF_ARBITRAGE_WEAR_COST: 0.02}))
    assert not same._schedule_arbitrage_reevaluation.called
    same._pricing_mgr.clear_arbitrage_runtime.assert_not_called()


def test_reload_withdraws_the_plan_when_the_mode_is_no_longer_dynamic_pricing():
    left = _reload(
        True, 0.0, _data(predictive_charging_mode="realtime_price")
    )
    left._pricing_mgr.clear_arbitrage_runtime.assert_called()


# ---------------------------------------------------------------------------
# Hourly arbitrage-only refresh
# ---------------------------------------------------------------------------


def _refreshable(ctrl: SimpleNamespace, slots: list[PriceSlot]) -> PricingManager:
    ctrl._dynamic_pricing_evaluated_date = datetime.now().date()
    return _manager(ctrl, slots)


def test_refresh_is_due_only_when_the_feature_runs_and_an_hour_has_passed():
    ctrl = _controller([_Battery("b1", 20, 10)])
    manager = _refreshable(ctrl, _future_slots(PRICES))
    now = datetime.now()

    assert manager._arbitrage_refresh_due(now) is True

    ctrl._dp_arbitrage_refreshed_at = now - timedelta(minutes=59)
    assert manager._arbitrage_refresh_due(now) is False
    ctrl._dp_arbitrage_refreshed_at = now - timedelta(minutes=60)
    assert manager._arbitrage_refresh_due(now) is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: setattr(c, "arbitrage_charging_enabled", False),
        lambda c: setattr(c, "predictive_charging_overridden", True),
        lambda c: setattr(c, "_dynamic_pricing_evaluated_date", None),
        lambda c: setattr(c, "_current_price_slot_active", True),
        lambda c: setattr(c, "predictive_charging_mode", "realtime_price"),
    ],
)
def test_refresh_is_not_due_when_something_else_owns_the_moment(mutate):
    ctrl = _controller([_Battery("b1", 20, 10)])
    manager = _refreshable(ctrl, _future_slots(PRICES))
    mutate(ctrl)
    assert manager._arbitrage_refresh_due(datetime.now()) is False


def test_a_full_evaluation_counts_as_a_refresh():
    ctrl = _controller([_Battery("b1", 20, 10)])
    manager = _evaluate(ctrl, _future_slots(PRICES))
    assert ctrl._dp_arbitrage_refreshed_at is not None
    assert manager._arbitrage_refresh_due(datetime.now()) is False


def test_refresh_creates_a_schedule_when_there_was_none():
    ctrl = _controller([_Battery("b1", 20, 10)])
    slots = _future_slots(PRICES)
    manager = _refreshable(ctrl, slots)
    assert ctrl._dynamic_pricing_schedule is None

    manager._refresh_arbitrage_plan(datetime.now())

    schedule = ctrl._dynamic_pricing_schedule
    assert schedule is not None
    assert _arbitrage_slots(schedule)
    assert schedule.schedule_type == SLOT_PURPOSE_ARBITRAGE
    assert schedule.charging_needed is True
    assert schedule.deficit_charging_needed is False
    assert schedule.arbitrage_charging_needed is True
    assert schedule.arbitrage_energy_kwh == pytest.approx(sum(schedule.slot_arbitrage_kwh.values()))
    assert schedule.arbitrage_plan is ctrl._dp_last_arbitrage_plan
    assert schedule.estimated_cost == pytest.approx(
        sum(c.grid_kwh * c.price for c in schedule.arbitrage_plan.charges)
    )
    assert ctrl._dp_arbitrage_refreshed_at is not None


def test_refresh_leaves_deficit_slots_exactly_as_they_were():
    ctrl = _controller(
        [_Battery("b1", 20, 20)],
        decision=_decision(should_charge=True, deficit=1.0),
    )
    manager = _evaluate(ctrl, _future_slots(PRICES))
    schedule = ctrl._dynamic_pricing_schedule
    deficit_before = {
        slot: schedule.purpose_for(slot)
        for slot in schedule.selected_slots
        if schedule.purpose_for(slot) == SLOT_PURPOSE_DEFICIT
    }
    targets_before = dict(schedule.slot_energy_targets_kwh)
    assert deficit_before

    ctrl._dp_arbitrage_refreshed_at = datetime.now() - timedelta(hours=2)
    manager._refresh_arbitrage_plan(datetime.now())

    after = ctrl._dynamic_pricing_schedule
    assert {
        slot: after.purpose_for(slot)
        for slot in after.selected_slots
        if after.purpose_for(slot) == SLOT_PURPOSE_DEFICIT
    } == deficit_before
    assert after.slot_energy_targets_kwh == targets_before
    assert SLOT_PURPOSE_COMBINED not in after.slot_purposes.values()
    assert after.selected_slots == sorted(after.selected_slots, key=lambda s: s.start)
    assert after.deficit_charging_needed is True


def test_refresh_changes_nothing_when_the_plan_is_the_same():
    ctrl = _controller([_Battery("b1", 20, 10)])
    slots = _future_slots(PRICES)
    manager = _evaluate(ctrl, slots)
    schedule = ctrl._dynamic_pricing_schedule
    slot = _arbitrage_slots(schedule)[0]
    ctrl._dp_pre_evaluated_purposes[slot.start] = SLOT_PURPOSE_ARBITRAGE
    selected = list(schedule.selected_slots)
    energy = dict(schedule.slot_arbitrage_kwh)

    manager._refresh_arbitrage_plan(datetime.now())

    assert ctrl._dynamic_pricing_schedule is schedule
    assert schedule.selected_slots == selected
    assert schedule.slot_arbitrage_kwh == energy
    # A verdict taken an hour ahead is not thrown away without a reason.
    assert ctrl._dp_pre_evaluated_purposes[slot.start] == SLOT_PURPOSE_ARBITRAGE


def test_refresh_withdraws_future_slots_that_stopped_being_worth_it():
    battery = _Battery("b1", 20, 10)
    ctrl = _controller([battery])
    slots = _future_slots(PRICES)
    manager = _evaluate(ctrl, slots)
    stale = _arbitrage_slots(ctrl._dynamic_pricing_schedule)[0]
    ctrl._dp_pre_evaluated_purposes[stale.start] = SLOT_PURPOSE_ARBITRAGE
    battery.data["battery_soc"] = 100  # the sun filled it

    manager._refresh_arbitrage_plan(datetime.now())

    assert ctrl._dynamic_pricing_schedule is None
    assert stale.start not in ctrl._dp_pre_evaluated_purposes
    assert ctrl._dp_last_arbitrage_plan.planned is False


def test_refresh_adds_slots_that_became_worthwhile_since_the_morning():
    battery = _Battery("b1", 100, 10)  # full this morning: nothing planned
    ctrl = _controller([battery])
    slots = _future_slots(PRICES)
    manager = _evaluate(ctrl, slots)
    assert not _arbitrage_slots(ctrl._dynamic_pricing_schedule) if ctrl._dynamic_pricing_schedule else True

    battery.data["battery_soc"] = 20  # the house drained it
    ctrl._dp_arbitrage_refreshed_at = datetime.now() - timedelta(hours=1)
    manager._refresh_arbitrage_plan(datetime.now())

    assert _arbitrage_slots(ctrl._dynamic_pricing_schedule)


def test_refresh_never_touches_a_slot_that_has_started():
    battery = _Battery("b1", 100, 10)
    ctrl = _controller([battery])
    live = _live_slot()
    later = _future_arb_slot(3.0)
    schedule = _arb_schedule(live, 1.0)
    schedule.selected_slots.append(later)
    schedule.slot_purposes[later] = SLOT_PURPOSE_ARBITRAGE
    schedule.slot_arbitrage_kwh[later] = 1.0
    ctrl._dynamic_pricing_schedule = schedule
    manager = _refreshable(ctrl, [live, later, *_future_slots(PRICES, offset_h=6)])

    manager._refresh_arbitrage_plan(datetime.now())

    kept = ctrl._dynamic_pricing_schedule
    assert kept.selected_slots == [live]
    assert kept.slot_arbitrage_kwh == {live: 1.0}


def test_refresh_respects_solar_risk_windows():
    ctrl = _controller([_Battery("b1", 20, 10)])
    slots = _future_slots(PRICES)
    manager = _refreshable(ctrl, slots)
    risky = {slots[0], slots[1], slots[2]}
    manager._slot_overlaps_curtailment_risk = lambda slot: slot in risky

    manager._refresh_arbitrage_plan(datetime.now())

    schedule = ctrl._dynamic_pricing_schedule
    chosen = _arbitrage_slots(schedule) if schedule else []
    assert not set(chosen) & risky


def test_refresh_does_not_offer_another_purposes_slots():
    ctrl = _controller(
        [_Battery("b1", 20, 20)],
        decision=_decision(should_charge=True, deficit=2.0),
    )
    manager = _evaluate(ctrl, _future_slots(PRICES))
    schedule = ctrl._dynamic_pricing_schedule
    owned = {s for s in schedule.selected_slots if schedule.purpose_for(s) != SLOT_PURPOSE_ARBITRAGE}
    manager._refresh_arbitrage_plan(datetime.now())
    after = ctrl._dynamic_pricing_schedule
    assert not owned & set(_arbitrage_slots(after))


def test_refresh_without_prices_does_nothing():
    ctrl = _controller([_Battery("b1", 20, 10)])
    manager = _refreshable(ctrl, [])
    manager._refresh_arbitrage_plan(datetime.now())
    assert ctrl._dynamic_pricing_schedule is None
    assert ctrl._dp_arbitrage_refreshed_at is not None  # do not retry every cycle


def test_control_loop_refreshes_once_an_hour_and_survives_a_failure():
    ctrl = _controller([_Battery("b1", 12, 10)])
    slot = _live_slot()
    manager = _runtime(ctrl, slot, 1.0, [])
    ctrl._dynamic_pricing_schedule = None  # nothing running, nothing planned
    ctrl._dp_arbitrage_refreshed_at = None
    manager.get_future_price_slots = lambda horizon_end=None: _future_slots(PRICES)
    manager.energy_horizon_end = lambda now: now + timedelta(hours=40)
    calls: list = []
    real = manager._refresh_arbitrage_plan

    def counted(now):
        calls.append(now)
        real(now)

    manager._refresh_arbitrage_plan = counted

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    assert len(calls) == 1
    assert ctrl._dynamic_pricing_schedule is not None

    def boom(now):
        raise RuntimeError("planner exploded")

    manager._refresh_arbitrage_plan = boom
    ctrl._dp_arbitrage_refreshed_at = datetime.now() - timedelta(hours=2)
    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())  # must not raise
    assert datetime.now() - ctrl._dp_arbitrage_refreshed_at < timedelta(minutes=1)


def test_midnight_reset_drops_arbitrage_energy_of_slots_it_removes():
    ctrl = _controller([_Battery("b1", 50, 10)])
    now = datetime.now()
    old = PriceSlot(now - timedelta(days=1, hours=2), now - timedelta(days=1, hours=1), 0.1)
    new = PriceSlot(now + timedelta(hours=1), now + timedelta(hours=2), 0.1)
    schedule = _arb_schedule(old, 1.0)
    schedule.selected_slots.append(new)
    schedule.slot_purposes[new] = SLOT_PURPOSE_ARBITRAGE
    schedule.slot_arbitrage_kwh[new] = 1.0
    ctrl._dynamic_pricing_schedule = schedule
    ctrl._dynamic_pricing_evaluated_date = now.date() - timedelta(days=1)
    manager = _runtime(ctrl, new, 1.0, [])
    manager.clear_curtailment_runtime = lambda *args, **kwargs: None
    ctrl._dynamic_pricing_schedule = schedule
    ctrl._dynamic_pricing_evaluated_date = now.date() - timedelta(days=1)
    ctrl._dp_arbitrage_refreshed_at = now

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert old not in schedule.selected_slots
    assert old not in schedule.slot_arbitrage_kwh
    assert new in schedule.slot_arbitrage_kwh
