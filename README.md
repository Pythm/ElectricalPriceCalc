# ElectricalPriceCalc

A Python app that fetches electricity prices from [Nordpool](https://www.nordpoolgroup.com/) and calculates optimal charging times and energy-saving opportunities for your electric equipment.

---

## 📱 Supported Platforms

This app is designed to work with [**AppDaemon**](https://github.com/AppDaemon/appdaemon) and [**Home Assistant**](https://www.home-assistant.io/).

- **Home Assistant** is a popular open-source home automation platform with extensive device integration. If you're not already using it, we recommend exploring [its website](https://www.home-assistant.io/).
- **AppDaemon** is a multi-threaded, sandboxed Python environment for writing automation apps, compatible with Home Assistant and MQTT-based systems.

---

## 🔍 How It Works

The app calculates:
- Optimal **electric vehicle charging times** based on future Nordpool prices (including taxes).
- When to **spend or save electricity** for cost efficiency.

> Originally part of [ad-ElectricalManagement](https://github.com/Pythm/ad-ElectricalManagement). This app was extracted into its own repository for easier integration into custom automation workflows. The ad-ElectricalManagement app will use this app going forward.

---

## 📦 Dependencies

Install the required packages using `requirements.txt` if your Appdaemon install method does not handle requirements automatically:

```bash
pip install -r requirements.txt
```

---

## 🛠️ Installation & Configuration

1. **Clone the repository** into your [AppDaemon](https://appdaemon.readthedocs.io/en/latest/) `apps` directory:
   ```bash
   git clone https://github.com/Pythm/ElectricalPriceCalc.git /path/to/appdaemon/apps/
   ```

2. **Configure the app** in your AppDaemon configuration file (`.yaml` or `.toml`):

```yaml
electricalPriceCalc:
  module: electricalPriceCalc
  class: ElectricalPriceCalc
  pricearea: 'NO5'  # Nordpool price area (e.g., NO5, DE1)
  currency: 'NOK'   # Default: EUR
  country_code: 'NO'  # Used to fetch location data
  VAT: 1.25       # Default: 25% VAT (1.25 = 1 + 0.25)
  daytax: # Daytime electricity tax (optional)
    1: 0.4782
    2: 0.4782
    3: 0.4782
    4: 0.5986
    5: 0.5986
    6: 0.5986
    7: 0.5986
    8: 0.5986
    9: 0.5986
    10: 0.5986
    11: 0.5986
    12: 0.5986
  nighttax: # Nighttime electricity tax (optional)
    1: 0.3602
    2: 0.3602
    3: 0.3602
    4: 0.4713
    5: 0.4713
    6: 0.4713
    7: 0.4713
    8: 0.4713
    9: 0.4713
    10: 0.4713
    11: 0.4713
    12: 0.4713
  additional_tax: 0.0295
  power_support_above: 0.9125  # Threshold for power support (includes VAT) (optional)
  support_amount: 0.9          # Percentage of support (e.g., 90%) (optional)
```

---

## 📌 Notes

- `country_code` is used to find hollidays. Will attempt to fetch latitude/longitude from your AppDaemon configuration if not defined.
- `VAT` is specified as a multiplier (e.g., 1.25 represents 25% VAT) and is applied only to Nordpool Price before adding the other taxes.
- Taxes and thresholds are optional and can be customized based on your region.
- Add tax per kWh from your electricity grid provider with `daytax` and `nighttax`. Night tax applies from 22:00 to 06:00 on workdays and all day on weekends and hollidays. Can be a float or a dict with month number and tax like example above.
- In Norway, we receive 90% electricity support (Strømstøtte) on electricity prices above 0.70 kr exclusive / 0.9125 kr inclusive VAT (MVA) calculated per hour. Define `power_support_above` and `support_amount` to have calculations take the support into account. Do not define if not applicable.
- In Norway, we can also choose **“Norgespris,”** a fixed‑price option. Configure the price with `fixedprice` instead of `pricearea`. If you are in an area with a fixed electricity price and only want to use [ad‑ElectricalManagement](https://github.com/Pythm/ad-ElectricalManagement) to stay below a maximum kW per‑hour usage, this setting is the right choice.
---

## ✅ Contributing

Contributions are welcome! Please open an issue or submit a pull request.

---

## 🔗 Links

- [Nordpool](https://www.nordpoolgroup.com/)
- [AppDaemon Docs](https://appdaemon.readthedocs.io/en/latest/)
- [Home Assistant](https://www.home-assistant.io/)

---

### 📌 Example Use Case

```python
    ELECTRICITYPRICE = self.ADapi.get_app(self.args['electricalPriceApp'])

    price_now = ELECTRICITYPRICE.electricity_price_now()

    startAt, stopNoLaterThan, price = ELECTRICITYPRICE.get_Continuous_Cheapest_Time(
            hoursTotal = 2,
            calculateBeforeNextDayPrices = False,
            finishByHour = 7,
            startBeforePrice = 0.01, 
            stopAtPriceIncrease = 0.01
        )

    time_to_save:list = []
    time_to_save = ELECTRICITYPRICE.find_times_to_save(
        pricedrop = 0.08,
        max_continuous_hours = 12,
        on_for_minimum = 6,
        pricedifference_increase = 1.07,
        reset_continuous_hours = False,
        previous_save_hours = time_to_save
    )

    time_to_spend = ELECTRICITYPRICE.find_times_to_spend(
        priceincrease = 0.5
    )
```
