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

## Buying extra to sell (optional)

Turn on **Sell extra arbitrage energy** (`switch.omnibattery_arbitrage_export`, needs *Price arbitrage charging*) and the planner also buys extra energy in cheap periods to **export** it in the dear ones, not only to use at home.

- **Value of a sale:** `round-trip efficiency × export price − buy price − wear cost`, and it has to reach the minimum margin, exactly like a household trade. The export price comes from the price layer (`get_future_export_price_slots`): tax, netting and any Zonneplan bonus are decided there, never here. Without a separate export price sensor the export price is the import price (net metering).
- **Household first:** on equal value the house wins. A sale only gets the discharge power the house leaves over.
- **Power is never hardcoded.** Buying is limited by the fleet's maximum charge power (and peak shaving), selling by its maximum discharge power. There is no separate grid export limit.
- **Only extra energy is planned for sale.** Energy already in the battery stays the house's.
- **How the sale actually happens:** the plan hands a request to the High-Price Sale machinery. That releases the energy only when it is really in the battery, only beyond the demand protected until sunrise, never below the floor and never above the battery's power. A request made before the purchase simply finds nothing to sell yet and is picked up at the next rebuild (every five minutes).
- **The discharge reserve holds the energy** for the planned sale hours, otherwise the house would use it before the peak.
- **Not tied to the High-Price Sale select.** Arbitrage selling switches the sale machinery on by itself, so that select can stay Off.
- The sale appears in the plan sensor (`discharges` with `kind: export`, `export_kwh`) and in the High-Price Sale status (`arbitrage_kwh`).

**Arbitrage wear cost** is the cost of cycling the battery per grid kWh bought (default 0). It is deducted from every trade on top of the efficiency losses; the minimum margin is a threshold on top of that. Changing either re-plans immediately.

Arbitrage periods carry the `arbitrage` purpose and their own energy per period. At the start of a period, the target SOC is the current SOC plus that period's planned energy (split across batteries by their remaining room), never the charge ceiling. About an hour before each arbitrage period it is re-planned from the live battery and the live remainder of the horizon, and dropped if it is no longer worth it, for example when solar has filled the battery. It is also re-planned with every normal plan rebuild, and **once an hour on its own** (an arbitrage-only refresh that adds slots that became worthwhile and withdraws future ones that stopped being so; deficit slots, a running slot and notifications are left alone). The refresh is skipped while a charge slot is running or the feature is off.

This feature uses the price your sensor reports for importing and nothing else. It does not read a tax or export tariff, so it values later energy at the import price. That is right while feed-in is netted against imports; revisit it when the netting scheme changes.

**Arbitrage Plan** is a diagnostic sensor. Its state is `planned`, `nothing_profitable`, `invalid_input`, `no_plan` or `disabled`. Its attributes list the planned purchases and the later demand they count on, with prices and kWh, plus `expected_gain`, `total_grid_kwh`, `total_stored_kwh`, `unserved_demand_kwh`, `reason` and `discharge_hold_active`.

## Dashboard card

The attributes feed a card on your own dashboard. This one needs no custom cards. It also reads the predictive-charging, discharge-reserve, High-Price Sale and integration-status entities: when nothing is planned it says why in plain language (battery, demand, already-planned grid charge), a Price spread section shows the cheapest and dearest hours until sunrise and the best buy-then-use trade against your margin (it reads your price sensor's quarter-hourly forecast), and a Discharge section shows what the battery is doing now, how much the reserve holds back and any planned sales:

```yaml
type: markdown
title: Battery & prices
content: |-
  {%- set p = 'sensor.omnibattery_arbitrage_plan' %}
  {%- set pc = 'binary_sensor.omnibattery_predictive_charging_active' %}
  {%- set dr = 'binary_sensor.omnibattery_discharge_reserve_status' %}
  {%- set hp = 'sensor.omnibattery_high_price_discharge_status' %}
  {%- set ist = 'sensor.omnibattery_integration_status' %}
  {%- set price = 'sensor.zonneplan_current_quarter_hourly_electricity_tariff' %}
  {%- set rte = states('number.omnibattery_round_trip_efficiency') | float(0.85) %}
  {%- set wear = states('number.omnibattery_arbitrage_wear_cost') | float(0) %}
  {%- set margin = states('number.omnibattery_min_arbitrage_margin') | float(0) %}
  {%- set st = states(p) %}
  {%- set reason = state_attr(p, 'reason') %}
  {%- set committed = state_attr(p, 'committed_charge_kwh') | float(0) %}
  {%- set why = state_attr(pc, 'decision_reason') | string %}
  {%- set cause = 'weekly full charge' if 'Weekly' in why else 'forecast deficit' if 'deficit' in why else 'plan' %}
  {%- set usable = state_attr(dr, 'usable_energy_kwh') | float(0) %}
  {%- set need = state_attr(dr, 'horizon_demand_kwh') | float(0) / (rte ** 0.5) %}
  {%- set left = usable + committed - need %}
  {%- set texts = {
    'no_unserved_demand': 'battery + planned charge already cover the demand' ~ ('' if state_attr(p, 'export_enabled') else ' (turn on selling to also trade the spread)'),
    'no_profitable_spread': 'price gap too small after losses, wear and margin',
    'needs_discharge_hold': 'turn on Discharge Reserve or Price-Based Discharge',
    'no_charge_slots': 'no free cheap slot to buy in',
    'no_room': 'no room without pushing out solar'
  } %}
  {%- set status_texts = {
    'price_discharge_blocked': '⛔ blocked, price too low',
    'price_reserve_hold': '🔒 holding for dearer hours',
    'discharging': '🔋 discharging',
    'charging': '🔌 charging',
    'balanced': '⚖️ idle'
  } %}
  {% if st == 'planned' %}
  ## ✅ Buying {{ state_attr(p, 'total_grid_kwh') }} kWh cheap{{ ' to sell' if state_attr(p, 'export_kwh') | float(0) > 0 else '' }} · +€{{ state_attr(p, 'expected_gain') | float(0) | round(2) }}
  {% elif st == 'disabled' %}
  ## ⏸ Arbitrage off
  {% else %}
  ## 💤 No extra buying
  {{ texts.get(reason, reason) }}
  {% endif %}

  {% if st != 'disabled' %}
  _Selling extra: {{ 'on' if state_attr(p, 'export_enabled') else 'off' }}_

  | Energy until tomorrow's prices | |
  |---|--:|
  | Battery, usable now | {{ usable | round(1) }} kWh |
  | + Planned grid charge{{ ' (' ~ cause ~ ')' if committed > 0 else '' }} | {{ committed | round(1) }} kWh |
  | − House demand, after losses | {{ need | round(1) }} kWh |
  | **= Left over** | **{{ '+' if left >= 0 else '' }}{{ left | round(1) }} kWh** |
  {% endif %}
  {%- set a = state_attr(p, 'charges') or [] %}
  {%- set d = state_attr(p, 'discharges') or [] %}
  {% if a %}

  **Buy:**
  {% for c in a -%}
  - {{ as_timestamp(c.start) | timestamp_custom('%H:%M') }}–{{ as_timestamp(c.end) | timestamp_custom('%H:%M') }} · €{{ c.price | round(2) }} · {{ c.stored_kwh | round(1) }} kWh
  {% endfor %}
  **Use / sell:**
  {% for x in d -%}
  - {{ '💶 sell' if x.kind == 'export' else '🏠 home' }} {{ as_timestamp(x.start) | timestamp_custom('%H:%M') }}–{{ as_timestamp(x.end) | timestamp_custom('%H:%M') }} · €{{ x.price | round(2) }} · {{ (x.delivered_kwh if x.delivered_kwh is defined else x.stored_kwh) | round(1) }} kWh
  {% endfor %}
  {% endif %}
  ---
  {%- set fc = state_attr(price, 'forecast') or [] %}
  {%- set nowts = as_timestamp(now()) %}
  {%- set endts = as_timestamp(state_attr(dr, 'horizon_end')) if state_attr(dr, 'horizon_end') else nowts + 86400 %}
  {%- set ns = namespace(minp=none, lo=none, loat='', hi=none, hiat='', best=none, buyp=0, usep=0) %}
  {%- for e in fc if as_timestamp(e.start_date) >= nowts and as_timestamp(e.start_date) < endts %}
  {%- set pr = (e.price_tax_included.amount if e.price_tax_included is defined else e.electricity_price) / 10000000 %}
  {%- if ns.minp is not none %}
  {%- set net = rte * pr - ns.minp - wear %}
  {%- if ns.best is none or net > ns.best %}{% set ns.best = net %}{% set ns.buyp = ns.minp %}{% set ns.usep = pr %}{% endif %}
  {%- endif %}
  {%- if ns.minp is none or pr < ns.minp %}{% set ns.minp = pr %}{% endif %}
  {%- if ns.lo is none or pr < ns.lo %}{% set ns.lo = pr %}{% set ns.loat = e.start_date %}{% endif %}
  {%- if ns.hi is none or pr > ns.hi %}{% set ns.hi = pr %}{% set ns.hiat = e.start_date %}{% endif %}
  {%- endfor %}
  **Prices until sunrise:**
  {% if ns.lo is none -%}
  no forecast found on `{{ price }}`
  {%- else -%}
  cheapest €{{ ns.lo | round(2) }} ({{ as_timestamp(ns.loat) | timestamp_custom('%a %H:%M') }}) · dearest €{{ ns.hi | round(2) }} ({{ as_timestamp(ns.hiat) | timestamp_custom('%a %H:%M') }})
  {%- if ns.best is not none and ns.usep > ns.buyp %}

  Best trade: net **€{{ ns.best | round(2) }}/kWh** {{ '✅' if ns.best >= margin else '❌' }} (needs ≥ €{{ margin | round(2) }}). To pay off at the cheapest price, the dear hour must be ≥ €{{ ((ns.lo + wear + margin) / rte) | round(2) }}.
  {%- else %}

  No dear hour comes after a cheap one.
  {%- endif -%}
  {%- endif %}

  **Discharge:**
  - Now: {{ status_texts.get(states(ist), states(ist)) }}
  - Reserve: {% if state_attr(dr, 'reserve_kwh') | float(0) > 0 %}holding {{ state_attr(dr, 'reserve_kwh') | float(0) | round(1) }} kWh for hours above €{{ state_attr(dr, 'threshold_price') | float(0) | round(2) }}{% else %}nothing held{% endif %}
  {%- set sales = state_attr(hp, 'allocations') or [] %}
  - Sale: {% if sales %}{{ state_attr(hp, 'total_allocated_kwh') | float(0) | round(1) }} kWh planned {{ as_timestamp((sales | first).start) | timestamp_custom('%H:%M') }}–{{ as_timestamp((sales | last).end) | timestamp_custom('%H:%M') }}{% else %}none planned{% endif %}

  _{{ as_timestamp(state_attr(p, 'evaluation_time')) | timestamp_custom('%H:%M') if state_attr(p, 'evaluation_time') else '-' }}_
```

Adjust the entity ids at the top of the card if yours differ. The built-in daily timeline does not draw arbitrage periods yet.
