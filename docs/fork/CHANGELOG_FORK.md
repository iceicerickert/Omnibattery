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
  - Arbitrage is re-planned every hour on its own (arbitrage slots only), not just on full plan rebuilds.
  - The dashboard card explains why nothing is planned (battery, demand, already-planned grid charge).

### Known gaps / later

- Household use is valued at the **import price**; exports at the export price curve. For 2027 (end of the salderingsregeling), point the export price sensor at a tax-excluded series; nothing else needs to change.
- **Zonneplan bonus (upstream price layer):** it is applied to every hour, not only daytime, and only when an export price sensor is configured (without one the export curve is the plain import curve, by design and by an upstream test). Selling in the evening would therefore be overvalued if you configure an export sensor with the bonus on. Fix in `pricing/engine.py::_apply_zonneplan_export_bonus` (daylight only) before using that combination.
- Not in the OmniBattery panel (`marstek-panel.js`) and not drawn in the daily timeline (arbitrage slots are deliberately left out of that projection). Add both later.
- Translations only for English and Dutch; other languages fall back to English.
- Docs are English only.


### Files touched in shared code (where upstream merges may conflict)

`__init__.py`, `pricing/engine.py`, `pricing/__init__.py`, `pricing/notifications.py`, `pricing/high_price_discharge.py`, `control/high_price_discharge.py`, `control/discharge_reserve.py`, `switch.py`, `number.py`, `sensor.py`, `diagnostics.py`, `const/integration_const.py`, `strings.json`, `translations/en.json`, `translations/nl.json`.
New files: `pricing/arbitrage.py`, `tests/test_arbitrage_planner.py`, `tests/test_arbitrage_charging.py`, `docs/fork/`.
