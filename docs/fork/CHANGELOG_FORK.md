# Fork changelog

Changes carried by this fork on top of upstream (ffunes/Omnibattery). Kept in its own file so syncing upstream never conflicts with `CHANGELOG.md`.

## Unreleased

### Added

- **Price arbitrage charging** (opt-in, Dynamic Pricing). Buys extra energy in cheap periods, beyond the forecast deficit, when the later demand it replaces repays round-trip losses, the new **Arbitrage wear cost** and the **Minimum arbitrage margin**. Details: [arbitrage.md](arbitrage.md).
  - New pure planner `pricing/arbitrage.py`; new slot purpose `arbitrage` with its own planned energy per slot.
  - Entities: switch `arbitrage_charging`, number `arbitrage_wear_cost`, diagnostic sensor `arbitrage_plan` (attributes feed a dashboard card).
  - Re-planned one hour before each arbitrage slot and with every plan rebuild; needs the Discharge Reserve or Price-Based Discharge.
  - Notifications gained an arbitrage section; diagnostics export the new fields.
  - **Sell extra** (opt-in switch `arbitrage_export`): also buy extra in cheap periods to export in dear ones. The planner values exports at the export price curve; the High-Price Sale machinery releases the energy only when it is really there and beyond the demand protected until sunrise; the discharge reserve holds it until then. Power comes from the batteries' charge and discharge limits, nothing is hardcoded.
  - Spare **stored** energy is sold too, bought back at the cheapest later hours of the published curve; without this a plan vanished the moment its purchase had made the battery full.
  - Arbitrage is re-planned every hour on its own (arbitrage slots only), not just on full plan rebuilds.
  - The dashboard card explains why nothing is planned (battery, demand, already-planned grid charge).

### Known gaps / later

- Household use is valued at the **import price**; exports at the export price curve. For 2027 (end of the salderingsregeling), point the export price sensor at a tax-excluded series; nothing else needs to change.
- **Zonneplan export bonus is now daytime-only** (`pricing/engine.py::_apply_zonneplan_export_bonus`): the slot must lie entirely between sunrise and sunset (Home Assistant's sun data); without sun data there is no bonus. Upstream applied it to every hour. It still only applies when an export price sensor with the Zonneplan type is configured, as upstream intends.
- Not in the OmniBattery panel (`marstek-panel.js`) and not drawn in the daily timeline (arbitrage slots are deliberately left out of that projection). Add both later.
- Translations only for English and Dutch; other languages fall back to English.
- `tests/test_consumption_tracker.py::test_exclude_dates_drops_the_day_and_blocks_its_rebuild` fails on upstream `main` too (its fixture date has aged out of a 35-day window).
- Docs are English only.


### Files touched in shared code (where upstream merges may conflict)

`__init__.py`, `pricing/engine.py`, `pricing/__init__.py`, `pricing/notifications.py`, `pricing/high_price_discharge.py`, `tests/test_zonneplan_pricing.py` (one line), `control/high_price_discharge.py`, `control/discharge_reserve.py`, `switch.py`, `number.py`, `sensor.py`, `diagnostics.py`, `const/integration_const.py`, `strings.json`, `translations/en.json`, `translations/nl.json`.
New files: `pricing/arbitrage.py`, `tests/test_arbitrage_planner.py`, `tests/test_arbitrage_charging.py`, `docs/fork/`.
