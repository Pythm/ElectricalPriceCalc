# ElectricalPriceCalc: notes for an AI assistant

Read this with [README.md](README.md). The workflow for finding the user's real entity ids (long-lived token, `ha_snapshot.py`, asking when several match) is in the [Pythm_AppdaemonApps LLM guide](https://github.com/Pythm/Pythm_AppdaemonApps/blob/master/LLM_GUIDE.md). Follow its rules: use ids from the index only, ask when several match, one app per `apps.yaml` edit, back up `apps.yaml` first, no commit or push unasked.

## What the app does

Builds the electricity price list for today and tomorrow (spot price + VAT + day/night grid tax + extra tax - optional power support) and answers questions from other apps: cheapest continuous hours for charging, hours to save in, hours to spend in. It controls nothing and sends no events. Other apps read it with `get_app('<its app name>')`.

Needs: [ElectricalManagement](https://github.com/Pythm/ad-ElectricalManagement) and two apps in [Pythm_AppdaemonApps](https://github.com/Pythm/Pythm_AppdaemonApps) (`appliance_home_connect`, `log_energy_usage`) use it. Alone it does nothing visible. Not in HACS: install by `git clone`.

## Setting it up (steps for the assistant)

1. **Ask the questions in "ALWAYS ask the user" below.** The price source and the tax numbers decide everything and cannot be read from Home Assistant.
2. **Packages.** `requirements.txt` lists `nordpool`, `holidays`, `geopy`. `holidays` is always needed. `nordpool` only with `pricearea`. `geopy` only when `country_code` is left out. AppDaemon as a HA add-on: the user adds the packages in the add-on configuration and restarts it. Docker/pip: `pip install -r requirements.txt`.
3. **Clone** into the AppDaemon `apps` folder: `git clone https://github.com/Pythm/ElectricalPriceCalc.git`.
4. **Add one block** to `apps.yaml` (back it up first, show the block, explain it in a sentence).
5. **Make the apps that use it wait for it:** add the price app's name to their `dependencies:` and set their `electricalPriceApp` (or `price_app`) to the same name. The name must be the exact YAML key of this app.
6. **Check:** parse the YAML, then ask the user to reload AppDaemon and look at the log for ERROR or WARNING lines from this app (see Troubleshooting).

## Config (apps.yaml)

Pick **one** price source: `fixedprice`, `pricearea` or `nordpool`. Everything else is optional.

```yaml
electricalPriceCalc:            # the app name other apps must use
  module: electricalPriceCalc
  class: ElectricalPriceCalc
  country_code: 'NO'            # holidays: weekends and holidays use night tax all day
  currency: NOK                 # default EUR (only a label, no conversion)

  # --- price source, ONE of these ---
  fixedprice: 0.50              # fixed price per kWh incl. VAT (Norway: Norgespris)
  # pricearea: NO5              # Nordpool price area; needs the nordpool package
  # nordpool: sensor.xxx        # HA Nordpool integration sensor (omit to auto-detect)

  # --- taxes, from the user's grid company ---
  daytax: 0.4613                # per kWh, a number or a dict of months 1..12
  nighttax: 0.2329              # 22:00-06:00 on workdays, all day on weekends/holidays
  additional_tax: 0.034         # per kWh added to every hour
  # VAT: 1.25                   # multiplier, pricearea only
  # power_support_above: 0.9125 # Norway: strømstøtte threshold incl. VAT
  # support_amount: 0.9         # 90 %
```

Month dict form (all 12 keys required): `daytax: {1: 0.4782, 2: 0.4782, ... 12: 0.5986}`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `fixedprice` | float | none | Used as given, `VAT` is ignored. Day/night tax and `additional_tax` are still added. |
| `pricearea` | str | none | Nordpool area such as `NO5`, `SE3`, `DE1`. Prices are fetched in the background, retry every 30 min on failure. |
| `nordpool` | entity id | none | A sensor of the HA Nordpool integration with the attributes `raw_today`, `raw_tomorrow`, `tomorrow_valid`, `currency`. VAT is already in that sensor: `VAT` is ignored. |
| `currency` | str | `EUR` | With a `nordpool` sensor the sensor's own currency is used. |
| `VAT` | float | 1.25 with `pricearea`, else 0 | Multiplier, not a percentage. |
| `daytax`, `nighttax` | float or dict 1..12 | 0 | A dict must cover all 12 months. |
| `additional_tax` | float | 0 | |
| `power_support_above`, `support_amount` | float | 10.0, 0.0 | Support is off unless both are set. The support is computed on the price before taxes. |
| `country_code` | str | none | Two-letter code of the `holidays` package. Without it the country is looked up from AppDaemon's latitude/longitude (needs `geopy`); until then Norwegian holidays are used and a WARNING is logged. |

No source given: the app takes the first HA sensor with `nordpool` in its name and logs which one, or a WARNING if there is none. Prefer to write the source explicitly.

## Which entities fit each key

Only `nordpool` takes an entity. Search the index for `"nordpool"` in the entity id or `"integration":"nordpool"`, `sensor` domain, one per price area. Several matches (different areas or currencies): ask. The tax and support numbers are **not** in Home Assistant: never invent them.

## ALWAYS ask the user

- **How does the user pay for electricity?** A fixed price (Norway: Norgespris, `fixedprice`), spot price with the app fetching from Nordpool (`pricearea`), or spot price through the HA Nordpool integration (`nordpool`). If the user already has the HA integration, `nordpool` avoids a second source. Do not guess from the country.
- **The fixed price and whether it includes VAT** (`fixedprice` is used as written).
- **Price area, currency, country code** (the country decides the holidays).
- **Grid tax** (`daytax`, `nighttax`, per kWh, per month if it changes, and whether night tax applies on weekends) and **`additional_tax`** (levies, supplier margin): from the user's electricity bill or grid company. If the user does not know, leave them out and say that prices then exclude grid tax and cheapest-hour choices still work but absolute prices are wrong for any `charge_below_price` or `pricedrop` setting.
- **Power support** (`power_support_above`, `support_amount`): only for users who get it; otherwise leave both out.
- **The app name** other apps will use, and which of them (ElectricalManagement, `appliance_home_connect`, `log_energy_usage`) get it as `electricalPriceApp` / `price_app` and in `dependencies`.

## Behaviour not to "fix"

- `electricity_price_now()` and the search methods can return `None` / `(None, None, None)`: before the first fetch, in a gap between days, or between 06:00 and 15:00 when tomorrow's prices are missing (then only the price is returned, `(None, None, price)`). Callers must handle it.
- Prices are per slot (hourly or 15 minutes). Days with 23 or 25 hours have 23 or 25 hourly slots.
- The daily refresh (00:01 today, 13:00 today + tomorrow) uses its own local-time timer on purpose (`_run_daily_local`). AppDaemon's `run_daily` drifts by an hour after a daylight-saving change and made the 00:01 rebuild run at 23:01, which removed tomorrow's prices. Do not change it back to `run_daily`.
- Support is calculated on the price before taxes and `daytax`/`nighttax` is chosen from the slot's time. Both intentional.
- `find_times_to_spend(..., lowest_hours=None)`: default 3 lowest hours of the day. ElectricalManagement passes its `low_price_max_continuous_hours`.
- More than one of `fixedprice`, `pricearea`, `nordpool` is an error (older versions silently used the first).

## API for other apps

```python
price_app = self.ADapi.get_app('electricalPriceCalc')   # the user's app name
price_app.electricity_price_now()                       # float or None
price_app.get_Continuous_Cheapest_Time(hoursTotal=2, calculateBeforeNextDayPrices=False,
    finishByHour=7, startBeforePrice=0.01, stopAtPriceIncrease=0.01)   # (start, stop, avg price)
price_app.get_lowest_prices(checkitem=1, hours=6, min_change=None)       # float or None
price_app.find_times_to_save(pricedrop, max_continuous_hours, on_for_minimum,
    pricedifference_increase, reset_continuous_hours, previous_save_hours)  # list of PeakHour
price_app.find_times_to_spend(priceincrease, lowest_hours=None)          # list of PeakHour
price_app.print_peaks(saving_hours_list)                                 # text
price_app.elpricestoday      # list of PriceHour(start, end, value), today + tomorrow when known
price_app.tomorrow_valid     # bool
price_app.currency, price_app.current_daytax, price_app.current_nighttax
```

Read the lists through the properties; they are replaced as one snapshot, never edited in place.

## Dependency pitfalls

- A consumer that starts before this app finds no app (`get_app` returns `None`). Put this app's name in the consumer's `dependencies`.
- A `dependencies` name that is not an app in `apps.yaml` stops AppDaemon from starting the dependent apps. Check the name letter by letter.
- Reloading this app reloads every app that depends on it. That is expected after an update.

## Troubleshooting (log lines of this app)

| Line | Meaning |
|---|---|
| `Invalid config for <name>: <key>: ...` (ERROR) | A value failed validation (tax dict missing months, two price sources, wrong type). The app does not start. |
| `The 'nordpool' package is required for 'pricearea' mode` (ERROR) | Install the `nordpool` package. |
| `No price source configured and no sensor containing 'nordpool' found` (WARNING) | Set `fixedprice`, `pricearea` or `nordpool`. |
| `Auto-discovered Nordpool sensor ...` (INFO) | No source was set; confirm that this is the sensor the user wants. |
| `No raw_today prices on <sensor>. Retrying in 1800 s` (WARNING) | The HA sensor has no data yet; check the integration. |
| `Nordpool prices today/tomorrow failed: ...` (WARNING) | Fetch failed; retried every 30 min. Tomorrow's prices missing after 13:00 are retried every 10 min. |
| `No country_code configured ...` / `Could not find holidays for <code>` (WARNING) | Norwegian holidays are used. Set a valid `country_code`. |
| `Failed to get country code from geolocation` (WARNING) | `geopy` missing or no network; set `country_code`. |
| `No electricity prices available yet for usage logging` (INFO, `log_energy_usage`) | This app has no price for now. Look for an error above; right after a restart it is normal. |

## Rules for the assistant

- Do not invent tax values, prices or an app name. Ask.
- Do not edit `electricalPriceCalc.py` to change the price formula, the refresh times or the support calculation unless the user asks.
- Change only this app's block and the `dependencies` / `electricalPriceApp` lines of the apps that use it. One app per edit, with a backup.
- After a change, check the log: no ERROR or WARNING from this app, and in `log_energy_usage` or ElectricalManagement no "prices not ready" lines after the first minutes.
