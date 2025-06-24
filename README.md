# ElectricalPriceCalc

A Python app that fetches electricity prices from [Nordpool](https://www.nordpoolgroup.com/) and calculates optimal charging times and energy-saving opportunities for your electric car.

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

> Originally part of a larger project called `ElectricalManagement`, this app was extracted into its own repository for easier integration into custom automation workflows.

---

## 📦 Dependencies

Install the required packages using `requirements.txt`:

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
    pricearea: NO5  # Nordpool price area (e.g., NO5, DE1)
    currency: NOK   # Default: EUR
    country_code: NO  # Used to fetch location data
    VAT: 1.25       # Default: 25% VAT (1.25 = 1 + 0.25)
    daytax: 0.5986  # Daytime electricity tax (optional)
    nighttax: 0.4713 # Nighttime electricity tax (optional)
    power_support_above: 0.9125  # Threshold for power support (includes VAT) (optional)
    support_amount: 0.9          # Percentage of support (e.g., 90%) (optional)
```

---

## 📌 Notes

- `country_code` will attempt to fetch latitude/longitude from your AppDaemon configuration if not defined.
- `VAT` is specified as a multiplier (e.g., 1.25 represents 25% VAT).
- Taxes and thresholds are optional and can be customized based on your region.

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
    if (
        len(self.time_to_save) == 0 # if not run before (Empty list)
        or self.electricity_price.tomorrow_valid # if tomorrow price is found
        or self.ADapi.now_is_between('00:00:00', '12:00:00') # Before tomorrow price is expected
    ):
        # Get time to turn down heaters
        self.time_to_save = self.electricity_price.findpeakhours(
            pricedrop = 0.1,
            max_continuous_hours = 12,
            on_for_minimum = 4,
            pricedifference_increase =  1.07,
            reset_continuous_hours = False,
            prev_peak_hours = self.time_to_save
        )
        """Finds peak variations in electricity price for saving purposes and returns list with datetime objects;
           'start', 'end' and 'duration' as a timedelta object for how long the electricity has been off.
        """

        if self.time_to_save:
            self.ADapi.log(f"Printout from test:{self.electricity_price.print_peaks(self.time_to_save)}")

        # Get runtime / chargetime
        """ Returns starttime, endtime and price for cheapest continuous hours with different results depenting on time the call was made.
        """
        starttime, stoptime, price = self.electricity_price.getContinuousCheapestTime(
            hoursTotal = 1.7,
            calculateBeforeNextDayPrices = False,
            finishByHour = 7
        )
        self.ADapi.log(
            f"Start: {starttime} "
            f"Stop: {stoptime} "
            f"with price: {price}"
        )
        # Find times to turn up heaters before price increase
        self.spend = self.electricity_price.findLowPriceHours(
            priceincrease = 0.8
        )
        """ Finds low price variations in electricity price for spending purposes and returns list with datetime objects.
        """

    else:
        self.ADapi.log(f"Tomorrows prices not ready. Check in 10 minutes") ###
        self.ADapi.run_in(self.get_new_prices, 600)

```
