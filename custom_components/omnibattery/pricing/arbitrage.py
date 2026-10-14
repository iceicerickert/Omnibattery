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
# A sale of stored energy has no start-up cost of its own, only the export slot.
MIN_STOCK_SALE_KWH = 0.05

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
    # What exporting to the grid pays in this slot, or None when it must not be
    # planned (no export curve, or the caller did not allow export). The price
    # layer owns tax, bonus and netting; this module only compares numbers.
    export_price: float | None = None

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


KIND_HOUSEHOLD = "household"
KIND_EXPORT = "export"


@dataclass(frozen=True)
class ArbitrageDischarge:
    """What a plan counts on later: household demand it replaces, or an export."""

    start: datetime
    end: datetime
    price: float
    stored_kwh: float
    kind: str = KIND_HOUSEHOLD
    # Energy that leaves the battery for the house or the grid, after losses.
    delivered_kwh: float = 0.0
    # Part of an export that comes from energy already in the battery (not
    # bought for this plan), to be bought back later at the refill price.
    stock_kwh: float = 0.0


@dataclass(frozen=True)
class ArbitragePair:
    """One purchase linked to one later use. The audit trail of the plan."""

    charge_start: datetime
    discharge_start: datetime
    charge_price: float
    discharge_price: float
    stored_kwh: float
    gain: float
    kind: str = KIND_HOUSEHOLD


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
        return self.status == STATUS_PLANNED and bool(self.charges or self.discharges)

    @property
    def exports(self) -> tuple[ArbitrageDischarge, ...]:
        """The planned sales: energy bought extra to be exported later."""
        return tuple(d for d in self.discharges if d.kind == KIND_EXPORT)

    @property
    def export_kwh(self) -> float:
        return sum(d.stored_kwh for d in self.exports)

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


def _refill_price(
    slots: Sequence[ArbitrageSlot],
    after: datetime,
    need_stored_kwh: float,
    charge_power_kw: float,
    eta: float,
    ceiling: float | None,
) -> float | None:
    """What buying back ``need_stored_kwh`` after ``after`` would cost per grid kWh.

    The cheapest later slots are filled up to their charge power, so selling a
    lot and re-buying a lot is priced at the average of enough cheap hours, not
    at the single cheapest quarter. None when they cannot supply it (fail safe:
    no sale of stored energy is planned without a believable way to refill).
    """
    if need_stored_kwh <= EPSILON or charge_power_kw <= EPSILON:
        return None
    candidates = sorted(
        (float(slot.price), slot.duration_hours)
        for slot in slots
        if slot.start >= after
        and _finite(slot.price)
        and slot.duration_hours > EPSILON
        and (ceiling is None or float(slot.price) <= ceiling)
    )
    got = 0.0
    cost = 0.0
    for price, hours in candidates:
        take = min(charge_power_kw * hours * eta, need_stored_kwh - got)
        cost += take * price
        got += take
        if got >= need_stored_kwh - EPSILON:
            return cost / got
    return None


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
    allow_export: bool = False,
    refill_slots: Sequence[ArbitrageSlot] = (),
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
    # What the battery can still push out in a slot once the household has had
    # its share of the same discharge power. Only extra bought energy is ever
    # planned for it; energy already stored is never counted as a sale here.
    export_room = [
        max(0.0, discharge_kw * slot.duration_hours / eta - demand[i])
        if allow_export
        and slot.export_price is not None
        and _finite(slot.export_price)
        and float(slot.export_price) > 0.0
        else 0.0
        for i, slot in enumerate(future)
    ]

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

    mids, ends, _lost = _simulate(level0, cap, pv, zeros, baseline_out)
    unserved = [max(0.0, demand[i] - baseline_out[i]) for i in range(count)]
    unserved_total = sum(unserved)
    export_total = sum(export_room)

    # Energy already in the battery beyond what the house needs until the end of
    # the horizon may also be sold, if it can be bought back later at a price
    # that leaves the margin. That is what keeps a plan alive after the extra
    # energy has been bought: the battery is then full, there is nothing left to
    # buy, but there is something to sell.
    stock_price: float | None = None
    if allow_export and refill_slots and export_total > EPSILON:
        sale_indexes = [i for i in range(count) if export_room[i] > EPSILON]
        stock_cap = max((min(ends[i:]) for i in sale_indexes), default=0.0)
        if stock_cap > EPSILON:
            stock_price = _refill_price(
                refill_slots,
                future[sale_indexes[0]].end,
                stock_cap,
                charge_kw,
                eta,
                float(max_charge_price) if max_charge_price is not None else None,
            )

    if sum(charge_room) <= EPSILON and stock_price is None:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_CHARGE_SLOTS,
            evaluation_time=now,
            unserved_demand_kwh=unserved_total,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )
    if unserved_total + export_total <= EPSILON:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_DEMAND,
            evaluation_time=now,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )

    # Candidate pairs, best first. A purchase must end before its use starts, and
    # a use is either household demand (worth the import price it avoids) or an
    # export (worth the export price).
    pairs: list[tuple[float, int, int, str]] = []
    for c in range(count):
        if charge_room[c] <= EPSILON:
            continue
        for d in range(c + 1, count):
            if future[d].start < future[c].end:
                continue
            if unserved[d] > EPSILON and future[d].price > 0.0:
                # Not drawing from the grid at a non-positive price is no saving,
                # and the discharge reserve never holds energy for such an hour.
                profit = profit_per_grid_kwh(future[c].price, future[d].price, rte, wear)
                if profit > EPSILON and profit >= margin:
                    pairs.append((profit, c, d, KIND_HOUSEHOLD))
            if export_room[d] > EPSILON:
                profit = profit_per_grid_kwh(
                    future[c].price, float(future[d].export_price), rte, wear
                )
                if profit > EPSILON and profit >= margin:
                    pairs.append((profit, c, d, KIND_EXPORT))
    if stock_price is not None:
        for d in range(count):
            if export_room[d] > EPSILON:
                profit = profit_per_grid_kwh(
                    stock_price, float(future[d].export_price), rte, wear
                )
                if profit > EPSILON and profit >= margin:
                    pairs.append((profit, -1, d, KIND_EXPORT))
    if not pairs:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_SPREAD,
            evaluation_time=now,
            unserved_demand_kwh=unserved_total,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )
    # On a tie the household wins: it saves an import and needs no export.
    pairs.sort(
        key=lambda entry: (-entry[0], entry[1], entry[2], entry[3] != KIND_HOUSEHOLD)
    )

    # Room per slot for added energy: a purchase held from slot c until it is
    # used in slot d raises the pre-discharge level in every slot of [c, d]. It
    # is bounded by what is left under ``cap`` there, which also means it can
    # never push out PV the baseline managed to store.
    room = [max(0.0, cap - mids[i]) for i in range(count)]
    bought = [0.0] * count
    used_house = [0.0] * count
    used_export = [0.0] * count
    linked: list[tuple[int, int, float, float, str]] = []
    ends_work = list(ends)
    for profit, c, d, kind in pairs:
        if c < 0:
            # Stored energy: bounded by what is left at every later moment, so
            # the house never loses what the baseline gave it.
            x = min(export_room[d] - used_export[d], max(0.0, min(ends_work[d:])))
            if x <= EPSILON:
                continue
            used_export[d] += x
            for j in range(d, count):
                ends_work[j] -= x
            linked.append((c, d, x, profit * x / eta, KIND_EXPORT))
            continue
        target_left = (
            unserved[d] - used_house[d]
            if kind == KIND_HOUSEHOLD
            else export_room[d] - used_export[d]
        )
        x = min(charge_room[c] - bought[c], target_left)
        if x <= EPSILON:
            continue
        x = min(x, min(room[c : d + 1]))
        if x <= EPSILON:
            continue
        bought[c] += x
        if kind == KIND_HOUSEHOLD:
            used_house[d] += x
        else:
            used_export[d] += x
        for j in range(c, d + 1):
            room[j] -= x
        linked.append((c, d, x, profit * x / eta, kind))

    # Drop noise. Removing a purchase only lowers levels, so what is left stays
    # within every constraint above.
    kept = [i for i in range(count) if bought[i] >= MIN_ALLOCATION_KWH]
    stock_entries = [e for e in linked if e[0] < 0 and e[2] >= MIN_STOCK_SALE_KWH]
    if not kept and not stock_entries:
        return ArbitragePlan(
            status=STATUS_NOTHING_PROFITABLE,
            reason=REASON_NO_ROOM if linked else REASON_NO_SPREAD,
            evaluation_time=now,
            unserved_demand_kwh=unserved_total,
            room_kwh=max(0.0, cap - level0),
            committed_charge_kwh=committed,
        )
    kept_set = set(kept)
    final = [entry for entry in linked if entry[0] in kept_set] + stock_entries
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
            # A stock sale has no purchase before it: the "charge" is the later
            # refill, shown at its price and anchored on the sale itself.
            charge_start=future[d].start if c < 0 else future[c].start,
            discharge_start=future[d].start,
            charge_price=stock_price if c < 0 else future[c].price,
            discharge_price=(
                future[d].price if kind == KIND_HOUSEHOLD else float(future[d].export_price)
            ),
            stored_kwh=x,
            gain=gain,
            kind=kind,
        )
        for c, d, x, gain, kind in final
    )
    use_house = [0.0] * count
    use_export = [0.0] * count
    use_stock = [0.0] * count
    for c, d, x, _gain, kind in final:
        if kind == KIND_HOUSEHOLD:
            use_house[d] += x
        else:
            use_export[d] += x
            if c < 0:
                use_stock[d] += x
    discharges = []
    for i in range(count):
        if use_house[i] > EPSILON:
            discharges.append(
                ArbitrageDischarge(
                    start=future[i].start,
                    end=future[i].end,
                    price=future[i].price,
                    stored_kwh=use_house[i],
                    kind=KIND_HOUSEHOLD,
                    delivered_kwh=use_house[i] * eta,
                )
            )
        if use_export[i] > EPSILON:
            discharges.append(
                ArbitrageDischarge(
                    start=future[i].start,
                    end=future[i].end,
                    price=float(future[i].export_price),
                    stored_kwh=use_export[i],
                    kind=KIND_EXPORT,
                    delivered_kwh=use_export[i] * eta,
                    stock_kwh=use_stock[i],
                )
            )
    total_stored = sum(charge.stored_kwh for charge in charges)
    value_weight = sum(entry[2] for entry in final)
    return ArbitragePlan(
        status=STATUS_PLANNED,
        reason=REASON_PLANNED,
        evaluation_time=now,
        charges=charges,
        discharges=tuple(discharges),
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
            sum(record.discharge_price * record.stored_kwh for record in pair_records)
            / value_weight
            if value_weight > EPSILON
            else None
        ),
        unserved_demand_kwh=unserved_total,
        room_kwh=max(0.0, cap - level0),
        committed_charge_kwh=committed,
        refs=tuple(charge.ref for charge in charges),
    )
