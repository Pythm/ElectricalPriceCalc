""" Electrical Price Calculations

    @Pythm / https://github.com/Pythm
"""

__version__ = "0.1.8"

from appdaemon import adbase as ad
import datetime
import math
import bisect
from typing import NamedTuple
import holidays
from pydantic import ValidationError
from pydantic_models_price import PeakHour, PriceHour, PriceCalcConfig

# ---------------------------------------------------------------------------
# Module constants. Values are unchanged from 0.1.7, only named.
# ---------------------------------------------------------------------------
RETRY_SECONDS = 1800                 # retry delay after a failed price fetch
TOMORROW_RETRY_SECONDS = 600         # retry delay for missing tomorrow spot prices after 13:00
DAILY_FETCH_TIME = "00:01:00"
TOMORROW_FETCH_TIME = "13:00:00"     # Nordpool publishes next-day prices around 13:00
FIXEDPRICE_INIT_TOMORROW_FROM = "12:50:00"
NIGHT_TAX_ENDS_AT = "06:00:00"
NIGHT_TAX_STARTS_AT = "22:00:00"
EARLY_RETURN_WINDOW = ("06:00:00", "15:00:00")   # no tomorrow prices yet: only return a price
NORDPOOL_RESOLUTION_MINUTES = 15
STOP_CALCULATING_AFTER_HOURS = 40    # never plan save hours further ahead than this
AFTER_PEAK_PRICE_DEFAULT = 100       # sentinel "no peak found" price
EXTEND_START_LOOKAHEAD_SLOTS = 4
START_PRICE_INCREASE_FACTOR = 1.5
NEXT_PRICE_INCREASE_FACTOR = 1.3
START_BEFORE_PRICE_FACTOR = 2
SPEND_SHORT_INCREASE_FACTOR = 0.6
SPEND_TWO_HOUR_INCREASE_FACTOR = 1.4
PEAK_TWO_HOUR_DROP_FACTOR = 1.3
SPEND_LOWEST_HOURS = 3
PRINT_PEAKS_UNTIL_HOUR = 16
FALLBACK_COUNTRY = 'Norway'


class _PriceSnapshot(NamedTuple):
    """Everything derived from one price calculation, published as a single rebind so readers
    never see a start list that does not match the price list."""
    prices: list[PriceHour]
    starts: list[datetime.datetime]
    ends: list[datetime.datetime]
    sorted_today: list[float]
    sorted_tomorrow: list[float]
    todayslength: int
    tomorrow_valid: bool
    slots_per_hour: int


_EMPTY_SNAPSHOT = _PriceSnapshot([], [], [], [], [], 0, False, 1)


class ElectricalPriceCalc(ad.ADBase):

    def initialize(self):
        self.ADapi = self.get_ad_api()
        self._snapshot: _PriceSnapshot = _EMPTY_SNAPSHOT
        self._retry_handle: str | None = None
        now = self.ADapi.datetime(aware=True)

        self.cfg = self._validate_config()
        self.daytax = self.cfg.daytax
        self.nighttax = self.cfg.nighttax
        self.additional_tax: float = self.cfg.additional_tax
        self.power_support_above: float = self.cfg.power_support_above
        self.support_amount: float = self.cfg.support_amount
        self.current_daytax, self.current_nighttax = self._taxes_for_month(now.month)

        self._setup_holidays(now)
        self._setup_price_source(now)

    # ------------------------------------------------------------------
    # Published, read-only views of the current snapshot
    # ------------------------------------------------------------------
    @property
    def elpricestoday(self) -> list[PriceHour]:
        return self._snapshot.prices

    @property
    def sorted_elprices_today(self) -> list[float]:
        return self._snapshot.sorted_today

    @property
    def sorted_elprices_tomorrow(self) -> list[float]:
        return self._snapshot.sorted_tomorrow

    @property
    def todayslength(self) -> int:
        return self._snapshot.todayslength

    @property
    def tomorrow_valid(self) -> bool:
        return self._snapshot.tomorrow_valid

    @property
    def _start_times(self) -> list[datetime.datetime]:
        return self._snapshot.starts

    @property
    def _end_times(self) -> list[datetime.datetime]:
        return self._snapshot.ends

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _validate_config(self) -> PriceCalcConfig:
        try:
            return PriceCalcConfig.model_validate(dict(self.args))
        except ValidationError as e:
            for err in e.errors():
                loc = '.'.join(str(part) for part in err['loc']) or '<root>'
                self.ADapi.log(f"Invalid config for {self.name}: {loc}: {err['msg']}", level='ERROR')
            raise

    def _setup_holidays(self, now: datetime.datetime) -> None:
        """Always start with a Norway calendar so _is_holiday works immediately; override from
        country_code, or asynchronously from a reverse geocode when only lat/long is known."""
        self._holiday_years = [now.year, now.year + 1]
        self.holidays = holidays.Norway(years=self._holiday_years)
        self.country_code = self.cfg.country_code

        if self.country_code is not None:
            self._apply_country_code(self.country_code)
        elif 'latitude' in self.config and 'longitude' in self.config:
            self.ADapi.submit_to_executor(
                self._geocode_country_worker,
                self.config['latitude'],
                self.config['longitude'],
                callback=self._on_country_code_geocoded
            )
        else:
            self.ADapi.log(
                f"No country_code configured and no latitude/longitude available. "
                f"Using {FALLBACK_COUNTRY} holidays in {self.name}",
                level='WARNING'
            )

    def _apply_country_code(self, country_code: str) -> bool:
        try:
            holiday_class = getattr(holidays, country_code.upper())
        except AttributeError:
            self.ADapi.log(
                f"Could not find holidays for {country_code}, defaulting to {FALLBACK_COUNTRY}.",
                level='WARNING'
            )
            return False
        self.holidays = holiday_class(years=self._holiday_years)   # atomic swap
        self.country_code = country_code
        self.ADapi.log(f"Country code set to {country_code.upper()} in {self.name}", level='INFO')
        return True

    def _geocode_country_worker(self, latitude: float, longitude: float) -> dict:
        """Runs in AppDaemon's thread pool: network only, no AppDaemon API calls. Never raises."""
        try:
            from geopy.geocoders import Nominatim
            geolocator = Nominatim(user_agent="ElectricalPriceCalc")
            location = geolocator.reverse((latitude, longitude), language='en')
            return {'country_code': location.raw['address'].get('country_code', 'NO')}
        except Exception as e:
            return {'error': str(e)}

    def _on_country_code_geocoded(self, kwargs) -> None:
        result = kwargs.get('result') or {}
        if 'country_code' not in result:
            self.ADapi.log(
                f"Failed to get country code from geolocation: {result.get('error')}. "
                f"Keeping {FALLBACK_COUNTRY} holidays.",
                level='WARNING'
            )
            return
        self._apply_country_code(result['country_code'])

    def _setup_price_source(self, now: datetime.datetime) -> None:
        cfg = self.cfg
        self.currency: str = cfg.currency
        # VAT is only applied to raw spot prices (pricearea mode). Fixed prices and the Home
        # Assistant Nordpool sensor already include VAT, so it is intentionally unused there.
        self.VAT: float = cfg.VAT

        if cfg.fixedprice is not None:
            self._setup_fixedprice(cfg.fixedprice)
        elif cfg.pricearea is not None:
            self._setup_pricearea(cfg.pricearea)
        elif cfg.nordpool is not None:
            self._setup_nordpool_sensor(cfg.nordpool)
        else:
            self._setup_nordpool_autodiscover()

    def _setup_fixedprice(self, fixedprice: float) -> None:
        tomorrow = self.ADapi.now_is_between(FIXEDPRICE_INIT_TOMORROW_FROM, '23:59:59')
        self._create_daily_prices_with_taxes(price=fixedprice, tomorrow=tomorrow)
        self.ADapi.run_daily(self._create_daily_prices_with_taxes, DAILY_FETCH_TIME, price=fixedprice, tomorrow=False)
        self.ADapi.run_daily(self._create_daily_prices_with_taxes, TOMORROW_FETCH_TIME, price=fixedprice, tomorrow=True)

    def _setup_pricearea(self, pricearea: str) -> None:
        try:
            from nordpool import elspot
        except ImportError as e:
            self.ADapi.log(
                "The 'nordpool' package is required for 'pricearea' mode. Install it with: pip install nordpool",
                level='ERROR'
            )
            raise ImportError("nordpool package missing (pip install nordpool)") from e
        self.pricearea = pricearea
        self.prices_spot = elspot.Prices(self.currency)
        self._fetchNordpoolSpotPrices({})
        self.ADapi.run_daily(self._fetchNordpoolSpotPrices, DAILY_FETCH_TIME)
        self.ADapi.run_daily(self._fetchNordpoolSpotPrices, TOMORROW_FETCH_TIME)

    def _setup_nordpool_sensor(self, entity_id: str) -> None:
        self.nordpool_prices = entity_id
        self._fetchNordpoolPrices({})
        self.ADapi.listen_state(self._update_price_rundaily, self.nordpool_prices, attribute='tomorrow')
        self.ADapi.run_daily(self._fetchNordpoolPrices, DAILY_FETCH_TIME)

    def _setup_nordpool_autodiscover(self) -> None:
        sensors = self.ADapi.get_state('sensor') or {}
        entity_id = next((eid for eid in sensors if 'nordpool' in eid), None)
        if entity_id is None:
            self.ADapi.log(
                "No price source configured and no sensor containing 'nordpool' found. "
                "Configure fixedprice, pricearea or nordpool.",
                level='WARNING'
            )
            return
        self.ADapi.log(f"Auto-discovered Nordpool sensor {entity_id} for {self.name}", level='INFO')
        self._setup_nordpool_sensor(entity_id)

    def _schedule_retry(self, callback, delay_seconds: int) -> None:
        """Keep a single pending retry timer: re-arming replaces any earlier one."""
        if self._retry_handle is not None:
            self.ADapi.cancel_timer(self._retry_handle, silent=True)
        self._retry_handle = self.ADapi.run_in(callback, delay_seconds)

    def _update_price_rundaily(self, entity, attribute, old, new, kwargs) -> None:
        self._fetchNordpoolPrices({})

    # ------------------------------------------------------------------
    # Fetch Nordpool prices with elspot (pricearea mode)
    # ------------------------------------------------------------------
    def _fetchNordpoolSpotPrices(self, kwargs) -> None:
        """Submit the blocking HTTP fetch to AppDaemon's thread pool; the result is delivered to
        _on_spot_prices_fetched on an app thread."""
        today = self.ADapi.datetime(aware=True).date()
        self.ADapi.submit_to_executor(self._fetch_spot_prices_worker, today, callback=self._on_spot_prices_fetched)

    def _fetch_spot_prices_worker(self, today: datetime.date) -> dict:
        """Runs in AppDaemon's thread pool: network only, no AppDaemon API calls. Never raises."""
        result = {'today': None, 'tomorrow': None, 'today_error': None, 'tomorrow_error': None}
        try:
            result['today'] = self.prices_spot.fetch(
                end_date=today,
                areas=[self.pricearea],
                resolution=NORDPOOL_RESOLUTION_MINUTES
            )
        except Exception as e:
            result['today_error'] = str(e)
        try:
            result['tomorrow'] = self.prices_spot.fetch(
                areas=[self.pricearea],
                resolution=NORDPOOL_RESOLUTION_MINUTES
            )
        except Exception as e:
            result['tomorrow_error'] = str(e)
        return result

    def _on_spot_prices_fetched(self, kwargs) -> None:
        result = kwargs.get('result') or {}

        todays_values = self._extract_spot_values(result.get('today'))
        if todays_values is None:
            self.ADapi.log(
                f"Nordpool prices today failed: {result.get('today_error') or 'no usable data'}. "
                f"Retrying in {RETRY_SECONDS} s",
                level='WARNING'
            )
            self._schedule_retry(self._fetchNordpoolSpotPrices, RETRY_SECONDS)
            return

        tomorrow_values = self._extract_spot_values(result.get('tomorrow'))
        if tomorrow_values is None:
            if result.get('tomorrow_error'):
                self.ADapi.log(
                    f"Nordpool prices tomorrow failed: {result['tomorrow_error']}. Retrying in {RETRY_SECONDS} s",
                    level='WARNING'
                )
                self._schedule_retry(self._fetchNordpoolSpotPrices, RETRY_SECONDS)
            elif self.ADapi.datetime(aware=True) > self.ADapi.parse_datetime(TOMORROW_FETCH_TIME, today=True, aware=True):
                self.ADapi.log(
                    f"Nordpool prices for tomorrow not published yet. Retrying in {TOMORROW_RETRY_SECONDS} s",
                    level='DEBUG'
                )
                self._schedule_retry(self._fetchNordpoolSpotPrices, TOMORROW_RETRY_SECONDS)

        nordpool_todays_prices = self._correctDictsNordpoolSpotPrices(nordpool_prices=todays_values)
        nordpool_tomorrow_prices = []
        if tomorrow_values is not None:
            nordpool_tomorrow_prices = self._correctDictsNordpoolSpotPrices(nordpool_prices=tomorrow_values)

        self._calculatePrices(nordpool_todays_prices=nordpool_todays_prices,
                              nordpool_tomorrow_prices=nordpool_tomorrow_prices)

    def _extract_spot_values(self, fetched) -> list | None:
        """Return the value list for the configured area, or None when the fetch is unusable.
        The nordpool library returns None or inf for prices that are not published yet."""
        if not fetched:
            return None
        try:
            values = fetched['areas'][self.pricearea]['values']
        except (KeyError, TypeError):
            return None
        if not values:
            return None
        for item in values:
            try:
                if item['value'] is None or not math.isfinite(float(item['value'])):
                    return None
            except (KeyError, TypeError, ValueError):
                return None
        return values

    def _correctDictsNordpoolSpotPrices(self, nordpool_prices: list) -> list:
        tz = self.ADapi.get_timezone()
        for item in nordpool_prices:
            item['value'] = (float(item['value']) / 1000) * self.VAT # convert price from pr mega to kilo and adds VAT
            item['start'] = item['start'].astimezone(tz)
            item['end'] = item['end'].astimezone(tz)
        return nordpool_prices

    # ------------------------------------------------------------------
    # Fetch Nordpool prices from the Home Assistant integration sensor
    # ------------------------------------------------------------------
    def _fetchNordpoolPrices(self, kwargs) -> None:
        todays_prices = self.ADapi.get_state(entity_id=self.nordpool_prices, attribute='raw_today')
        if not todays_prices:
            self.ADapi.log(
                f"No raw_today prices on {self.nordpool_prices}. Retrying in {RETRY_SECONDS} s",
                level='WARNING'
            )
            self._schedule_retry(self._fetchNordpoolPrices, RETRY_SECONDS)
            return

        currency = self.ADapi.get_state(entity_id=self.nordpool_prices, attribute='currency')
        if currency:
            self.currency = currency

        # Tomorrows prices if available. Compare before the in-place date conversion below.
        tomorrow_prices = []
        if self.ADapi.get_state(entity_id=self.nordpool_prices, attribute='tomorrow_valid'):
            raw_tomorrow = self.ADapi.get_state(entity_id=self.nordpool_prices, attribute='raw_tomorrow')
            if raw_tomorrow and raw_tomorrow != todays_prices:
                tomorrow_prices = raw_tomorrow

        nordpool_todays_prices = self._correctDictsNordpoolIntegrationPrices(nordpool_prices=todays_prices)
        nordpool_tomorrow_prices = []
        if tomorrow_prices:
            nordpool_tomorrow_prices = self._correctDictsNordpoolIntegrationPrices(nordpool_prices=tomorrow_prices)

        self._calculatePrices(nordpool_todays_prices=nordpool_todays_prices,
                              nordpool_tomorrow_prices=nordpool_tomorrow_prices)

    def _correctDictsNordpoolIntegrationPrices(self, nordpool_prices: list) -> list:
        for item in nordpool_prices:
            item['start'] = self.ADapi.convert_utc(item['start'])
            item['end'] = self.ADapi.convert_utc(item['end'])
        return nordpool_prices

    # ------------------------------------------------------------------
    # Fixed price slots
    # ------------------------------------------------------------------
    def _create_daily_prices_with_taxes(self, **kwargs) -> None:
        price = kwargs['price']
        tomorrow = kwargs['tomorrow']
        nordpool_todays_prices: list = self.create_time_slots(today=True, price=price)
        nordpool_tomorrow_prices: list = self.create_time_slots(today=False, price=price) if tomorrow else []

        self._calculatePrices(nordpool_todays_prices=nordpool_todays_prices,
                              nordpool_tomorrow_prices=nordpool_tomorrow_prices)

    def create_time_slots(self, today: bool, price: float) -> list:
        """One slot per real hour between local midnight and the next local midnight, so a
        DST-change day yields 23 or 25 slots with correct offsets (normal days: 24)."""
        offset = 0 if today else 1
        start_day = self.ADapi.parse_datetime("00:00:00", today=True, aware=True, days_offset=offset)
        end_day = self.ADapi.parse_datetime("00:00:00", today=True, aware=True, days_offset=offset + 1)
        tz = start_day.tzinfo

        slots = []
        cur = start_day.astimezone(datetime.timezone.utc)
        end_utc = end_day.astimezone(datetime.timezone.utc)
        while cur < end_utc:
            nxt = cur + datetime.timedelta(hours=1)
            slots.append({
                "start": cur.astimezone(tz),
                "end":   nxt.astimezone(tz),
                "value": price,
            })
            cur = nxt

        return slots

    # ------------------------------------------------------------------
    # Tax calculation and publishing
    # ------------------------------------------------------------------
    def _taxes_for_month(self, month: int) -> tuple[float, float]:
        daytax = self.daytax[month] if isinstance(self.daytax, dict) else self.daytax
        nighttax = self.nighttax[month] if isinstance(self.nighttax, dict) else self.nighttax
        return daytax, nighttax

    def _calculatePrices(self,
                         nordpool_todays_prices: list,
                         nordpool_tomorrow_prices: list) -> None:
        """Applies taxes/support to the raw slots and publishes one consistent snapshot."""
        now = self.ADapi.datetime(aware=True)
        today = now.date()
        prices: list[PriceHour] = []

        isNotWorkday: bool = self._is_holiday(today)
        if not isNotWorkday:
            isNotWorkday = now.weekday() > 4
        beforesix = self.ADapi.parse_datetime(NIGHT_TAX_ENDS_AT, today=True, aware=True)
        aftertwentytwo = self.ADapi.parse_datetime(NIGHT_TAX_STARTS_AT, today=True, aware=True)

        # Todays prices
        month = nordpool_todays_prices[0]['start'].month if nordpool_todays_prices else now.month
        today_daytax, today_nighttax = self._taxes_for_month(month)
        sorted_today = self._doCalculationPricesInclVat(nordpool_prices=nordpool_todays_prices,
                                                        beforesix=beforesix,
                                                        aftertwentytwo=aftertwentytwo,
                                                        isNotWorkday=isNotWorkday,
                                                        daytax=today_daytax,
                                                        nighttax=today_nighttax,
                                                        prices_out=prices)
        todayslength = len(sorted_today)

        # Tomorrows prices if available
        sorted_tomorrow: list[float] = []
        tomorrow_valid = False
        if len(nordpool_tomorrow_prices) > 0:
            tomorrow_valid = True
            isNotWorkday = self._is_holiday(today + datetime.timedelta(days=1))
            # Intentional: tomorrow is a weekend day when today is Friday or Saturday.
            if now.weekday() == 4 or now.weekday() == 5:
                isNotWorkday = True
            beforesix += datetime.timedelta(days=1)
            aftertwentytwo += datetime.timedelta(days=1)

            month = nordpool_tomorrow_prices[0]['start'].month
            tomorrow_daytax, tomorrow_nighttax = self._taxes_for_month(month)
            sorted_tomorrow = self._doCalculationPricesInclVat(nordpool_prices=nordpool_tomorrow_prices,
                                                               beforesix=beforesix,
                                                               aftertwentytwo=aftertwentytwo,
                                                               isNotWorkday=isNotWorkday,
                                                               daytax=tomorrow_daytax,
                                                               nighttax=tomorrow_nighttax,
                                                               prices_out=prices)

        # current_* always reflect TODAY's taxes.
        self.current_daytax = today_daytax
        self.current_nighttax = today_nighttax
        self._snapshot = _PriceSnapshot(
            prices=prices,
            starts=[item.start for item in prices],
            ends=[item.end for item in prices],
            sorted_today=sorted_today,
            sorted_tomorrow=sorted_tomorrow,
            todayslength=todayslength,
            tomorrow_valid=tomorrow_valid,
            slots_per_hour=self._slots_per_hour(prices),
        )

    @staticmethod
    def _slots_per_hour(prices: list[PriceHour]) -> int:
        """Derived from the first slot's length: 1 for hourly data, 4 for 15-minute data."""
        if not prices:
            return 1
        seconds = (prices[0].end - prices[0].start).total_seconds()
        return max(1, round(3600 / seconds)) if seconds > 0 else 1

    def _doCalculationPricesInclVat(self,
                                    nordpool_prices: list,
                                    beforesix: datetime.datetime,
                                    aftertwentytwo: datetime.datetime,
                                    isNotWorkday: bool,
                                    daytax: float,
                                    nighttax: float,
                                    prices_out: list[PriceHour]) -> list[float]:
        """Appends a PriceHour per slot to prices_out and returns the slot prices sorted ascending."""
        sorted_elprices: list[float] = []
        for item in nordpool_prices:
            calculated_support: float = 0.0 # Power support calculation

            # Support is computed on the pre-tax value (intentional).
            if float(item['value']) > self.power_support_above:
                calculated_support = (float(item['value']) - self.power_support_above ) * self.support_amount
            if (
                item['end'] <= beforesix
                or item['start'] >= aftertwentytwo
                or isNotWorkday
            ):
                item['value'] = round(float(item['value']) + nighttax + self.additional_tax - calculated_support, 3)
            else:
                item['value'] = round(float(item['value']) + daytax + self.additional_tax - calculated_support, 3)
            sorted_elprices.append(item['value'])

            prices_out.append(PriceHour(
                start=item['start'],
                end=item['end'],
                value=item['value']
            ))
        return sorted(sorted_elprices)

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _td_hours(td: datetime.timedelta) -> float:
        """Timedelta to hours using whole minutes (seconds truncated), exactly as the original formula."""
        return (td.days * 24 * 60 + td.seconds // 60) / 60

    def _local_midnight_plus_hours(self, hours: int) -> datetime.datetime:
        """Today's local midnight plus `hours` of wall-clock time (DST-safe)."""
        midnight = self.ADapi.parse_datetime("00:00:00", today=True, aware=True)
        tz = midnight.tzinfo
        naive = midnight.replace(tzinfo=None) + datetime.timedelta(hours=hours)
        if hasattr(tz, 'localize'):   # pytz zone: attach the correct offset for the wall time
            return tz.localize(naive)
        return naive.replace(tzinfo=tz)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_Continuous_Cheapest_Time(self,
                                     hoursTotal: float = 2,
                                     calculateBeforeNextDayPrices: bool = False,
                                     finishByHour: int = 7,
                                     startBeforePrice: float = 0.01,
                                     stopAtPriceIncrease: float = 0.01
                                     ) -> tuple[datetime.datetime | None, datetime.datetime | None, float | None]:
        """ Returns (start time, end time, average price) for the cheapest continuous hours,
            with different results depending on the time the call was made.
            Between 06:00 and 15:00 without tomorrow's prices only the price is returned:
            (None, None, price). (None, None, None) means no prices are available. """

        snap = self._snapshot
        prices, starts, ends = snap.prices, snap.starts, snap.ends
        if not prices:
            return None, None, None
        now = self.ADapi.datetime(aware=True)

        indexesToFinish = math.ceil(hoursTotal * snap.slots_per_hour)
        if indexesToFinish == 0:
            indexesToFinish = 1   # intentional floor: always at least one slot

        finishAt = self._local_midnight_plus_hours(finishByHour)
        # Intentional precedence: (after 13:00 and tomorrow prices present) or finishAt already passed.
        if (
            self.ADapi.now_is_between(TOMORROW_FETCH_TIME, '23:59:59')
            and len(prices) > snap.todayslength
            or finishAt < now
        ):
            finishAt += datetime.timedelta(days=1)

        elif (
            self.ADapi.now_is_between(*EARLY_RETURN_WINDOW)
            and len(prices) == snap.todayslength
            and not calculateBeforeNextDayPrices
        ):
            sorted_today = snap.sorted_today
            if not sorted_today:
                return None, None, None
            return None, None, sorted_today[min(indexesToFinish, len(sorted_today) - 1)]

        priceToComplete: float = 0.0
        avgPriceToComplete: float = math.inf

        # Hour-floored on purpose, also with 15-minute slots.
        checkTime = now.replace(minute=0, second=0, microsecond=0)
        index_start = bisect.bisect_left(starts, checkTime)
        index_end = bisect.bisect_right(ends, finishAt)
        if index_start >= len(prices):
            return None, None, None
        startTime = None
        endTime = None
        start_at_index = index_start

        if index_start < index_end - indexesToFinish:
            index_end -= indexesToFinish
            while index_start <= index_end:
                for item in prices[index_start:index_start + indexesToFinish]:
                    priceToComplete += item.value
                if priceToComplete < avgPriceToComplete:
                    avgPriceToComplete = priceToComplete
                    startTime = prices[index_start].start
                    endTime = prices[index_start + indexesToFinish - 1].end
                    start_at_index = index_start

                priceToComplete = 0.0
                index_start += 1
        else:
            if index_start + indexesToFinish > len(prices):
                index_end = len(prices)
            for item in prices[index_start:index_end]:
                priceToComplete += item.value
            startTime = prices[index_start].start
            endTime = prices[index_end - 1].end
            avgPriceToComplete = priceToComplete

        if startTime is None:
            return None, None, None
        avgPriceToComplete = round(avgPriceToComplete / indexesToFinish, 3)

        # Get highest price:
        highest_price = avgPriceToComplete
        for item in prices[start_at_index:start_at_index + indexesToFinish]:
            if highest_price < item.value:
                highest_price = item.value

        endTime = self._extend_Continuous_Cheapest_EndTime(snap=snap,
                                                           endTime=endTime,
                                                           price=highest_price,
                                                           stopAtPriceIncrease=stopAtPriceIncrease)

        final_startTime = self._extend_Continuous_Cheapest_StartTime(snap=snap,
                                                                     checkTime=checkTime,
                                                                     startTime=startTime,
                                                                     price=highest_price,
                                                                     startBeforePrice=startBeforePrice,
                                                                     stopAtPriceIncrease=stopAtPriceIncrease)
        return final_startTime, endTime, avgPriceToComplete

    def _extend_Continuous_Cheapest_EndTime(self,
                                            snap: _PriceSnapshot,
                                            endTime: datetime.datetime,
                                            price: float,
                                            stopAtPriceIncrease: float) -> datetime.datetime:
        prices = snap.prices
        index_start = bisect.bisect_left(snap.ends, endTime)

        for i, current in enumerate(prices[index_start:]):
            original_index = index_start + i
            next_item = prices[original_index + 1] if original_index < len(prices) - 1 else None

            if next_item is None:
                return current.end
            if price + stopAtPriceIncrease < next_item.value:
                return current.end
        return endTime

    def _extend_Continuous_Cheapest_StartTime(self,
                                              snap: _PriceSnapshot,
                                              checkTime: datetime.datetime,
                                              startTime: datetime.datetime,
                                              price: float,
                                              startBeforePrice: float,
                                              stopAtPriceIncrease: float) -> datetime.datetime:
        prices, starts = snap.prices, snap.starts
        startHourPrice = self.electricity_price_now(startTime)
        index_now = bisect.bisect_left(starts, checkTime)
        stop_index = bisect.bisect_left(starts, startTime)

        for i, current in enumerate(prices[stop_index: stop_index + EXTEND_START_LOOKAHEAD_SLOTS]):
            original_index = stop_index + i
            next_item = prices[original_index + 1] if original_index < len(prices) - 1 else None
            if next_item is None:
                continue
            if current.start - startTime <= datetime.timedelta(hours=1):
                if (
                    price < startHourPrice - (stopAtPriceIncrease * START_PRICE_INCREASE_FACTOR)
                    and startHourPrice < next_item.value - (stopAtPriceIncrease * NEXT_PRICE_INCREASE_FACTOR)
                ):
                    return next_item.start

        for i, current in enumerate(reversed(prices[index_now:stop_index + 1])):
            original_index = stop_index - i
            prev_item = prices[original_index - 1] if original_index > 0 else None
            if prev_item is None:
                return current.start

            if (
                startHourPrice + startBeforePrice < prev_item.value
                or price + (startBeforePrice * START_BEFORE_PRICE_FACTOR) < prev_item.value
            ):
                return current.start

        return startTime

    def get_lowest_prices(self,
                          checkitem: int = 1,
                          hours: float = 6,
                          min_change: float | None = None
                          ) -> float | None:
        """ Compares the X hour lowest price to a minimum change and retuns the highest price of those two. """

        snap = self._snapshot
        # Nominal slots per day (24 on hourly data) keeps the maths bit-identical to the old
        # todayslength-based formula on normal days while staying correct on 23/25-hour days.
        slots_per_day = snap.slots_per_hour * 24
        hours = int(hours / 24 * slots_per_day)

        apply_min_change = True
        if checkitem <= snap.todayslength - (2 / 24 * slots_per_day):
            price_list = snap.sorted_today
        elif snap.tomorrow_valid:
            price_list = snap.sorted_tomorrow
        else:
            price_list = snap.sorted_today
            apply_min_change = False   # intentional: no min_change applied in this branch

        if not price_list:
            return None
        hours = min(hours, len(price_list) - 1)
        if apply_min_change and min_change is not None:
            if price_list[hours] < price_list[0] + min_change:
                return price_list[0] + min_change
        return price_list[hours]

    def find_times_to_save(self,
                           pricedrop: float,
                           max_continuous_hours: int,
                           on_for_minimum: int,
                           pricedifference_increase: float,
                           reset_continuous_hours: bool,
                           previous_save_hours: list
                           ) -> list[PeakHour]:
        """Finds peak variations in electricity price for saving purposes and returns list with datetime objects;
           'start', 'end' and 'duration' as a timedelta object for how long the electricity has been off. """

        snap = self._snapshot
        if not snap.prices:
            return []
        now = self.ADapi.datetime(aware=True)
        checkTime = now.replace(minute=0, second=0, microsecond=0)
        index_now = bisect.bisect_left(snap.starts, checkTime)

        saving_hours_list: list = []
        continuous_hours_from_old_calc = 0
        # Slots -> hours. Paired with the reverse scaling in get_lowest_prices (intentional).
        on_for_minimum = ((on_for_minimum) / (snap.slots_per_hour * 24)) * 24

        if previous_save_hours:
            saving_hours_list, continuous_hours_from_old_calc = self._keep_already_calculated_save_hours(
                snap=snap,
                checkTime=checkTime,
                previous_save_hours=previous_save_hours,
                reset_continuous_hours=reset_continuous_hours,
                max_continuous_hours=max_continuous_hours,
                on_for_minimum=on_for_minimum
            )
        saving_hours_list = self._find_peak_hours(
            snap=snap,
            index_now=index_now,
            pricedrop=pricedrop,
            saving_hours_list=saving_hours_list
        )

        if saving_hours_list:
            saving_hours_list = self._remove_save_hours_too_low(
                snap=snap,
                index_now=index_now,
                saving_hours_list=saving_hours_list,
                on_for_minimum=on_for_minimum,
                pricedrop=pricedrop
            )

            saving_hours_list = self._calculate_save_hours(
                snap=snap,
                index_now=index_now,
                pricedrop=pricedrop,
                max_continuous_hours=max_continuous_hours,
                continuous_hours_from_old_calc=continuous_hours_from_old_calc,
                on_for_minimum=on_for_minimum,
                pricedifference_increase=pricedifference_increase,
                saving_hours_list=saving_hours_list,
                reset_continuous_hours=reset_continuous_hours
            )
            return self._putPeaksInOrder(snap, saving_hours_list)
        else:
            return []

    def find_times_to_spend(self,
                            priceincrease: float
                            ) -> list[PeakHour]:
        """ Finds low price variations in electricity price for spending purposes.
            Returns list with datetime objects. """

        snap = self._snapshot
        prices = snap.prices
        now = self.ADapi.datetime(aware=True)
        checkTime = now.replace(minute=0, second=0, microsecond=0)

        index_now = bisect.bisect_left(snap.starts, checkTime)
        low_priced_items = []

        for i, current in enumerate(prices[index_now:-2]):
            original_index = index_now + i
            prev_item = prices[original_index - 1] if original_index > 0 else None
            next_item = prices[original_index + 1]
            # Checks if price increases more than wanted peak difference
            if (
                next_item.value - current.value >= priceincrease
                and current.value <= self.get_lowest_prices(checkitem=original_index, hours=SPEND_LOWEST_HOURS, min_change=None)
            ):
                if not current.start in low_priced_items:
                    low_priced_items.append(current.start)
                if (
                    prev_item is not None
                    and prev_item.value < current.value
                    and not prev_item.start in low_priced_items
                ):
                    low_priced_items.append(prev_item.start)
            # Checks if price increases x1,4 peak difference during two hours
            elif (
                prev_item is not None
                and next_item.value - current.value >= (priceincrease * SPEND_SHORT_INCREASE_FACTOR)
                and next_item.value - prev_item.value >= (priceincrease * SPEND_TWO_HOUR_INCREASE_FACTOR)
                and prev_item.value <= self.get_lowest_prices(checkitem=original_index, hours=SPEND_LOWEST_HOURS, min_change=None)
                and not prev_item.start in low_priced_items
            ):
                low_priced_items.append(prev_item.start)

        return self._putPeaksInOrder(snap, low_priced_items)

    def electricity_price_now(self, time: datetime.datetime | None = None) -> float | None:
        """ Return current complete electricity price based on now or time given. """

        snap = self._snapshot
        if time is None:
            time = self.ADapi.datetime(aware=True)
        index = bisect.bisect_right(snap.starts, time) - 1
        if index >= 0 and time < snap.ends[index]:
            return snap.prices[index].value
        return None

    def print_peaks(self,
                    saving_hours_list: list[PeakHour] | None = None
                    ) -> str:
        """ Formats save and spend list to readable string for easy logging/testing of settings. """

        if saving_hours_list is None:
            saving_hours_list = []
        now = self.ADapi.datetime(aware=True)
        tomorrow_date = (now + datetime.timedelta(days=1)).date()
        four_p_m = datetime.datetime.combine(
            tomorrow_date,
            datetime.time(PRINT_PEAKS_UNTIL_HOUR, 0, 0),
            tzinfo=now.tzinfo
        )
        print_saving_hours_list: str = '\n'
        for item in saving_hours_list:
            if item.start > four_p_m:
                break
            print_saving_hours_list += f"Start at {item.start} until {item.end}. Duration {item.duration}.\n"
        return print_saving_hours_list

    # ------------------------------------------------------------------
    # Save/spend helpers. All take the snapshot they work on explicitly.
    # ------------------------------------------------------------------
    def _putPeaksInOrder(self, snap: _PriceSnapshot, saving_hours_list: list) -> list[PeakHour]:
        peak_list: list[PeakHour] = []
        continue_from_peak = False
        wanted = set(saving_hours_list)

        for current in snap.prices:
            if current.start in wanted:
                if not continue_from_peak:
                    start_of_peak = current.start
                continue_from_peak = True

            elif continue_from_peak:
                continue_from_peak = False
                peak_list.append(PeakHour(
                    start=start_of_peak,
                    end=current.start,
                    duration=current.start - start_of_peak
                ))

        return peak_list

    def _keep_already_calculated_save_hours(self,
                                            snap: _PriceSnapshot,
                                            checkTime: datetime.datetime,
                                            previous_save_hours: list,
                                            reset_continuous_hours: bool,
                                            max_continuous_hours: int,
                                            on_for_minimum: float
                                            ) -> tuple[list, int]:
        prices, starts, ends = snap.prices, snap.starts, snap.ends
        saving_hours_list = []
        continuous_hours_from_old_calc = 0
        continuous_hours_int = 0

        for item in previous_save_hours:
            if item.start > checkTime:
                if (
                    continuous_hours_int > 0
                    and continuous_hours_from_old_calc > 0
                ):
                    continuous_hours_from_old_calc -= self._calc_remove_hours_after_last_peak(
                        current_time=checkTime,
                        last_end_of_peak=start_of_peak,   # intentional: measured from the start of the last peak
                        continuous_hours_int=continuous_hours_int,
                        max_continuous_hours=max_continuous_hours,
                        on_for_minimum=on_for_minimum)

                    if continuous_hours_from_old_calc < 0:
                        continuous_hours_from_old_calc = 0
                return saving_hours_list, math.ceil(continuous_hours_from_old_calc)
            else:
                index_now = bisect.bisect_left(starts, item.start)

                # Find previous continuous time and remove.
                if (
                    continuous_hours_int > 0
                    and continuous_hours_from_old_calc > 0
                ):
                    continuous_hours_from_old_calc -= self._calc_remove_hours_after_last_peak(
                        current_time=item.start,
                        last_end_of_peak=end_of_last_peak,
                        continuous_hours_int=continuous_hours_int,
                        max_continuous_hours=max_continuous_hours,
                        on_for_minimum=on_for_minimum)

                    if continuous_hours_from_old_calc < 0:
                        continuous_hours_from_old_calc = 0

                # Calculate new peak time.
                start_of_peak = item.start
                end_of_last_peak = item.end
                if item.end > checkTime:
                    end_of_peak = checkTime
                    index_end = bisect.bisect_right(ends, checkTime)

                    for current in prices[index_now:index_end]:
                        saving_hours_list.append(current.start)
                    if not reset_continuous_hours:
                        continuous_hours_int = self._td_hours(end_of_peak - start_of_peak)
                        continuous_hours_from_old_calc += continuous_hours_int
                    return saving_hours_list, math.ceil(continuous_hours_from_old_calc)

                else:
                    index_end = bisect.bisect_right(ends, item.end)
                    end_of_peak = item.end

                    for current in prices[index_now:index_end]:
                        saving_hours_list.append(current.start)

                    if not reset_continuous_hours:
                        continuous_hours_int = self._td_hours(end_of_peak - start_of_peak)
                        continuous_hours_from_old_calc += continuous_hours_int
                    else:
                        continuous_hours_from_old_calc = 0

        if end_of_last_peak < checkTime:
            if (
                continuous_hours_int > 0
                and continuous_hours_from_old_calc > 0
            ):
                continuous_hours_from_old_calc -= self._calc_remove_hours_after_last_peak(
                    current_time=checkTime,
                    last_end_of_peak=end_of_last_peak,
                    continuous_hours_int=continuous_hours_int,
                    max_continuous_hours=max_continuous_hours,
                    on_for_minimum=on_for_minimum)

                if continuous_hours_from_old_calc < 0:
                    continuous_hours_from_old_calc = 0

        return saving_hours_list, math.ceil(continuous_hours_from_old_calc)

    def _calc_remove_hours_after_last_peak(self,
                                           current_time: datetime.datetime,
                                           last_end_of_peak: datetime.datetime,
                                           continuous_hours_int: float,
                                           max_continuous_hours: int,
                                           on_for_minimum: float) -> float:
        time_since_last_peak_int = self._td_hours(current_time - last_end_of_peak)
        difference = max_continuous_hours - continuous_hours_int
        return (difference / on_for_minimum) * time_since_last_peak_int

    def _find_peak_hours(self,
                         snap: _PriceSnapshot,
                         index_now: int,
                         pricedrop: float,
                         saving_hours_list: list
                         ) -> list:
        prices = snap.prices
        for i, current in enumerate(prices[index_now:-1]):
            original_index = index_now + i
            prev_item = prices[original_index - 1] if original_index > 0 else None
            next_item = prices[original_index + 1]

            # If price drops more than wanted peak difference
            if current.value - next_item.value >= pricedrop and current.start not in saving_hours_list:
                saving_hours_list.append(current.start)
            # If price drops during 2 hours
            elif prev_item is not None:
                if prev_item.value - next_item.value >= pricedrop * PEAK_TWO_HOUR_DROP_FACTOR and prev_item.start not in saving_hours_list:
                    saving_hours_list.append(prev_item.start)

        return saving_hours_list

    def _determine_stop_calculating_at(self,
                                       snap: _PriceSnapshot,
                                       saving_hours_list: list
                                       ) -> tuple[int, float, datetime.datetime]:
        prices = snap.prices
        stop_calculating_at = int(STOP_CALCULATING_AFTER_HOURS / 24 * (snap.slots_per_hour * 24))
        after_peak_price = AFTER_PEAK_PRICE_DEFAULT
        last_peak_end_time = prices[0].start
        calculate_from = len(prices)
        for i, current in enumerate(reversed(prices)):
            if current.start in saving_hours_list:
                last_peak_end_time = current.end
                original_index = len(prices) - i - 1
                after_peak_price = float(prices[original_index + 1].value)
                calculate_from -= i
                break

        stop_calculating_at = (
            snap.todayslength if len(prices) == snap.todayslength else
            min(stop_calculating_at, calculate_from)
        )
        return stop_calculating_at, after_peak_price, last_peak_end_time

    def _remove_save_hours_too_low(self,
                                   snap: _PriceSnapshot,
                                   index_now: int,
                                   saving_hours_list: list,
                                   on_for_minimum: float,
                                   pricedrop: float
                                   ) -> list:
        prices = snap.prices
        for i, current in enumerate(prices[index_now:-2]):
            if current.start in saving_hours_list:
                original_index = index_now + i
                prev_item = prices[original_index - 1] if original_index > 0 else None
                next_item = prices[original_index + 1]
                if (
                    current.value < self.get_lowest_prices(checkitem=original_index, hours=on_for_minimum, min_change=pricedrop)
                    or (prev_item is not None and prev_item.value < next_item.value)
                ):
                    saving_hours_list.remove(current.start)

        return saving_hours_list

    def _calculate_save_hours(self,
                              snap: _PriceSnapshot,
                              index_now: int,
                              pricedrop: float,
                              max_continuous_hours: int,
                              continuous_hours_from_old_calc: int,
                              on_for_minimum: float,
                              pricedifference_increase: float,
                              saving_hours_list: list,
                              reset_continuous_hours: bool
                              ) -> list:
        prices = snap.prices
        slots_per_day = snap.slots_per_hour * 24
        continuous_hours = datetime.timedelta(0)
        peakdiff = pricedrop
        current_max_continuous_hours = max_continuous_hours

        stop_calculating_at, after_peak_price, last_peak_end_time = self._determine_stop_calculating_at(
            snap=snap, saving_hours_list=saving_hours_list)
        continue_from_peak = False
        continuous_hours_int: float = 0
        # Per-slot increase derived from the per-hour setting (old formula shape, nominal slots/day).
        pricedifference_increase = ((pricedifference_increase - 1) / slots_per_day) * 24 + 1

        check_index_now = stop_calculating_at - index_now - 1

        for i, current in enumerate(reversed(prices[index_now:stop_calculating_at])):
            if current.start in saving_hours_list:
                if not continue_from_peak:
                    last_peak_end_time = current.end
                    original_index = stop_calculating_at - i - 1
                    after_peak_price = float(prices[original_index + 1].value)
                continuous_hours = last_peak_end_time - current.start
                continue_from_peak = True
            elif current.value > after_peak_price + peakdiff and continue_from_peak:
                # Price is higher than peakdiff. Add to save
                peakdiff *= pricedifference_increase  # Adds a x% increase in price difference per hour saving.
                continuous_hours = last_peak_end_time - current.start
                if current.start not in saving_hours_list:
                    saving_hours_list.append(current.start)
            elif continuous_hours > datetime.timedelta(0) or continue_from_peak:
                # If no peak/save found; reset
                continue_from_peak = False
                saving_hours_list, last_peak_end_time, continuous_hours_int = self._calculate_continuous_hours(
                    snap=snap,
                    saving_hours_list=saving_hours_list,
                    max_continuous_hours=current_max_continuous_hours,
                    continuous_hours=continuous_hours,
                    continuous_hours_int=continuous_hours_int,
                    last_peak_end_time=last_peak_end_time,
                    pricedrop=pricedrop,
                    pricedifference_increase=pricedifference_increase,
                    reset_continuous_hours=reset_continuous_hours
                )
                continuous_hours = datetime.timedelta(0)
                peakdiff = pricedrop

            if continuous_hours_int > 0:
                difference = max_continuous_hours - continuous_hours_int
                remove = (difference / on_for_minimum) / slots_per_day * 24
                continuous_hours_int -= remove

            if current_max_continuous_hours < max_continuous_hours:
                normal_on_timedelta = self._td_hours(last_peak_end_time - current.start)
                current_max_continuous_hours += math.ceil(normal_on_timedelta / on_for_minimum)
            elif current_max_continuous_hours > max_continuous_hours:
                current_max_continuous_hours = max_continuous_hours

            if i == check_index_now and continue_from_peak:
                continuous_hours += datetime.timedelta(hours=continuous_hours_from_old_calc)
                saving_hours_list, last_peak_end_time, continuous_hours_int = self._calculate_continuous_hours(
                    snap=snap,
                    saving_hours_list=saving_hours_list,
                    max_continuous_hours=current_max_continuous_hours,
                    continuous_hours=continuous_hours,
                    continuous_hours_int=math.ceil(continuous_hours_int),
                    last_peak_end_time=last_peak_end_time,
                    pricedrop=pricedrop,
                    pricedifference_increase=pricedifference_increase,
                    reset_continuous_hours=reset_continuous_hours
                )

        return saving_hours_list

    def _calculate_continuous_hours(self,
                                    snap: _PriceSnapshot,
                                    saving_hours_list: list,
                                    max_continuous_hours: int,
                                    continuous_hours: datetime.timedelta,
                                    continuous_hours_int: float,
                                    last_peak_end_time: datetime.datetime,
                                    pricedrop: float,
                                    pricedifference_increase: float,
                                    reset_continuous_hours: bool
                                    ) -> tuple[list, datetime.datetime, float]:
        continuous_hours_int += int(math.floor(self._td_hours(continuous_hours)))
        peak_list = self._putPeaksInOrder(snap, saving_hours_list)
        for item in peak_list:
            continuous_hours_from_list_int = int(math.floor(self._td_hours(item.end - item.start)))
            if continuous_hours_from_list_int > continuous_hours_int:
                continuous_hours_from_list_int = continuous_hours_int

            if continuous_hours_from_list_int > max_continuous_hours:
                continuous_hours_to_remove = continuous_hours_from_list_int - max_continuous_hours
                saving_hours_list, last_peak_end_time = self._remove_too_many_continous_hours(
                    snap=snap,
                    saving_hours_list=saving_hours_list,
                    continuous_hours_to_remove=continuous_hours_to_remove,
                    start_peak_time=item.start,
                    last_peak_end_time=item.end,
                    pricedrop=pricedrop,
                    pricedifference_increase=pricedifference_increase,
                    reset_continuous_hours=reset_continuous_hours
                )
                continuous_hours_int -= continuous_hours_to_remove

        return saving_hours_list, last_peak_end_time, continuous_hours_int

    def _remove_too_many_continous_hours(self,
                                         snap: _PriceSnapshot,
                                         saving_hours_list: list,
                                         continuous_hours_to_remove: float,
                                         start_peak_time: datetime.datetime,
                                         last_peak_end_time: datetime.datetime,
                                         pricedrop: float,
                                         pricedifference_increase: float,
                                         reset_continuous_hours: bool
                                         ) -> tuple[list, datetime.datetime]:
        prices, starts, ends = snap.prices, snap.starts, snap.ends
        last_index = len(prices) - 1

        index_start = bisect.bisect_left(starts, start_peak_time)
        index_end = bisect.bisect_right(ends, last_peak_end_time)
        continuous_items_to_remove = int((continuous_hours_to_remove / 24 * (snap.slots_per_hour * 24)))

        # Find the least expencive hour in peak_hour.
        # index_end equals len(prices) when the peak ends with the last slot: clamp the read.
        list_with_lower_prices: list = []
        price_start = prices[index_start].value
        price_end = prices[min(index_end, last_index)].value
        for i, current in enumerate(prices[index_start:index_end]):
            if (
                current.value < price_start
                and current.value < price_end
            ):
                original_index = index_start + i
                list_with_lower_prices.append(original_index)

        if list_with_lower_prices:
            sorted_list = sorted(prices[index_start:index_end], key=lambda x: x.value)
            remove_price_below = sorted_list[len(list_with_lower_prices)].value

            index_start_corrected = index_start
            for i, current in enumerate(prices[index_start:index_end]):
                if current.value <= remove_price_below:
                    if current.start in saving_hours_list:
                        saving_hours_list.remove(current.start)
                        continuous_items_to_remove -= 1

                    if i == index_start_corrected - index_start:
                        index_start_corrected += 1
            if (
                continuous_items_to_remove <= 0
                or reset_continuous_hours
            ):
                return saving_hours_list, last_peak_end_time

            for current in reversed(prices[index_start_corrected:index_end]):
                if not current.start in saving_hours_list:
                    index_end -= 1
                    last_peak_end_time = current.start
                else:
                    break
            index_start = index_start_corrected

        while continuous_items_to_remove > 0:
            start_pricedrop: float = self._calculate_difference_over_given_time(
                pricedrop=pricedrop,
                multiplier=pricedifference_increase,
                iterations=index_end - index_start
            )
            end_item = prices[min(index_end, last_index)]
            if prices[index_start].value > end_item.value + start_pricedrop:
                if end_item.start in saving_hours_list:
                    saving_hours_list.remove(end_item.start)
                    last_peak_end_time = end_item.start
                    continuous_items_to_remove -= 1
                index_end -= 1
            else:
                if prices[index_start].start in saving_hours_list:
                    saving_hours_list.remove(prices[index_start].start)
                    continuous_items_to_remove -= 1
                index_start += 1

            if index_start >= index_end:
                break

        return saving_hours_list, last_peak_end_time

    def _calculate_difference_over_given_time(self,
                                              pricedrop: float,
                                              multiplier: float,
                                              iterations: int
                                              ) -> float:
        return pricedrop * (multiplier ** iterations)

    def _is_holiday(self, date: datetime.date) -> bool:
        return date in self.holidays
