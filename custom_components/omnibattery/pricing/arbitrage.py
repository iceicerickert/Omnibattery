"""Price arbitrage planner: buy extra energy in cheap slots, use it in dear ones.

Deficit charging buys exactly what the forecast says the house will be short of.
Negative-price charging fills the battery when importing pays you. Neither buys
energy *because the spread is worth it*: a night at 0.12 €/kWh followed by an
evening at 0.38 €/kWh is money on the table once the battery has covered
everything else it was going to cover.

This module decides how much extra to buy, in which slots, against which later
demand. It is a pure planner (no Home Assistant, no controller) in the style of
``chronological.py`` and ``discharge_reserve.py``; the runtime adapter lives in
``pricing/engine.py``.

Model
-----
Energy is tracked as *stored* kWh above the discharge floor. A round trip
efficiency ``RTE`` is split evenly between charging and discharging, so one grid
kWh stores ``sqrt(RTE)`` and one stored kWh delivers ``sqrt(RTE)``. Buying a
stored kWh in a slot at ``p_c`` and using it in a slot at ``p_d`` therefore
earns, per grid kWh::

    RTE * p_d - p_c - wear_cost

and a pair is only used when that is positive and at least ``min_margin``.

The value of a later slot is the grid import it avoids, so only *unserved*
household demand counts: demand left after PV, after the energy already in the
battery has been assigned to the dearest hours first (the same rule the
discharge reserve applies at runtime), and after any grid charge already
committed to cover the forecast deficit. Planned energy that merely duplicates
those would be counted twice.

Hard constraints, all checked against a chronological level profile:

* the battery cannot hold more than ``usable + free_space`` above its floor;
* an arbitrage kWh may never crowd out forecast PV that would otherwise be
  stored (the plan must not increase PV that is lost to a full battery);
* charge power per slot, discharge power per slot;
* a kWh must be bought before the slot it is meant for.

Prices are whatever the caller's price layer reports for importing; this module
never reads a tax or export tariff. Passing the right number is the caller's job.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

EPSILON = 1e-9
# Allocations below this are noise: a 0.03 kWh slot would still start a charge
# session, a notification and a handover.
MIN_ALLOCATION_KWH = 0.1

STATUS_DISABLED = "disabled"
STATUS_PLANNED = "planned"
STATUS_NOTHING_PROFITABLE = "nothing_profitable"
STATUS_INVALID = "invalid_input"

REASON_DISABLED = "disabled"
REASON_NO_SLOTS = "no_price_slots"
REASON_NO_CAPACITY = "no_capacity"
REASON_NO_POWER = "no_power"
REASON_BAD_EFFICIENCY = "invalid_efficiency"
REASON_BAD_INPUT = "invalid_input"
REASON_NO_CHARGE_SLOTS = "no_charge_slots"
REASON_NO_DEMAND = "no_unserved_demand"
REASON_NO_SPREAD = "no_profitable_spread"
REASON_NO_ROOM = "no_room"
REASON_PLANNED = "planned"
# Raised by the runtime adapter, not the planner: it decides what holds the energy.
REASON_NEEDS_DISCHARGE_HOLD = "needs_discharge_hold"
REASON_NO_FLEET = "no_battery_data"


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value: Any, default: float = 0.0) -> float:
    if not _finite(value):
        return default
    return max(0.0, float(value))


def _hours(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600.0)


@dataclass(frozen=True)
class ArbitrageSlot:
    """One future price slot with what the household does in it.

    ``net_demand_kwh`` is grid demand left after PV (what the battery could
    serve). ``expected_surplus_kwh`` is PV that will land in the battery, already
    in battery-side kWh. ``chargeable`` is False for slots the caller does not
    allow buying in: already taken by another purpose, inside a protected solar
    window, or priced above the user's static ceiling.
    """

    start: datetime
    end: datetime
    price: float
    net_demand_kwh: float = 0.0
    expected_surplus_kwh: float = 0.0
    chargeable: bool = True
    ref: Any = None

    @property
    def duration_hours(self) -> float:
        return _hours(self.start, self.end)


@dataclass(frozen=True)
class ArbitrageCharge:
    """Energy to buy in one slot."""

    start: datetime
    end: datetime
    price: float
    stored_kwh: float
    grid_kwh: float
    ref: Any = None


@dataclass(frozen=True)
class ArbitrageDischarge:
    """The later demand a plan is counting on, for explanation and the card."""

    start: datetime
    end: datetime
    price: float
    stored_kwh: float


@dataclass(frozen=True)
class ArbitragePair:
    """One purchase linked to one later use. The audit trail of the plan."""

    charge_start: datetime
    discharge_start: datetime
    charge_price: float
    discharge_price: float
    stored_kwh: float
    gain: float


@dataclass(frozen=True)
class ArbitragePlan:
    status: str = STATUS_DISABLED
    reason: str = REASON_DISABLED
    evaluation_time: datetime | None = None
    charges: tuple[ArbitrageCharge, ...] = ()
    discharges: tuple[ArbitrageDischarge, ...] = ()
    pairs: tuple[ArbitragePair, ...] = ()
    total_stored_kwh: float = 0.0
    total_grid_kwh: float = 0.0
    expected_gain: float = 0.0
    average_charge_price: float | None = None
    average_value_price: float | None = None
    # Inputs worth echoing back, so an attribute can explain "why this much".
    unserved_demand_kwh: float = 0.0
    room_kwh: float = 0.0
    committed_charge_kwh: float = 0.0
    refs: tuple[Any, ...] = field(default=(), repr=False)

    @property
    def planned(self) -> bool:
        return self.status == STATUS_PLANNED and bool(self.charges)

    def stored_kwh_for(self, ref: Any) -> float:
        for charge in self.charges:
            if charge.ref == ref:
                return charge.stored_kwh
        return 0.0


def profit_per_grid_kwh(
    charge_price: float,
    use_price: float,
    round_trip_efficiency: float,
    wear_cost: float = 0.0,
) -> float:
    """Net gain of buying one grid kWh at ``charge_price`` and using it later."""
    return round_trip_efficiency * use_price - charge_price - wear_cost


def _simulate(
    level0: float,
    cap: float,
    pv: Sequence[float],
    grid: Sequence[float],
    out: Sequence[float],
) -> tuple[list[float], list[float], float]:
    """Walk the slots once; return (mid_levels, end_levels, pv_lost).

    ``mid`` is the level after charge and PV but before the slot's discharge,
    uncapped, so ``cap - mid`` is the room a pair still has at that moment.
    ``end`` is capped at ``cap`` and floored at zero. PV beyond ``cap`` is lost.
    """
    level = level0
    mids: list[float] = []
    ends: list[float] = []
    lost = 0.0
    for index in range(len(pv)):
        mid = level + grid[index] + pv[index]
        mids.append(mid)
        if mid > cap:
            lost += mid - cap
            mid = cap
        level = max(0.0, mid - out[index])
        ends.append(level)
    return mids, ends, lost


def plan_arbitrage(
    slots: Sequence[ArbitrageSlot],
    *,
    now: datetime,
    enabled: bool = True,
    usable_energy_kwh: float = 0.0,
    free_space_kwh: float = 0.0,
    committed_charge_kwh: float = 0.0,
    charge_power_kw: float = 0.0,
    discharge_power_kw: float = 0.0,
    round_trip_efficiency: float = 0.85,
    wear_cost: float = 0.0,
    min_margin: float = 0.0,
    max_charge_price: float | None = None,
) -> ArbitragePlan:
    """Plan extra grid purchases that pay for themselves later.

    Never raises on bad input: every doubt returns an empty plan with a reason,
    because the caller treats "no plan" as "charge nothing extra".
    """
    if not enabled:
        return ArbitragePlan(
            status=STATUS_DISABLED, reason=REASON_DISABLED, evaluation_time=now
        )

    numbers = (
        usable_energy_kwh,
        free_space_kwh,
        committed_charge_kwh,
        charge_power_kw,
        discharge_power_kw,
        wear_cost,
        min_margin,
    )
    if not all(_finite(value) for value in numbers) or not _finite(
        round_trip_efficiency
    ):
        return ArbitragePlan(
            status=STATUS_INVALID, reason=REASON_BAD_INPUT, evaluation_time=now
        )
    if not 0.0 < float(round_trip_efficiency) <= 1.0:
        return ArbitragePlan(
            status=STATUS_INVALID, reason=REASON_BAD_EFFICIENCY, evaluation_time=now
        )
    if max_charge_price is not None and not _finite(max_charge_price):
        return ArbitragePlan(
            status=STATUS_INVALID, reason=REASON_BAD_INPUT, evaluation_time=now
        )

    rte = float(round_trip_efficiency)
    eta = math.sqrt(rte)
    wear = max(0.0, float(wear_cost))
    margin = max(0.0, float(min_margin))
    usable = _positive(usable_energy_kwh)
    free = _positive(free_space_kwh)
    committed = _positive(committed_charge_kwh)
    cap = usable + free
    if cap <= EPSILON:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_CAPACITY,
            evaluation_time=now,
        )
    if _positive(charge_power_kw) <= EPSILON or _positive(discharge_power_kw) <= EPSILON:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_POWER,
            evaluation_time=now,
        )

    future = sorted(
        (
            slot
            for slot in slots
            if _finite(slot.price)
            and slot.duration_hours > EPSILON
            and slot.start >= now
        ),
        key=lambda slot: slot.start,
    )
    if not future:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_SLOTS,
            evaluation_time=now,
        )

    count = len(future)
    charge_kw = _positive(charge_power_kw)
    discharge_kw = _positive(discharge_power_kw)

    # Per slot, battery-side. What a slot can take in, and what the household
    # would draw from the battery there at most.
    charge_room = [
        charge_kw * slot.duration_hours * eta if slot.chargeable else 0.0
        for slot in future
    ]
    if max_charge_price is not None:
        ceiling = float(max_charge_price)
        charge_room = [
            room if future[i].price <= ceiling else 0.0
            for i, room in enumerate(charge_room)
        ]
    demand = [
        min(_positive(slot.net_demand_kwh), discharge_kw * slot.duration_hours) / eta
        for slot in future
    ]
    pv = [_positive(slot.expected_surplus_kwh) for slot in future]

    level0 = min(cap, usable + committed)
    zeros = [0.0] * count

    # Baseline: what happens with no arbitrage. The energy already stored (and
    # the PV that will arrive) serves the dearest demand first.
    baseline_out = [0.0] * count
    for index in sorted(range(count), key=lambda i: (-future[i].price, future[i].start)):
        if future[index].price <= 0.0 or demand[index] <= EPSILON:
            continue
        # Removing ``x`` here lowers every later end level by ``x``, so ``x`` is
        # bounded by the lowest of them and by what is on hand mid-slot.
        mids, ends, _lost = _simulate(level0, cap, pv, zeros, baseline_out)
        reachable = min(min(mids[index], cap), min(ends[index:]))
        take = min(demand[index], max(0.0, reachable))
        if take > EPSILON:
            baseline_out[index] += take

    mids, _ends, _lost = _simulate(level0, cap, pv, zeros, baseline_out)
    unserved = [max(0.0, demand[i] - baseline_out[i]) for i in range(count)]
    unserved_total = sum(unserved)

    if sum(charge_room) <= EPSILON:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_CHARGE_SLOTS,
            evaluation_time=now,
            unserved_demand_kwh=unserved_total,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )
    if unserved_total <= EPSILON:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_DEMAND,
            evaluation_time=now,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )

    # Candidate pairs, best first. A purchase must end before its use starts.
    pairs: list[tuple[float, int, int]] = []
    for c in range(count):
        if charge_room[c] <= EPSILON:
            continue
        for d in range(c + 1, count):
            if unserved[d] <= EPSILON or future[d].start < future[c].end:
                continue
            if future[d].price <= 0.0:
                # Not drawing from the grid at a non-positive price is no saving,
                # and the discharge reserve never holds energy for such an hour.
                continue
            profit = profit_per_grid_kwh(
                future[c].price, future[d].price, rte, wear
            )
            if profit <= EPSILON or profit < margin:
                continue
            pairs.append((profit, c, d))
    if not pairs:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_SPREAD,
            evaluation_time=now,
            unserved_demand_kwh=unserved_total,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )
    pairs.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))

    # Room per slot for added energy: a purchase held from slot c until it is
    # used in slot d raises the pre-discharge level in every slot of [c, d]. It
    # is bounded by what is left under ``cap`` there, which also means it can
    # never push out PV the baseline managed to store.
    room = [max(0.0, cap - mids[i]) for i in range(count)]
    bought = [0.0] * count
    used = [0.0] * count
    linked: list[tuple[int, int, float, float]] = []
    for profit, c, d in pairs:
        x = min(charge_room[c] - bought[c], unserved[d] - used[d])
        if x <= EPSILON:
            continue
        x = min(x, min(room[c : d + 1]))
        if x <= EPSILON:
            continue
        bought[c] += x
        used[d] += x
        for j in range(c, d + 1):
            room[j] -= x
        linked.append((c, d, x, profit * x / eta))

    # Drop noise. Removing a purchase only lowers levels, so what is left stays
    # within every constraint above.
    kept = [i for i in range(count) if bought[i] >= MIN_ALLOCATION_KWH]
    if not kept:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_ROOM if linked else REASON_NO_SPREAD,
            evaluation_time=now,
            unserved_demand_kwh=unserved_total,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )
    kept_set = set(kept)
    final = [entry for entry in linked if entry[0] in kept_set]
    charges = tuple(
        ArbitrageCharge(
            start=future[i].start,
            end=future[i].end,
            price=future[i].price,
            stored_kwh=bought[i],
            grid_kwh=bought[i] / eta,
            ref=future[i].ref,
        )
        for i in kept
    )
    pair_records = tuple(
        ArbitragePair(
            charge_start=future[c].start,
            discharge_start=future[d].start,
            charge_price=future[c].price,
            discharge_price=future[d].price,
            stored_kwh=x,
            gain=gain,
        )
        for c, d, x, gain in final
    )
    use = [0.0] * count
    for _c, d, x, _gain in final:
        use[d] += x
    discharges = tuple(
        ArbitrageDischarge(
            start=future[i].start,
            end=future[i].end,
            price=future[i].price,
            stored_kwh=use[i],
        )
        for i in range(count)
        if use[i] > EPSILON
    )
    total_stored = sum(charge.stored_kwh for charge in charges)
    value_weight = sum(entry[2] for entry in final)
    return ArbitragePlan(
        status=STATUS_PLANNED,
        reason=REASON_PLANNED,
        evaluation_time=now,
        charges=charges,
        discharges=discharges,
        pairs=pair_records,
        total_stored_kwh=total_stored,
        total_grid_kwh=sum(charge.grid_kwh for charge in charges),
        expected_gain=sum(entry[3] for entry in final),
        average_charge_price=(
            sum(charge.price * charge.stored_kwh for charge in charges) / total_stored
            if total_stored > EPSILON
            else None
        ),
        average_value_price=(
            sum(future[d].price * x for _c, d, x, _g in final) / value_weight
            if value_weight > EPSILON
            else None
        ),
        unserved_demand_kwh=unserved_total,
        room_kwh=max(0.0, cap - level0),
        committed_charge_kwh=committed,
        refs=tuple(charge.ref for charge in charges),
    )
