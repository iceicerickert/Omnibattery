# Fork changelog

Changes carried by this fork on top of upstream (ffunes/Omnibattery). Kept in its own file so syncing upstream never conflicts with `CHANGELOG.md`.

## Unreleased

### Added

- **Price arbitrage charging** (opt-in, Dynamic Pricing). Buys extra energy in cheap periods, beyond the forecast deficit, when the later demand it replaces repays round-trip losses, the new **Arbitrage wear cost** and the **Minimum arbitrage margin**. Details: [arbitrage.md](arbitrage.md).
  - New pure planner `pricing/arbitrage.py`; new slot purpose `arbitrage` with its own planned energy per slot.
  - Entities: switch `arbitrage_charging`, number `arbitrage_wear_cost`, diagnostic sensor `arbitrage_plan` (attributes feed a dashboard card).
  - Re-planned one hour before each arbitrage slot and with every plan rebuild; needs the Discharge Reserve or Price-Based Discharge.
  - Notifications gained an arbitrage section; diagnostics export the new fields.

### Known gaps / later

- Values later energy at the **import price** only. The end of the salderingsregeling (2027) is not modelled: when the Zonneplan integration exposes import and export tariffs, give the planner a separate export price for the discharge value.
- Not in the OmniBattery panel (`marstek-panel.js`) and not drawn in the daily timeline (arbitrage slots are deliberately left out of that projection). Add both later.
- Translations only for English and Dutch; other languages fall back to English.
- Docs are English only.

### Files touched in shared code (where upstream merges may conflict)

`__init__.py`, `pricing/engine.py`, `pricing/__init__.py`, `pricing/notifications.py`, `switch.py`, `number.py`, `sensor.py`, `diagnostics.py`, `const/integration_const.py`, `strings.json`, `translations/en.json`, `translations/nl.json`.
New files: `pricing/arbitrage.py`, `tests/test_arbitrage_planner.py`, `tests/test_arbitrage_charging.py`, `docs/fork/`.
