Fork-only documentation. Kept outside `site-docs/` so upstream doc changes never conflict with it.

Entities: `switch.omnibattery_arbitrage_charging`, `number.omnibattery_arbitrage_wear_cost`, `sensor.omnibattery_arbitrage_plan`.
These are not in the OmniBattery panel yet (see `CHANGELOG_FORK.md`).

# Price arbitrage charging (fork feature)

This opt-in feature buys energy **beyond** the forecast deficit when a cheap period and a later expensive one are far enough apart to pay for the round trip. Deficit charging buys what the forecast says the house will be short of; negative-price charging fills the battery when importing pays you. Neither buys energy only because the spread is worth it.

The planner works through the remaining price horizon (through the next sunrise, like the discharge reserve). For each cheap period and each later period with unserved household demand it values one grid kWh as:

```text
round_trip_efficiency × later_price − cheap_price − wear_cost
```

A pair is used only when that is positive and at least **Minimum Arbitrage Margin**. The efficiency is split evenly between charging and discharging. The best pairs are taken first, and every kWh is checked against a chronological battery model:

- **Unserved demand only.** The energy already stored, the grid charge already planned for the deficit, and expected solar are assigned to the dearest hours first (the same rule the discharge reserve uses). Only demand left over counts, so nothing is bought twice.
- **Room.** The battery cannot exceed its charge ceiling at any point between purchase and use.
- **Solar is never displaced.** A purchase may not leave less room for solar the battery would otherwise have stored.
- **Power.** Charge and discharge power per period, including peak shaving.
- **Order.** A kWh is bought before the period it is meant for.
- **Max Price Threshold** still limits where energy may be bought, and periods already used by deficit or negative-price charging are not offered, so a period has one reason.

**Price arbitrage charging** needs the **Discharge Reserve** or **Price-Based Discharge**. Without one of them the house would use the cheap energy before the expensive hours, so no plan is made and the reason is shown in the notification and on the plan sensor.

**Arbitrage wear cost** is the cost of cycling the battery per grid kWh bought (default 0). It is deducted from every trade on top of the efficiency losses; the minimum margin is a threshold on top of that. Changing either re-plans immediately.

Arbitrage periods carry the `arbitrage` purpose and their own energy per period. At the start of a period, the target SOC is the current SOC plus that period's planned energy (split across batteries by their remaining room), never the charge ceiling. About an hour before each arbitrage period it is re-planned from the live battery and the live remainder of the horizon, and dropped if it is no longer worth it, for example when solar has filled the battery. It is also re-planned with every normal plan rebuild, and **once an hour on its own** (an arbitrage-only refresh that adds slots that became worthwhile and withdraws future ones that stopped being so; deficit slots, a running slot and notifications are left alone). The refresh is skipped while a charge slot is running or the feature is off.

This feature uses the price your sensor reports for importing and nothing else. It does not read a tax or export tariff, so it values later energy at the import price. That is right while feed-in is netted against imports; revisit it when the netting scheme changes.

**Arbitrage Plan** is a diagnostic sensor. Its state is `planned`, `nothing_profitable`, `invalid_input`, `no_plan` or `disabled`. Its attributes list the planned purchases and the later demand they count on, with prices and kWh, plus `expected_gain`, `total_grid_kwh`, `total_stored_kwh`, `unserved_demand_kwh`, `reason` and `discharge_hold_active`.

## Dashboard card

The attributes feed a card on your own dashboard. This one needs no custom cards. It also reads the predictive-charging and discharge-reserve status sensors, so when nothing is planned it shows why in plain language, with the battery, demand and already-planned grid charge next to it. Adjust the three entity ids at the top if yours differ:

```yaml
type: markdown
title: Arbitrage plan
content: |-
  {% set p = 'sensor.omnibattery_arbitrage_plan' %}
  {% set pc = 'binary_sensor.omnibattery_predictive_charging_active' %}
  {% set dr = 'binary_sensor.omnibattery_discharge_reserve_status' %}
  {% set st = states(p) %}
  {% set reason = state_attr(p, 'reason') %}
  {% set committed = state_attr(p, 'committed_charge_kwh') | float(0) %}
  {% set why = state_attr(pc, 'decision_reason') %}
  {% set usable = state_attr(dr, 'usable_energy_kwh') | float(0) %}
  {% set demand = state_attr(dr, 'horizon_demand_kwh') | float(0) %}
  {% set texts = {
    'no_unserved_demand': 'Nothing extra to buy. The battery plus the grid charge already planned cover everything the house needs until sunrise.',
    'no_profitable_spread': 'Not worth it. The gap between cheap and dear hours is too small after round-trip losses, wear cost and margin.',
    'needs_discharge_hold': 'Turn on the Discharge Reserve or Price-Based Discharge. Without one of them the house would use the cheap energy before the dear hours.',
    'no_charge_slots': 'No cheap period is free to buy in (they are used by other charging, or above your max price).',
    'no_room': 'No room in the battery without pushing out solar.',
    'no_battery_data': 'No battery could be read.',
    'no_price_slots': 'No future prices available.',
    'no_capacity': 'No battery capacity available.',
    'no_power': 'No charge or discharge power available.'
  } %}
  {% if st == 'planned' %}
  ## ✅ Planned: expected gain €{{ state_attr(p, 'expected_gain') | float(0) | round(2) }}
  Buying {{ state_attr(p, 'total_grid_kwh') }} kWh from the grid.
  {% elif st == 'disabled' %}
  ## ⏸ Off
  Switch on *Price arbitrage charging* to use it.
  {% else %}
  ## 💤 Not buying extra
  {{ texts.get(reason, reason) }}
  {% endif %}
  {% if st != 'disabled' %}
  | Right now | |
  |---|---|
  | Usable energy in battery (live) | {{ usable | round(1) }} kWh |
  | House demand in priced hours | {{ demand | round(1) }} kWh |
  | Grid charge already planned | {{ committed | round(1) }} kWh |
  {% endif %}
  {% if st != 'planned' and committed > 0 and why %}
  **Why charge is planned already:** {{ why }}
  {% endif %}
  {% set a = state_attr(p, 'charges') or [] %}
  {% set d = state_attr(p, 'discharges') or [] %}
  {% if a %}
  | Buy | Price | kWh |
  |---|---|---|
  {% for c in a -%}
  | {{ as_timestamp(c.start) | timestamp_custom('%H:%M') }}–{{ as_timestamp(c.end) | timestamp_custom('%H:%M') }} | {{ c.price | round(3) }} | {{ c.stored_kwh }} |
  {% endfor %}
  | Use | Price | kWh |
  |---|---|---|
  {% for x in d -%}
  | {{ as_timestamp(x.start) | timestamp_custom('%H:%M') }}–{{ as_timestamp(x.end) | timestamp_custom('%H:%M') }} | {{ x.price | round(3) }} | {{ x.stored_kwh }} |
  {% endfor %}
  {% endif %}
  _Updated {{ as_timestamp(state_attr(p, 'evaluation_time')) | timestamp_custom('%H:%M') if state_attr(p, 'evaluation_time') else '-' }}_
```

With [ApexCharts card](https://github.com/RomRider/apexcharts-card) the same attributes give a price curve with the buy and use periods marked:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Arbitrage plan
graph_span: 24h
span:
  start: day
series:
  - entity: sensor.omnibattery_arbitrage_plan
    name: Buy (kWh)
    type: column
    data_generator: |
      return (entity.attributes.charges || []).map(c => [new Date(c.start).getTime(), c.stored_kwh]);
  - entity: sensor.omnibattery_arbitrage_plan
    name: Use (kWh)
    type: column
    data_generator: |
      return (entity.attributes.discharges || []).map(d => [new Date(d.start).getTime(), -d.stored_kwh]);
```

Adjust the entity id to the one your installation shows. The built-in daily timeline does not draw arbitrage periods yet.
