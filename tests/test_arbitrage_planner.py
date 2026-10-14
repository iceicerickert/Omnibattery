"""Unit tests for the pure price-arbitrage planner.

No Home Assistant, no controller: these exercise the arithmetic that decides how
much extra energy is worth buying in a cheap slot to use in a dear one, and every
constraint that stops it (capacity, PV that must not be displaced, power, order).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.pricing.arbitrage import (
    MIN_ALLOCATION_KWH,
    REASON_BAD_EFFICIENCY,
    REASON_BAD_INPUT,
    REASON_DISABLED,
    REASON_NO_CAPACITY,
    REASON_NO_CHARGE_SLOTS,
    REASON_NO_DEMAND,
    REASON_NO_POWER,
    REASON_NO_SLOTS,
    REASON_NO_SPREAD,
    STATUS_DISABLED,
    STATUS_INVALID,
    STATUS_NOTHING_PROFITABLE,
    STATUS_PLANNED,
    KIND_EXPORT,
    KIND_HOUSEHOLD,
    ArbitrageSlot,
    plan_arbitrage,
    profit_per_grid_kwh,
)

DAY = datetime(2026, 11, 2)
NOW = DAY
RTE = 0.85
ETA = math.sqrt(RTE)


def _slot(
    hour, price, *, demand=0.0, surplus=0.0, minutes=60, chargeable=True, ref=None,
    export_price=None,
):
    start = DAY + timedelta(hours=hour)
    return ArbitrageSlot(
        start=start,
        end=start + timedelta(minutes=minutes),
        price=price,
        net_demand_kwh=demand,
        expected_surplus_kwh=surplus,
        chargeable=chargeable,
        ref=ref if ref is not None else hour,
        export_price=export_price,
    )


def _plan(slots, **overrides):
    kwargs = dict(
        now=NOW,
        usable_energy_kwh=0.0,
        free_space_kwh=10.0,
        charge_power_kw=3.0,
        discharge_power_kw=3.0,
        round_trip_efficiency=RTE,
    )
    kwargs.update(overrides)
    return plan_arbitrage(slots, **kwargs)


def _night_and_evening(*, night=0.10, evening=0.40, demand=1.0):
    """Cheap 01:00-04:00, dear 18:00-21:00 with household demand."""
    slots = []
    for hour in range(24):
        if 1 <= hour < 4:
            slots.append(_slot(hour, night))
        elif 18 <= hour < 21:
            slots.append(_slot(hour, evening, demand=demand))
        else:
            slots.append(_slot(hour, 0.25))
    return slots


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_disabled_plans_nothing():
    plan = _plan(_night_and_evening(), enabled=False)
    assert plan.status == STATUS_DISABLED
    assert plan.reason == REASON_DISABLED
    assert not plan.planned


@pytest.mark.parametrize("rte", [0.0, -0.5, 1.2, float("nan"), float("inf")])
def test_invalid_efficiency_is_rejected(rte):
    plan = _plan(_night_and_evening(), round_trip_efficiency=rte)
    assert plan.status == STATUS_INVALID
    assert plan.reason in {REASON_BAD_EFFICIENCY, REASON_BAD_INPUT}
    assert not plan.planned


@pytest.mark.parametrize(
    "field",
    ["usable_energy_kwh", "free_space_kwh", "charge_power_kw", "wear_cost", "min_margin"],
)
def test_non_finite_numbers_are_rejected(field):
    plan = _plan(_night_and_evening(), **{field: float("nan")})
    assert plan.status == STATUS_INVALID
    assert plan.reason == REASON_BAD_INPUT


def test_non_finite_price_ceiling_is_rejected():
    plan = _plan(_night_and_evening(), max_charge_price=float("nan"))
    assert plan.status == STATUS_INVALID


def test_no_slots():
    plan = _plan([])
    assert plan.status == STATUS_NOTHING_PROFITABLE
    assert plan.reason == REASON_NO_SLOTS


def test_no_capacity():
    plan = _plan(_night_and_evening(), usable_energy_kwh=0.0, free_space_kwh=0.0)
    assert plan.reason == REASON_NO_CAPACITY


@pytest.mark.parametrize("field", ["charge_power_kw", "discharge_power_kw"])
def test_zero_power_plans_nothing(field):
    plan = _plan(_night_and_evening(), **{field: 0.0})
    assert plan.reason == REASON_NO_POWER


def test_slots_in_the_past_and_the_current_slot_are_ignored():
    slots = _night_and_evening()
    plan = _plan(slots, now=DAY + timedelta(hours=22))
    assert not plan.planned
    # Mid-slot: the slot already running cannot be planned for either.
    plan = _plan(slots, now=DAY + timedelta(hours=1, minutes=1))
    assert all(charge.start >= DAY + timedelta(hours=2) for charge in plan.charges)


def test_non_finite_price_and_zero_length_slots_are_skipped():
    slots = _night_and_evening()
    slots.append(_slot(5, float("nan"), demand=5.0))
    slots.append(_slot(6, 9.0, demand=5.0, minutes=0))
    plan = _plan(slots)
    assert plan.planned
    assert all(math.isfinite(pair.discharge_price) for pair in plan.pairs)
    assert all(pair.discharge_price < 9.0 for pair in plan.pairs)


# ---------------------------------------------------------------------------
# The core trade
# ---------------------------------------------------------------------------


def test_profit_formula_is_per_grid_kwh_with_round_trip_losses():
    assert profit_per_grid_kwh(0.10, 0.40, 0.85, 0.0) == pytest.approx(0.24)
    assert profit_per_grid_kwh(0.10, 0.40, 0.85, 0.05) == pytest.approx(0.19)


def test_buys_cheap_for_a_dear_evening():
    plan = _plan(_night_and_evening())
    assert plan.status == STATUS_PLANNED
    assert plan.planned
    # Everything is bought before the evening and nothing in the dear slots.
    assert all(charge.start < DAY + timedelta(hours=18) for charge in plan.charges)
    # Three evening hours of 1 kWh delivered each need 3 / eta stored kWh.
    assert plan.total_stored_kwh == pytest.approx(3.0 / ETA, rel=1e-6)
    assert plan.total_grid_kwh == pytest.approx(3.0 / RTE, rel=1e-6)
    # Cheapest slots first: the three 0.10 slots, not the 0.25 filler.
    assert {charge.price for charge in plan.charges} == {0.10}


def test_expected_gain_matches_the_hand_calculation():
    slots = [_slot(1, 0.10), _slot(18, 0.40, demand=1.0)]
    plan = _plan(slots)
    stored = 1.0 / ETA
    grid = stored / ETA
    assert plan.total_stored_kwh == pytest.approx(stored)
    assert plan.expected_gain == pytest.approx(grid * (0.85 * 0.40 - 0.10))
    assert plan.average_charge_price == pytest.approx(0.10)
    assert plan.average_value_price == pytest.approx(0.40)
    assert len(plan.pairs) == 1
    assert plan.pairs[0].gain == pytest.approx(plan.expected_gain)


def test_stored_kwh_for_returns_the_planned_energy_per_ref():
    slots = [_slot(1, 0.10, ref="night"), _slot(18, 0.40, demand=1.0)]
    plan = _plan(slots)
    assert plan.stored_kwh_for("night") == pytest.approx(1.0 / ETA)
    assert plan.stored_kwh_for("other") == 0.0


def test_quarter_hour_slots_give_the_same_answer_as_hourly():
    hourly = [_slot(1, 0.10), _slot(18, 0.40, demand=1.0)]
    quarters = [_slot(1 + q * 0.25, 0.10, minutes=15) for q in range(4)] + [
        _slot(18 + q * 0.25, 0.40, demand=0.25, minutes=15) for q in range(4)
    ]
    assert _plan(quarters).total_stored_kwh == pytest.approx(
        _plan(hourly).total_stored_kwh
    )


def test_negative_charge_price_is_a_real_opportunity():
    slots = [_slot(2, -0.05), _slot(19, 0.20, demand=1.0)]
    plan = _plan(slots)
    assert plan.planned
    assert plan.expected_gain > 0.0


def test_no_use_at_zero_or_negative_prices():
    slots = [_slot(2, -0.20), _slot(19, -0.05, demand=2.0)]
    plan = _plan(slots)
    assert not plan.planned


def test_dearest_hour_gets_the_energy_first_when_room_is_short():
    slots = [
        _slot(1, 0.10),
        _slot(17, 0.30, demand=2.0),
        _slot(19, 0.50, demand=2.0),
    ]
    plan = _plan(slots, free_space_kwh=2.0)
    assert plan.planned
    assert plan.total_stored_kwh == pytest.approx(2.0)
    used = {pair.discharge_price for pair in plan.pairs}
    assert used == {0.50}


# ---------------------------------------------------------------------------
# Profitability gates
# ---------------------------------------------------------------------------


def test_spread_that_does_not_repay_the_round_trip_is_skipped():
    # 0.85 * 0.34 = 0.289 < 0.30: buying loses money despite a higher price later.
    plan = _plan([_slot(1, 0.30), _slot(18, 0.34, demand=2.0)])
    assert plan.status == STATUS_NOTHING_PROFITABLE
    assert plan.reason == REASON_NO_SPREAD


def test_equal_prices_never_trade():
    plan = _plan([_slot(1, 0.20), _slot(18, 0.20, demand=2.0)])
    assert not plan.planned


def test_wear_cost_is_charged_against_every_kwh():
    slots = [_slot(1, 0.10), _slot(18, 0.20, demand=2.0)]  # profit 0.07
    assert _plan(slots, wear_cost=0.0).planned
    assert _plan(slots, wear_cost=0.05).planned
    assert not _plan(slots, wear_cost=0.08).planned


def test_wear_cost_lowers_the_reported_gain():
    slots = [_slot(1, 0.10), _slot(18, 0.40, demand=1.0)]
    free = _plan(slots, wear_cost=0.0)
    worn = _plan(slots, wear_cost=0.05)
    assert worn.expected_gain < free.expected_gain
    assert worn.total_stored_kwh == pytest.approx(free.total_stored_kwh)


def test_minimum_margin_is_a_threshold_not_a_cost():
    slots = [_slot(1, 0.10), _slot(18, 0.20, demand=2.0)]  # profit 0.07
    assert _plan(slots, min_margin=0.05).planned
    assert not _plan(slots, min_margin=0.08).planned
    # The margin does not reduce the gain that is reported.
    assert _plan(slots, min_margin=0.05).expected_gain == pytest.approx(
        _plan(slots, min_margin=0.0).expected_gain
    )


def test_negative_wear_and_margin_are_treated_as_zero():
    slots = [_slot(1, 0.10), _slot(18, 0.40, demand=1.0)]
    assert _plan(slots, wear_cost=-1.0, min_margin=-1.0).total_stored_kwh == pytest.approx(
        _plan(slots).total_stored_kwh
    )


def test_static_price_ceiling_limits_where_energy_is_bought():
    slots = [_slot(1, 0.10), _slot(2, 0.16), _slot(18, 0.50, demand=2.0)]
    plan = _plan(slots, max_charge_price=0.12)
    assert {charge.price for charge in plan.charges} == {0.10}
    assert not _plan(slots, max_charge_price=0.05).planned


def test_slots_the_caller_marked_unchargeable_are_never_used():
    slots = [
        _slot(1, 0.05, chargeable=False),
        _slot(2, 0.10),
        _slot(18, 0.40, demand=1.0),
    ]
    plan = _plan(slots)
    assert [charge.start.hour for charge in plan.charges] == [2]
    only_blocked = [
        _slot(1, 0.05, chargeable=False),
        _slot(18, 0.40, demand=1.0, chargeable=False),
    ]
    assert _plan(only_blocked).reason == REASON_NO_CHARGE_SLOTS


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def test_cheap_slot_after_the_dear_one_is_useless():
    slots = [_slot(8, 0.40, demand=2.0), _slot(20, 0.10)]
    plan = _plan(slots)
    assert not plan.planned


def test_purchase_must_end_before_the_use_starts():
    # Same slot cannot be bought and used in.
    plan = _plan([_slot(5, 0.10, demand=2.0)])
    assert not plan.planned
    # Adjacent slots are fine.
    plan = _plan([_slot(5, 0.10), _slot(6, 0.50, demand=1.0)])
    assert plan.planned


def test_each_use_is_paired_only_with_an_earlier_purchase():
    slots = [
        _slot(1, 0.10),
        _slot(10, 0.40, demand=1.0),
        _slot(12, 0.05),
        _slot(20, 0.40, demand=1.0),
    ]
    plan = _plan(slots)
    for pair in plan.pairs:
        assert pair.charge_start < pair.discharge_start
    assert plan.total_stored_kwh == pytest.approx(2.0 / ETA)


# ---------------------------------------------------------------------------
# Energy accounting
# ---------------------------------------------------------------------------


def test_plan_is_limited_by_the_demand_it_can_displace():
    plan = _plan(_night_and_evening(demand=0.5))
    assert plan.total_stored_kwh == pytest.approx(1.5 / ETA)
    assert plan.unserved_demand_kwh == pytest.approx(1.5 / ETA)


def test_battery_capacity_caps_the_plan():
    plan = _plan(_night_and_evening(demand=5.0), free_space_kwh=2.0)
    assert plan.total_stored_kwh <= 2.0 + 1e-9


def test_energy_already_stored_is_used_first():
    # 2 stored kWh already on board serve the dearest demand; only the rest is
    # worth buying.
    slots = _night_and_evening(demand=1.0)
    plan = _plan(slots, usable_energy_kwh=2.0, free_space_kwh=8.0)
    assert plan.total_stored_kwh == pytest.approx(3.0 / ETA - 2.0, rel=1e-6)


def test_a_full_battery_that_covers_everything_buys_nothing():
    plan = _plan(_night_and_evening(demand=1.0), usable_energy_kwh=10.0, free_space_kwh=0.0)
    assert plan.reason == REASON_NO_DEMAND
    assert not plan.planned


def test_grid_charge_already_committed_for_the_deficit_is_not_bought_twice():
    slots = _night_and_evening(demand=1.0)
    without = _plan(slots)
    covered = _plan(slots, committed_charge_kwh=10.0)
    partial = _plan(slots, committed_charge_kwh=1.0)
    assert not covered.planned
    assert partial.total_stored_kwh == pytest.approx(without.total_stored_kwh - 1.0)
    assert partial.committed_charge_kwh == pytest.approx(1.0)


def test_committed_charge_cannot_exceed_the_battery():
    slots = _night_and_evening(demand=5.0)
    plan = _plan(slots, free_space_kwh=3.0, committed_charge_kwh=50.0)
    assert not plan.planned


def test_noise_below_the_minimum_allocation_is_dropped():
    tiny = MIN_ALLOCATION_KWH / 4
    slots = [_slot(1, 0.10), _slot(18, 0.40, demand=tiny)]
    plan = _plan(slots)
    assert not plan.planned


def test_charge_power_limits_a_single_slot():
    slots = [_slot(1, 0.10), _slot(18, 0.50, demand=4.0)]
    plan = _plan(slots, charge_power_kw=1.0)
    assert plan.total_stored_kwh == pytest.approx(1.0 * ETA)
    assert plan.charges[0].grid_kwh == pytest.approx(1.0)


def test_charge_spills_over_to_the_next_cheapest_slot_when_power_is_short():
    slots = [_slot(1, 0.10), _slot(2, 0.12), _slot(18, 0.50, demand=2.0)]
    plan = _plan(slots, charge_power_kw=1.0)
    assert [charge.start.hour for charge in plan.charges] == [1, 2]
    assert plan.total_stored_kwh == pytest.approx(2.0 * ETA)


def test_discharge_power_limits_what_a_dear_slot_can_absorb():
    slots = [_slot(1, 0.10), _slot(18, 0.50, demand=5.0)]
    plan = _plan(slots, discharge_power_kw=1.0)
    assert plan.total_stored_kwh == pytest.approx(1.0 / ETA)


# ---------------------------------------------------------------------------
# PV must not be displaced
# ---------------------------------------------------------------------------


def test_arbitrage_never_crowds_out_pv_the_battery_would_have_stored():
    # Battery holds 5 kWh above its floor. 4 kWh of PV is expected at noon, so
    # at most 1 kWh can be held from the night to the evening without pushing
    # sun out.
    slots = [
        _slot(1, 0.10),
        _slot(12, 0.25, surplus=4.0),
        _slot(19, 0.50, demand=4.0),
    ]
    plan = _plan(slots, usable_energy_kwh=0.0, free_space_kwh=5.0)
    assert plan.total_stored_kwh <= 1.0 + 1e-9


def test_pv_that_already_overflows_the_battery_leaves_no_room():
    slots = [
        _slot(1, 0.10),
        _slot(12, 0.25, surplus=9.0),
        _slot(19, 0.50, demand=4.0),
    ]
    plan = _plan(slots, usable_energy_kwh=0.0, free_space_kwh=5.0)
    assert not plan.planned


def test_buying_after_the_sun_is_not_blocked_by_it():
    slots = [
        _slot(12, 0.25, surplus=9.0),
        _slot(15, 0.10),
        _slot(19, 0.50, demand=4.0),
    ]
    # Battery is full from the sun; after it serves the evening the plan has
    # nothing to add (demand is covered by stored sun, not by a purchase).
    plan = _plan(slots, usable_energy_kwh=0.0, free_space_kwh=5.0)
    assert plan.total_stored_kwh <= 5.0 + 1e-9


def test_pv_that_arrives_before_the_dear_hour_serves_it_instead_of_the_grid():
    slots = [
        _slot(1, 0.10),
        _slot(12, 0.25, surplus=4.0),
        _slot(19, 0.50, demand=2.0),
    ]
    plan = _plan(slots, usable_energy_kwh=0.0, free_space_kwh=10.0)
    assert not plan.planned
    assert plan.reason == REASON_NO_DEMAND


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_input_gives_the_same_plan():
    slots = _night_and_evening()
    first = _plan(slots)
    second = _plan(list(reversed(slots)))
    assert [c.start for c in first.charges] == [c.start for c in second.charges]
    assert first.total_stored_kwh == pytest.approx(second.total_stored_kwh)
    assert first.expected_gain == pytest.approx(second.expected_gain)


def test_two_day_horizon_pairs_within_it():
    slots = []
    for day in range(2):
        for hour in range(24):
            start = DAY + timedelta(days=day, hours=hour)
            price = 0.10 if 1 <= hour < 4 else 0.40 if 18 <= hour < 21 else 0.25
            demand = 1.0 if 18 <= hour < 21 else 0.0
            slots.append(
                ArbitrageSlot(start, start + timedelta(hours=1), price, demand, 0.0)
            )
    plan = _plan(slots, free_space_kwh=20.0)
    assert plan.planned
    assert plan.total_stored_kwh == pytest.approx(6.0 / ETA, rel=1e-6)
    for pair in plan.pairs:
        assert pair.charge_start < pair.discharge_start


def test_plan_never_charges_more_than_it_discharges():
    for demand in (0.3, 1.0, 4.0):
        for free in (1.0, 4.0, 10.0):
            plan = _plan(_night_and_evening(demand=demand), free_space_kwh=free)
            used = sum(discharge.stored_kwh for discharge in plan.discharges)
            assert plan.total_stored_kwh == pytest.approx(used)
            assert plan.total_stored_kwh <= free + 1e-9


# ---------------------------------------------------------------------------
# Buying extra to sell: export targets
# ---------------------------------------------------------------------------


def _export_day(*, cheap=0.24, dear=0.50, export=None, demand=0.0):
    """Cheap midday, dear evening; the evening can be exported."""
    export = dear if export is None else export
    slots = []
    for hour in range(24):
        if 12 <= hour < 15:
            slots.append(_slot(hour, cheap))
        elif 18 <= hour < 21:
            slots.append(_slot(hour, dear, demand=demand, export_price=export))
        else:
            slots.append(_slot(hour, 0.30))
    return slots


def test_export_is_off_unless_allowed():
    plan = _plan(_export_day())
    assert not plan.planned
    assert plan.reason == REASON_NO_DEMAND


def test_buys_cheap_to_sell_dear_when_export_is_allowed():
    plan = _plan(_export_day(), allow_export=True)
    assert plan.planned
    assert plan.export_kwh > 0
    assert all(d.kind == KIND_EXPORT for d in plan.discharges)
    # The cheapest hours are always used; dearer ones only once those are full,
    # and every purchase comes before the first sale.
    assert {12, 13, 14} <= {c.start.hour for c in plan.charges}
    assert all(c.start.hour < 18 for c in plan.charges)
    assert all(d.start.hour in (18, 19, 20) for d in plan.exports)
    assert all(pair.kind == KIND_EXPORT for pair in plan.pairs)
    assert plan.expected_gain > 0


def test_export_gain_matches_the_hand_calculation():
    slots = [_slot(13, 0.24), _slot(19, 0.50, export_price=0.50)]
    plan = _plan(slots, allow_export=True, free_space_kwh=20.0)
    # One cheap hour at 3 kW is the binding limit: 3 kWh grid -> 3*eta stored.
    assert plan.total_grid_kwh == pytest.approx(3.0)
    assert plan.total_stored_kwh == pytest.approx(3.0 * ETA)
    assert plan.expected_gain == pytest.approx(3.0 * (RTE * 0.50 - 0.24))
    assert plan.exports[0].delivered_kwh == pytest.approx(3.0 * ETA * ETA)
    assert plan.average_value_price == pytest.approx(0.50)


def test_export_uses_the_export_price_not_the_import_price():
    # Import is dear but exporting pays little: the spread is judged on export.
    slots = [_slot(13, 0.24), _slot(19, 0.60, export_price=0.26)]
    assert not _plan(slots, allow_export=True).planned
    slots = [_slot(13, 0.24), _slot(19, 0.26, export_price=0.60)]
    assert _plan(slots, allow_export=True).planned


@pytest.mark.parametrize("export_price", [None, 0.0, -0.1, float("nan")])
def test_missing_or_non_positive_export_prices_plan_no_sale(export_price):
    slots = [_slot(13, 0.24), _slot(19, 0.50, export_price=export_price)]
    assert not _plan(slots, allow_export=True).planned


def test_export_must_clear_wear_cost_and_margin():
    slots = [_slot(13, 0.24), _slot(19, 0.35, export_price=0.35)]  # 0.0575 profit
    assert _plan(slots, allow_export=True).planned
    assert not _plan(slots, allow_export=True, wear_cost=0.06).planned
    assert not _plan(slots, allow_export=True, min_margin=0.06).planned
    assert _plan(slots, allow_export=True, wear_cost=0.02, min_margin=0.03).planned


def test_household_demand_wins_a_tie_with_export():
    slots = [_slot(13, 0.24), _slot(19, 0.50, demand=1.0, export_price=0.50)]
    plan = _plan(slots, allow_export=True, free_space_kwh=20.0)
    kinds = {d.kind for d in plan.discharges}
    assert KIND_HOUSEHOLD in kinds
    household = sum(d.stored_kwh for d in plan.discharges if d.kind == KIND_HOUSEHOLD)
    assert household == pytest.approx(1.0 / ETA)


def test_export_takes_what_the_household_leaves_of_the_discharge_power():
    # 3 kW for one hour = 3 kWh delivered. The house draws 1 kWh of it.
    slots = [_slot(10, 0.10, ref="a"), _slot(11, 0.10), _slot(12, 0.10),
             _slot(19, 0.50, demand=1.0, export_price=0.50)]
    plan = _plan(slots, allow_export=True, free_space_kwh=30.0, charge_power_kw=10.0)
    delivered = sum(d.delivered_kwh for d in plan.discharges)
    assert delivered == pytest.approx(3.0)
    assert plan.export_kwh * ETA == pytest.approx(2.0)


def test_export_is_limited_by_discharge_power():
    slots = [_slot(12, 0.10), _slot(13, 0.10), _slot(14, 0.10), _slot(19, 0.50, export_price=0.50)]
    plan = _plan(slots, allow_export=True, free_space_kwh=30.0, discharge_power_kw=1.0)
    assert plan.exports[0].delivered_kwh == pytest.approx(1.0)


def test_export_is_limited_by_charge_power_and_battery_room():
    slots = [_slot(13, 0.24), _slot(19, 0.50, export_price=0.50)]
    small = _plan(slots, allow_export=True, free_space_kwh=1.0)
    assert small.total_stored_kwh <= 1.0 + 1e-9
    slow = _plan(slots, allow_export=True, free_space_kwh=30.0, charge_power_kw=0.5)
    assert slow.total_grid_kwh == pytest.approx(0.5)


def test_a_full_battery_buys_nothing_to_sell():
    slots = [_slot(13, 0.24), _slot(19, 0.50, export_price=0.50)]
    plan = _plan(slots, allow_export=True, usable_energy_kwh=10.0, free_space_kwh=0.0)
    assert not plan.planned


def test_stored_energy_is_not_planned_as_a_sale():
    # The planner buys extra for a sale; energy already stored stays the
    # household's. A nearly full battery has little room, so little is planned.
    slots = [_slot(13, 0.24), _slot(19, 0.50, export_price=0.50)]
    plan = _plan(slots, allow_export=True, usable_energy_kwh=8.0, free_space_kwh=2.0)
    assert plan.total_stored_kwh <= 2.0 + 1e-9


def test_export_order_purchase_before_sale():
    slots = [_slot(8, 0.50, export_price=0.50), _slot(20, 0.24)]
    assert not _plan(slots, allow_export=True).planned
    same = [_slot(19, 0.24, export_price=0.80)]
    assert not _plan(same, allow_export=True).planned


def test_export_never_pushes_out_forecast_solar():
    slots = [
        _slot(1, 0.10),
        _slot(12, 0.25, surplus=9.0),
        _slot(19, 0.50, export_price=0.50),
    ]
    plan = _plan(slots, allow_export=True, free_space_kwh=5.0)
    assert not plan.planned


def test_cheapest_hours_are_bought_and_dearest_hours_sold():
    slots = [
        _slot(11, 0.30), _slot(12, 0.20), _slot(13, 0.25),
        _slot(17, 0.40, export_price=0.40), _slot(18, 0.55, export_price=0.55),
    ]
    plan = _plan(slots, allow_export=True, free_space_kwh=1.5, discharge_power_kw=3.0)
    assert plan.planned
    assert plan.charges[0].start.hour == 12
    assert {d.start.hour for d in plan.exports} == {18}


def test_export_plan_never_sells_more_than_it_buys():
    for free in (1.0, 4.0, 10.0):
        plan = _plan(_export_day(demand=0.5), allow_export=True, free_space_kwh=free)
        assert plan.total_stored_kwh == pytest.approx(
            sum(d.stored_kwh for d in plan.discharges)
        )
        assert plan.total_stored_kwh <= free + 1e-9


def test_export_off_keeps_household_plans_unchanged():
    slots = _night_and_evening()
    with_flag = _plan(slots, allow_export=True)
    without = _plan(slots)
    # No export prices on those slots: the flag changes nothing.
    assert with_flag.total_stored_kwh == pytest.approx(without.total_stored_kwh)
    assert with_flag.expected_gain == pytest.approx(without.expected_gain)
