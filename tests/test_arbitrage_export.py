"""Buying extra energy to sell it: engine, sale manager, reserve and switch.

The planner and the sale planner have their own pure tests. These check the
glue: where export prices come from, how a planned sale reaches the sale
manager, that the house cannot use the energy before the sale, and that the
switch only ever turns the feature on explicitly.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CONF_ARBITRAGE_EXPORT_ENABLED,
    DEFAULT_ARBITRAGE_EXPORT_ENABLED,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
)
from custom_components.omnibattery.control.discharge_reserve import DischargeReserveManager
from custom_components.omnibattery.control.high_price_discharge import (
    HighPriceDischargeManager,
)
from custom_components.omnibattery.pricing import PriceSlot, notifications
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.pricing.arbitrage import (
    KIND_EXPORT,
    ArbitrageSlot,
    plan_arbitrage,
)
from custom_components.omnibattery.pricing.high_price_discharge import ArbitrageExport
from custom_components.omnibattery.switch import ArbitrageExportSwitch

from tests.test_arbitrage_charging import (
    PRICES,
    _Battery,
    _controller,
    _evaluate,
    _future_slots,
    _manager,
    _noop,
    _refreshable,
)


def _export_controller(batteries=None, *, export=True, **kwargs):
    ctrl = _controller(batteries or [_Battery("b1", 20, 10)], **kwargs)
    ctrl.arbitrage_export_enabled = export
    return ctrl


def _with_export_curve(manager, slots, prices=None):
    """Give the manager an export curve (defaults to the import curve)."""
    curve = [
        PriceSlot(s.start, s.end, (prices or {}).get(s, s.price)) for s in slots
    ]
    manager.get_future_export_price_slots = lambda horizon_end=None: curve
    return manager


# ---------------------------------------------------------------------------
# Defaults and the switch
# ---------------------------------------------------------------------------


def test_selling_is_opt_in():
    assert DEFAULT_ARBITRAGE_EXPORT_ENABLED is False


def test_switch_persists_and_replans_in_both_directions():
    calls: list = []
    controller = SimpleNamespace(
        arbitrage_export_enabled=False,
        _schedule_arbitrage_reevaluation=lambda: calls.append("replan"),
    )
    entry = SimpleNamespace(entry_id="e", data={})

    def update_entry(target, *, data):
        target.data = data

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update_entry))
    entity = ArbitrageExportSwitch(hass, entry, controller)
    entity.async_write_ha_state = lambda: None

    assert entity.is_on is False
    asyncio.run(entity.async_turn_on())
    assert entry.data[CONF_ARBITRAGE_EXPORT_ENABLED] is True
    assert controller.arbitrage_export_enabled is True and entity.is_on is True
    asyncio.run(entity.async_turn_off())
    assert entry.data[CONF_ARBITRAGE_EXPORT_ENABLED] is False
    assert controller.arbitrage_export_enabled is False
    assert calls == ["replan", "replan"]
    assert entity.entity_id.endswith("arbitrage_export")


# ---------------------------------------------------------------------------
# The engine plans a sale
# ---------------------------------------------------------------------------


def test_with_selling_on_extra_energy_is_bought_for_the_dear_hours():
    ctrl = _export_controller()
    slots = _future_slots(PRICES)
    manager = _manager(ctrl, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))

    plan = ctrl._dp_last_arbitrage_plan
    assert plan.planned
    assert plan.exports
    assert plan.export_kwh > 0
    assert all(s.price >= 0.25 for s in plan.exports)
    schedule = ctrl._dynamic_pricing_schedule
    assert schedule.arbitrage_charging_needed
    assert schedule.arbitrage_plan is plan


def test_with_selling_off_no_sale_is_planned_even_with_a_curve():
    ctrl = _export_controller(export=False)
    slots = _future_slots(PRICES)
    manager = _manager(ctrl, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    plan = ctrl._dp_last_arbitrage_plan
    assert not plan.exports


def test_selling_needs_arbitrage_itself_to_be_on():
    ctrl = _export_controller(arbitrage=False)
    manager = _manager(ctrl, _future_slots(PRICES))
    assert manager._arbitrage_export_enabled() is False


def test_export_price_is_what_the_export_curve_says_not_the_import_price():
    ctrl = _export_controller()
    ctrl.min_arbitrage_margin = 0.03  # a real margin, so pennies do not qualify
    slots = _future_slots(PRICES)
    manager = _manager(ctrl, slots)
    # Dear to import, worthless to export in every evening slot.
    curve = {s: 0.05 for s in slots if s.price >= 0.25}
    _with_export_curve(manager, slots, curve)
    prices = manager._arbitrage_export_prices(slots, slots[-1].end)
    assert all(prices[s] == 0.05 for s in curve)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    assert not ctrl._dp_last_arbitrage_plan.exports  # household use still allowed


def test_the_lowest_overlapping_export_price_is_used():
    ctrl = _export_controller()
    slot = PriceSlot(datetime(2026, 10, 14, 18), datetime(2026, 10, 14, 19), 0.40)
    manager = _manager(ctrl, [slot])
    manager.get_future_export_price_slots = lambda horizon_end=None: [
        PriceSlot(datetime(2026, 10, 14, 18), datetime(2026, 10, 14, 18, 30), 0.55),
        PriceSlot(datetime(2026, 10, 14, 18, 30), datetime(2026, 10, 14, 19), 0.35),
    ]
    assert manager._arbitrage_export_prices([slot], slot.end) == {slot: 0.35}


def test_a_slot_without_an_export_price_is_left_out():
    ctrl = _export_controller()
    slot = PriceSlot(datetime(2026, 10, 14, 18), datetime(2026, 10, 14, 19), 0.40)
    manager = _manager(ctrl, [slot])
    manager.get_future_export_price_slots = lambda horizon_end=None: []
    assert manager._arbitrage_export_prices([slot], slot.end) == {}
    manager.get_future_export_price_slots = lambda horizon_end=None: [
        PriceSlot(slot.start, slot.end, float("nan"))
    ]
    assert manager._arbitrage_export_prices([slot], slot.end) == {}


def test_a_failing_export_curve_plans_no_sale_instead_of_raising():
    ctrl = _export_controller()
    slots = _future_slots(PRICES)
    manager = _manager(ctrl, slots)

    def boom(horizon_end=None):
        raise RuntimeError("sensor gone")

    manager.get_future_export_price_slots = boom
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    assert not ctrl._dp_last_arbitrage_plan.exports


def test_charging_slots_never_run_beyond_the_batterys_charge_ceiling():
    ctrl = _export_controller([_Battery("b1", 20, 10, max_soc=60)])
    slots = _future_slots(PRICES)
    manager = _manager(ctrl, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    plan = ctrl._dp_last_arbitrage_plan
    assert plan.total_stored_kwh <= 4.0 + 1e-6  # 20% -> 60% of 10 kWh


def test_power_limits_come_from_the_batteries_not_from_constants():
    slots = _future_slots(PRICES)
    small = _export_controller()
    small.max_charge_capacity = 1000
    small.max_discharge_capacity = 1000
    big = _export_controller([_Battery("b1", 20, 40)])
    for ctrl in (small, big):
        manager = _manager(ctrl, slots)
        _with_export_curve(manager, slots)
        asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    assert small._dp_last_arbitrage_plan.total_grid_kwh < big._dp_last_arbitrage_plan.total_grid_kwh
    # 1 kW for the hours it may buy: well below what 4 kW could do.
    assert small._dp_last_arbitrage_plan.total_grid_kwh <= 1.0 * sum(
        (c.end - c.start).total_seconds() / 3600 for c in small._dp_last_arbitrage_plan.charges
    ) + 1e-6


def test_status_and_notification_report_the_sale_and_are_json_safe():
    ctrl = _export_controller()
    slots = _future_slots(PRICES)
    manager = _manager(ctrl, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    info = manager.arbitrage_status()
    assert info["export_enabled"] is True
    assert info["export_kwh"] > 0
    assert any(d["kind"] == KIND_EXPORT for d in info["discharges"])
    assert all("delivered_kwh" in d for d in info["discharges"])
    json.dumps(info)
    text = notifications.format_arbitrage_lines(ctrl._dp_last_arbitrage_plan, unit="€/kWh")
    assert "Sell" in text and "Expected gain" in text


def test_hourly_refresh_replans_the_sale_too():
    ctrl = _export_controller()
    slots = _future_slots(PRICES)
    manager = _refreshable(ctrl, slots)
    _with_export_curve(manager, slots)
    manager._refresh_arbitrage_plan(datetime.now())
    assert ctrl._dp_last_arbitrage_plan.exports
    assert ctrl._dynamic_pricing_schedule.arbitrage_plan.exports


# ---------------------------------------------------------------------------
# The sale manager receives the planned sale
# ---------------------------------------------------------------------------


NOW = datetime(2026, 10, 14, 15, 0)


def _plan_with_sale():
    slots = [
        ArbitrageSlot(NOW + timedelta(hours=1), NOW + timedelta(hours=2), 0.24, export_price=0.24),
        ArbitrageSlot(NOW + timedelta(hours=5), NOW + timedelta(hours=6), 0.50, export_price=0.50),
    ]
    return plan_arbitrage(
        slots, now=NOW, usable_energy_kwh=0.0, free_space_kwh=10.0,
        charge_power_kw=3.0, discharge_power_kw=3.0, round_trip_efficiency=0.88,
        allow_export=True,
    )


def _sale_manager(**controller_overrides):
    plan = _plan_with_sale()
    controller = SimpleNamespace(
        arbitrage_charging_enabled=True,
        arbitrage_export_enabled=True,
        high_price_discharge_enabled=False,
        high_price_surplus_export_enabled=False,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        high_price_discharge_max_power_w=3500.0,
        min_arbitrage_margin=0.03,
        round_trip_efficiency=0.88,
        discharge_reserve_enabled=True,
        _dynamic_pricing_schedule=SimpleNamespace(arbitrage_plan=plan),
        _dp_last_arbitrage_plan=plan,
    )
    for key, value in controller_overrides.items():
        setattr(controller, key, value)
    manager = HighPriceDischargeManager(SimpleNamespace(), controller)
    manager._now = lambda: NOW
    return manager, controller, plan


def test_arbitrage_selling_alone_switches_the_sale_machinery_on():
    manager, controller, _plan = _sale_manager()
    assert manager.feature_enabled() is True
    controller.arbitrage_export_enabled = False
    assert manager.feature_enabled() is False
    controller.arbitrage_export_enabled = True
    controller.arbitrage_charging_enabled = False
    assert manager.feature_enabled() is False


def test_the_sale_manager_still_needs_dynamic_pricing_and_no_override():
    manager, controller, _plan = _sale_manager(predictive_charging_mode="time_slot")
    assert manager.feature_enabled() is False
    manager, controller, _plan = _sale_manager(predictive_charging_overridden=True)
    assert manager.feature_enabled() is False


def test_requests_carry_the_planned_sales_with_aware_times():
    manager, _controller, plan = _sale_manager()
    requests = manager._arbitrage_exports(NOW)
    assert len(requests) == len(plan.exports) >= 1
    assert all(isinstance(r, ArbitrageExport) for r in requests)
    assert all(r.start.tzinfo is not None and r.end.tzinfo is not None for r in requests)
    assert sum(r.energy_kwh for r in requests) == pytest.approx(
        sum(d.delivered_kwh for d in plan.exports)
    )


def test_requests_skip_sales_that_are_over_and_missing_plans():
    manager, controller, _plan = _sale_manager()
    assert manager._arbitrage_exports(NOW + timedelta(hours=7)) == ()
    controller._dynamic_pricing_schedule = None
    controller._dp_last_arbitrage_plan = None
    assert manager._arbitrage_exports(NOW) == ()


def test_the_config_signature_changes_when_selling_is_toggled():
    manager, controller, _plan = _sale_manager()
    first = manager._config()
    controller.arbitrage_export_enabled = False
    second = manager._config()
    assert first != second


def test_the_planner_receives_the_requests_when_a_plan_is_rebuilt(monkeypatch):
    from custom_components.omnibattery.control import high_price_discharge as module

    manager, controller, _plan = _sale_manager()
    captured = {}

    def fake_plan(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status="planned", reason="x", total_allocated_kwh=0.0, arbitrage_kwh=0.0,
            allocations=(), horizon_end=None, protected_demand_kwh=0.0,
            usable_energy_kwh=0.0, trigger_1_budget_kwh=0.0, refill_price=None,
            trigger_1_reason=None,
        )

    monkeypatch.setattr(module, "plan_high_price_discharge", fake_plan)
    pricing = SimpleNamespace(
        energy_horizon_end=lambda now: NOW + timedelta(hours=16),
        _curtailment_battery_snapshots=lambda: [],
    )
    controller._pricing_mgr = pricing
    manager._build_horizon = lambda *a, **k: []
    manager._build_fill_slots = lambda *a, **k: ()
    manager._maybe_rebuild(manager._config())

    assert captured["arbitrage_exports"]
    assert captured["enabled"] is False and captured["trigger_1_enabled"] is False

    controller.arbitrage_export_enabled = False
    manager._plan_config = None
    manager._maybe_rebuild(manager._config())
    assert captured["arbitrage_exports"] == ()


def test_status_shows_selling_only_when_it_is_in_use():
    manager, controller, _plan = _sale_manager()
    manager._set_status("waiting", "x")
    assert manager.get_status()["arbitrage_export_enabled"] is True
    controller.arbitrage_export_enabled = False
    manager._set_status("waiting", "x")
    assert "arbitrage_export_enabled" not in manager.get_status()


# ---------------------------------------------------------------------------
# The house may not use the energy before the sale
# ---------------------------------------------------------------------------


def _reserve_manager(plan_owner):
    manager = DischargeReserveManager(SimpleNamespace(), plan_owner)
    manager._now = lambda: NOW
    return manager


def test_planned_sales_are_held_in_the_reserve_like_demand():
    manager, controller, plan = _sale_manager()
    reserve = _reserve_manager(controller)
    slots = [
        PriceSlot(NOW + timedelta(hours=h), NOW + timedelta(hours=h + 1), 0.3)
        for h in range(8)
    ]
    held = reserve._arbitrage_export_by_slot(slots)
    assert sum(held.values()) == pytest.approx(sum(d.delivered_kwh for d in plan.exports))
    sale_hours = {d.start for d in plan.exports}
    assert {s.start for s in held} <= sale_hours | {
        d.start + timedelta(hours=0) for d in plan.exports
    }


def test_the_reserve_holds_nothing_for_a_sale_that_is_off():
    manager, controller, _plan = _sale_manager()
    reserve = _reserve_manager(controller)
    slots = [PriceSlot(NOW + timedelta(hours=5), NOW + timedelta(hours=6), 0.5)]
    controller.arbitrage_export_enabled = False
    assert reserve._arbitrage_export_by_slot(slots) == {}
    controller.arbitrage_export_enabled = True
    controller.arbitrage_charging_enabled = False
    assert reserve._arbitrage_export_by_slot(slots) == {}


def test_a_sale_split_over_two_price_slots_is_held_pro_rata():
    manager, controller, plan = _sale_manager()
    reserve = _reserve_manager(controller)
    sale = plan.exports[0]
    mid = sale.start + (sale.end - sale.start) / 2
    slots = [PriceSlot(sale.start, mid, 0.5), PriceSlot(mid, sale.end, 0.5)]
    held = reserve._arbitrage_export_by_slot(slots)
    assert list(held.values()) == pytest.approx([sale.delivered_kwh / 2] * 2)


def test_the_reserve_rebuild_adds_the_sale_to_the_demand_it_protects():
    manager, controller, plan = _sale_manager()
    sale = plan.exports[0]
    price_slots = [
        PriceSlot(NOW + timedelta(hours=h), NOW + timedelta(hours=h + 1), 0.20 if h < 5 else 0.50)
        for h in range(8)
    ]
    controller.coordinators = []
    controller._pricing_mgr = SimpleNamespace(
        get_future_price_slots=lambda horizon_end=None: price_slots,
        energy_horizon_end=lambda now: NOW + timedelta(hours=10),
        _get_current_price=lambda: 0.20,
    )
    controller.discharge_reserve_min_saving = 0.05
    reserve = _reserve_manager(controller)
    reserve._fleet_snapshots = lambda: [
        SimpleNamespace(
            soc_pct=60.0, capacity_kwh=10.0, max_soc_pct=100.0, floor_soc_pct=10.0,
            eligible=True, can_discharge=True,
        )
    ]
    reserve._demand_and_surplus_by_slot = lambda *a: ({}, {})
    asyncio.run(reserve.async_rebuild_plan("test", force=True))
    plan_ = reserve.plan
    assert plan_ is not None
    assert plan_.reserve_kwh > 0  # without the sale there is no demand to hold for
    assert sum(s.net_demand_kwh for s in plan_.slots) == pytest.approx(sale.delivered_kwh)


def _daily():
    from custom_components.omnibattery.pricing.engine import DynamicPricingEvaluationHorizon

    return DynamicPricingEvaluationHorizon.DAILY


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


def test_reload_replans_when_selling_is_toggled_elsewhere():
    from unittest.mock import MagicMock

    from custom_components.omnibattery import ChargeDischargeController
    from custom_components.omnibattery.const import DEFAULT_ROUND_TRIP_EFFICIENCY

    controller = MagicMock()
    controller.config_entry = SimpleNamespace(
        data={
            "predictive_charging_mode": PREDICTIVE_MODE_DYNAMIC_PRICING,
            "min_arbitrage_margin": None,
            "arbitrage_charging_enabled": True,
            CONF_ARBITRAGE_EXPORT_ENABLED: True,
        },
        entry_id="e",
        title="t",
    )
    controller.arbitrage_charging_enabled = True
    controller.arbitrage_export_enabled = False
    controller.arbitrage_wear_cost = 0.0
    controller.min_arbitrage_margin = None
    controller.round_trip_efficiency = DEFAULT_ROUND_TRIP_EFFICIENCY
    controller.predictive_charging_mode = PREDICTIVE_MODE_DYNAMIC_PRICING
    controller.predictive_charging_enabled = True
    ChargeDischargeController.update_pd_parameters(controller)
    assert controller.arbitrage_export_enabled is True
    assert controller._schedule_arbitrage_reevaluation.called


# ---------------------------------------------------------------------------
# Zonneplan bonus: daytime only (price layer)
# ---------------------------------------------------------------------------


def _bonus_manager(monkeypatch, sunrise_h=8, sunset_h=19, *, sun_missing=False):
    from zoneinfo import ZoneInfo

    from homeassistant.helpers import sun
    from homeassistant.util import dt as dt_util

    from custom_components.omnibattery.pricing.engine import PricingManager

    tz = ZoneInfo("Europe/Amsterdam")
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)

    def fake_event(hass, event, day):
        if sun_missing:
            return None
        hour = sunrise_h if event == "sunrise" else sunset_h
        return datetime(day.year, day.month, day.day, hour, tzinfo=tz)

    monkeypatch.setattr(sun, "get_astral_event_date", fake_event)
    controller = SimpleNamespace(zonneplan_export_bonus_enabled=True)
    return PricingManager(SimpleNamespace(), controller)


def _slots_at(*hours):
    day = datetime(2026, 10, 14)
    return [
        PriceSlot(day + timedelta(hours=h), day + timedelta(hours=h, minutes=15), 0.50)
        for h in hours
    ]


def test_the_bonus_applies_in_daylight_only(monkeypatch):
    manager = _bonus_manager(monkeypatch)
    day, evening, night = manager._apply_zonneplan_export_bonus(
        _slots_at(12, 20, 3), "zonneplan"
    )
    assert day.price == pytest.approx(0.50 * 1.10 + 0.02)
    assert evening.price == 0.50
    assert night.price == 0.50


def test_a_slot_touching_sunrise_or_sunset_gets_no_bonus(monkeypatch):
    manager = _bonus_manager(monkeypatch)
    before, first, last, after = manager._apply_zonneplan_export_bonus(
        _slots_at(7.75, 8, 18.75, 19), "zonneplan"
    )
    assert before.price == 0.50          # ends exactly at sunrise: still dark
    assert first.price > 0.50            # fully in daylight
    assert last.price > 0.50             # 18:45-19:00 ends at sunset: daylight
    assert after.price == 0.50           # starts at sunset


def test_without_sun_data_there_is_no_bonus(monkeypatch):
    manager = _bonus_manager(monkeypatch, sun_missing=True)
    assert [s.price for s in manager._apply_zonneplan_export_bonus(_slots_at(12), "zonneplan")] == [0.50]


def test_a_failing_sun_lookup_means_no_bonus_not_an_error(monkeypatch):
    from homeassistant.helpers import sun

    manager = _bonus_manager(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("no location")

    monkeypatch.setattr(sun, "get_astral_event_date", boom)
    assert [s.price for s in manager._apply_zonneplan_export_bonus(_slots_at(12), "zonneplan")] == [0.50]


def test_other_providers_and_a_disabled_option_are_untouched(monkeypatch):
    manager = _bonus_manager(monkeypatch)
    assert [s.price for s in manager._apply_zonneplan_export_bonus(_slots_at(12), "nordpool")] == [0.50]
    manager._controller.zonneplan_export_bonus_enabled = False
    assert [s.price for s in manager._apply_zonneplan_export_bonus(_slots_at(12), "zonneplan")] == [0.50]


def test_the_bonus_follows_the_season(monkeypatch):
    summer = _bonus_manager(monkeypatch, sunrise_h=5, sunset_h=22)
    prices = [s.price for s in summer._apply_zonneplan_export_bonus(_slots_at(6, 21), "zonneplan")]
    assert all(p > 0.50 for p in prices)
    winter = _bonus_manager(monkeypatch, sunrise_h=8, sunset_h=17)
    prices = [s.price for s in winter._apply_zonneplan_export_bonus(_slots_at(6, 21), "zonneplan")]
    assert prices == [0.50, 0.50]


# ---------------------------------------------------------------------------
# Selling what the battery already holds
# ---------------------------------------------------------------------------

DEAR_THEN_CHEAP = [0.50] * 4 + [0.25] * 10


def _full_battery_controller(**kwargs):
    ctrl = _export_controller([_Battery("b1", 99, 10)], **kwargs)
    ctrl.min_arbitrage_margin = 0.03
    return ctrl


def test_a_full_battery_plans_a_sale_of_its_spare_energy():
    ctrl = _full_battery_controller()
    slots = _future_slots(DEAR_THEN_CHEAP)
    manager = _manager(ctrl, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))

    plan = ctrl._dp_last_arbitrage_plan
    assert plan.planned
    assert plan.charges == ()
    assert plan.exports
    assert all(d.stock_kwh > 0 for d in plan.exports)
    # Nothing to buy means no arbitrage slots in the calendar.
    schedule = ctrl._dynamic_pricing_schedule
    assert schedule is None or not schedule.arbitrage_charging_needed


def test_the_sale_manager_receives_the_sale_of_stored_energy():
    ctrl = _full_battery_controller()
    slots = _future_slots(DEAR_THEN_CHEAP)
    manager = _manager(ctrl, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    sale_manager = HighPriceDischargeManager(SimpleNamespace(), ctrl)
    requests = sale_manager._arbitrage_exports(datetime.now())
    assert requests
    assert sum(r.energy_kwh for r in requests) == pytest.approx(
        sum(d.delivered_kwh for d in ctrl._dp_last_arbitrage_plan.exports)
    )


def test_the_hourly_refresh_keeps_selling_after_the_purchase_has_happened():
    # The morning plan bought extra to sell. By the afternoon the battery is
    # full, so a fresh plan has nothing to buy; the sale must survive that.
    battery = _Battery("b1", 20, 10)
    ctrl = _export_controller([battery])
    ctrl.min_arbitrage_margin = 0.03
    slots = _future_slots([0.25] * 4 + [0.50] * 4 + [0.25] * 8)
    manager = _refreshable(ctrl, slots)
    _with_export_curve(manager, slots)
    manager._refresh_arbitrage_plan(datetime.now())
    first = ctrl._dp_last_arbitrage_plan
    assert first.exports

    battery.data["battery_soc"] = 99.0  # the planned purchase has been made
    manager._refresh_arbitrage_plan(datetime.now())
    second = ctrl._dp_last_arbitrage_plan
    assert second.planned
    assert second.charges == ()
    assert second.exports


def test_selling_stored_energy_needs_the_switch_and_a_refill_price():
    off = _full_battery_controller(export=False)
    slots = _future_slots(DEAR_THEN_CHEAP)
    manager = _manager(off, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    assert not off._dp_last_arbitrage_plan.exports

    # Nothing cheaper to buy back later: no sale.
    on = _full_battery_controller()
    flat = _future_slots([0.50] * 14)
    manager = _manager(on, flat)
    _with_export_curve(manager, flat)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    assert not on._dp_last_arbitrage_plan.exports


def test_a_sale_only_plan_is_reported_and_notified_without_a_purchase():
    ctrl = _full_battery_controller()
    slots = _future_slots(DEAR_THEN_CHEAP)
    manager = _manager(ctrl, slots)
    _with_export_curve(manager, slots)
    asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
    info = manager.arbitrage_status()
    assert info["status"] == "planned"
    assert info["charges"] == []
    assert info["export_kwh"] > 0
    json.dumps(info)
    text = notifications.format_arbitrage_lines(ctrl._dp_last_arbitrage_plan, unit="€/kWh")
    assert "stored energy" in text and "bought back" in text


# ---------------------------------------------------------------------------
# The planner follows the sale's rule (floor and margin come from the setup)
# ---------------------------------------------------------------------------


def test_the_sale_reserve_comes_from_the_floors_and_the_safety_margin():
    ctrl = _export_controller([_Battery("b1", 50, 10, min_soc=5), _Battery("b2", 50, 20, min_soc=5)])
    ctrl._predictive_min_soc_floor_enabled = True
    ctrl._predictive_min_soc_floor = 20.0
    ctrl.predischarge_reserve_soc = 0.0
    ctrl._predictive_safety_margin_kwh = 0.7
    manager = PricingManager(SimpleNamespace(), ctrl)
    floor_kwh, margin_kwh = manager._arbitrage_sale_reserve()
    assert floor_kwh == pytest.approx(0.15 * 10 + 0.15 * 20)
    assert margin_kwh == pytest.approx(0.7)


def test_the_largest_of_the_floors_wins():
    ctrl = _export_controller([_Battery("b1", 50, 10, min_soc=5)])
    ctrl._predictive_min_soc_floor_enabled = False
    ctrl.predischarge_reserve_soc = 30.0
    manager = PricingManager(SimpleNamespace(), ctrl)
    assert manager._arbitrage_sale_reserve()[0] == pytest.approx(0.25 * 10)
    ctrl.predischarge_reserve_soc = 0.0
    assert manager._arbitrage_sale_reserve()[0] == 0.0


def test_a_missing_margin_or_an_unreadable_battery_gives_a_zero_reserve():
    ctrl = _export_controller([_Battery("b1", 50, 10, available=False)])
    manager = PricingManager(SimpleNamespace(), ctrl)
    assert manager._arbitrage_sale_reserve() == (0.0, 0.0)


def test_the_engine_plans_less_when_the_sale_floor_and_margin_are_higher():
    slots = _future_slots(DEAR_THEN_CHEAP)
    results = []
    for floor_enabled, margin in ((False, 0.0), (True, 1.0)):
        ctrl = _full_battery_controller()
        ctrl._predictive_min_soc_floor_enabled = floor_enabled
        ctrl._predictive_min_soc_floor = 30.0
        ctrl._predictive_safety_margin_kwh = margin
        manager = _manager(ctrl, slots)
        _with_export_curve(manager, slots)
        asyncio.run(manager._evaluate_dynamic_pricing(horizon=_daily()))
        results.append(ctrl._dp_last_arbitrage_plan.export_kwh)
    assert results[0] > results[1]


# ---------------------------------------------------------------------------
# The sale plan follows a changed arbitrage plan at once
# ---------------------------------------------------------------------------


def _rebuild_counter(monkeypatch, manager, controller):
    from custom_components.omnibattery.control import high_price_discharge as module

    calls = []

    def fake_plan(*args, **kwargs):
        calls.append(kwargs.get("arbitrage_exports"))
        return SimpleNamespace(
            status="planned", reason="x", total_allocated_kwh=0.0, arbitrage_kwh=0.0,
            allocations=(), horizon_end=None, protected_demand_kwh=0.0,
            usable_energy_kwh=0.0, trigger_1_budget_kwh=0.0, refill_price=None,
            trigger_1_reason=None,
        )

    monkeypatch.setattr(module, "plan_high_price_discharge", fake_plan)
    controller._pricing_mgr = SimpleNamespace(
        energy_horizon_end=lambda now: NOW + timedelta(hours=16),
        _curtailment_battery_snapshots=lambda: [],
    )
    manager._build_horizon = lambda *a, **k: []
    manager._build_fill_slots = lambda *a, **k: ()
    return calls


def test_a_new_arbitrage_sale_rebuilds_the_sale_plan_without_waiting(monkeypatch):
    manager, controller, plan = _sale_manager()
    calls = _rebuild_counter(monkeypatch, manager, controller)
    config = manager._config()

    # The first rebuild happens before any arbitrage plan exists...
    saved = (controller._dynamic_pricing_schedule, controller._dp_last_arbitrage_plan)
    controller._dynamic_pricing_schedule = None
    controller._dp_last_arbitrage_plan = None
    manager._maybe_rebuild(config)
    assert calls == [()]

    # ...and the same config a moment later does not rebuild again...
    manager._maybe_rebuild(config)
    assert len(calls) == 1

    # ...until the arbitrage plan arrives, which must be picked up at once.
    controller._dynamic_pricing_schedule, controller._dp_last_arbitrage_plan = saved
    manager._maybe_rebuild(config)
    assert len(calls) == 2
    assert calls[1]

    # An identical plan does not keep rebuilding.
    manager._maybe_rebuild(config)
    assert len(calls) == 2


def test_a_changed_sale_amount_rebuilds_but_a_rounding_change_does_not(monkeypatch):
    manager, controller, plan = _sale_manager()
    calls = _rebuild_counter(monkeypatch, manager, controller)
    config = manager._config()
    manager._maybe_rebuild(config)
    n = len(calls)

    original = plan.exports
    from dataclasses import replace

    nudged = tuple(replace(d, delivered_kwh=d.delivered_kwh + 0.01) for d in original)
    object.__setattr__(plan, "discharges", nudged + tuple(d for d in plan.discharges if d.kind != "export"))
    manager._maybe_rebuild(config)
    assert len(calls) == n  # a hundredth of a kWh is noise

    bigger = tuple(replace(d, delivered_kwh=d.delivered_kwh + 1.0) for d in original)
    object.__setattr__(plan, "discharges", bigger + tuple(d for d in plan.discharges if d.kind != "export"))
    manager._maybe_rebuild(config)
    assert len(calls) == n + 1
