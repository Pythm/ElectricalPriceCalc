""" Electrical Price Calculations

    @Pythm / https://github.com/Pythm
"""

__version__ = "0.1.0"

from appdaemon import adbase as ad
import datetime
import math
import bisect
from nordpool import elspot
from geopy.geocoders import Nominatim
import holidays
from typing import List, Tuple


class ElectricalPriceCalc(ad.ADBase):

    def initialize(self):

        self.ADapi = self.get_ad_api()
        self.HASS_namespace:str = self.args.get('main_namespace', 'default')

        self.country_code = None
        if 'country_code' in self.args:
            self.country_code = self.args['country_code']
        elif 'latitude' in self.config and 'longitude' in self.config:
            try:
                geolocator = Nominatim(user_agent="ElectricalPriceCalc")
                location = geolocator.reverse((self.config['latitude'], self.config['longitude']), language='en')
                self.country_code = location.raw['address'].get('country_code', 'NO')
            except Exception as e:
                self.ADapi.log(f"Failed to get country code from geolocation: {e}", level='ERROR')

        if self.country_code is not None:
            # Initialize holidays for the detected country (default to Sweden if not found)
            try:
                holiday_class = getattr(holidays, self.country_code.upper())
                self.holidays = holiday_class(years=[datetime.date.today().year, datetime.date.today().year + 1])
            except AttributeError:
                self.ADapi.log(f"Could not find holidays for {self.country_code}, defaulting to Norway.", level = 'INFO')
                self.holidays = holidays.Norway(years=[datetime.date.today().year, datetime.date.today().year + 1])

        self.daytax:float = self.args.get('daytax',0)
        self.nighttax:float = self.args.get('nighttax',0)
        self.power_support_above:float = self.args.get('power_support_above', 10)
        self.support_amount:float = self.args.get('support_amount', 0)

        self.nordpool_last_updated = self.ADapi.datetime(aware=True)

        self.elpricestoday:list = []
        self.sorted_elprices_today:list = []
        self.sorted_elprices_tomorrow:list = []
        self.todayslength:int = 0

        if 'pricearea' in self.args:
            self.pricearea = self.args['pricearea']
            self.currency = self.args['currency']
            self.VAT = self.args.get('VAT', 1.25)
            self.prices_spot = elspot.Prices(self.currency)
            self._fetchNordpoolSpotPrices(0)
            self.ADapi.run_daily(self._fetchNordpoolSpotPrices, "00:00:01")
            self.ADapi.run_daily(self._fetchNordpoolSpotPrices, "13:00:00")
            
        elif 'nordpool' in self.args:
            self.nordpool_prices = self.args['nordpool']
            self.currency:str = self.ADapi.get_state(self.nordpool_prices, attribute = 'currency', namespace = self.HASS_namespace)
            self._fetchNordpoolPrices(0)
            self.ADapi.listen_state(self.update_price_rundaily, self.nordpool_prices,
                attribute = 'tomorrow'
            )
        else:
            sensor_states = self.ADapi.get_state()
            for sensor_id, sensor_states in sensor_states.items():
                if 'nordpool' in sensor_id:
                    self.nordpool_prices = sensor_id
                    self.currency:str = self.ADapi.get_state(self.nordpool_prices, attribute = 'currency', namespace = self.HASS_namespace)
                    self._fetchNordpoolPrices(0)
                    self.ADapi.listen_state(self.update_price_rundaily, self.nordpool_prices,
                        attribute = 'tomorrow'
                    )
                    break


    def _fetchNordpoolSpotPrices(self, kwargs) -> None:
        """ Fetches prices from the Nordpool library and adds day and night tax.
        """
        nordpool_todays_prices:list = []
        nordpool_tomorrow_prices:list = []
        try:
            todays_prices = self.prices_spot.fetch(
                # Need to specify end_date to fetch prices for today,
                # as otherwise the library defaults to tomorrow.
                end_date=datetime.date.today(),
                areas=[self.pricearea],
                # Set resolution to 15 minutes, library defaults to 60 minutes.
                resolution=15
            )
        except Exception as e:
            self.ADapi.log(f"Nordpool prices today failed. Exception: {e}", level = 'DEBUG')
            self.ADapi.run_in(self._fetchNordpoolSpotPrices, 1800)
            return
        else:
            nordpool_todays_prices = todays_prices['areas'][self.pricearea]['values']
        try:
            tomorrow_prices = self.prices_spot.fetch(
                areas=[self.pricearea],
                resolution=15
            )
        except Exception as e:
            self.ADapi.log(f"Nordpool prices today failed. Exception: {e}", level = 'DEBUG')
            self.ADapi.run_in(self._fetchNordpoolSpotPrices, 1800)
        else:
            if tomorrow_prices is not None:
                nordpool_tomorrow_prices = tomorrow_prices['areas'][self.pricearea]['values']
            elif self.ADapi.datetime(aware=True) > self.ADapi.parse_datetime('13:00:00', today = True, aware=True):
                self.ADapi.run_in(self._fetchNordpoolSpotPrices, 600)

        self.sorted_elprices_today = []
        self.sorted_elprices_tomorrow = []

        isNotWorkday:bool = self._is_holiday(datetime.date.today())
        beforesix = self.ADapi.parse_datetime("06:00:00", today = True, aware=True)
        aftertwentytwo = self.ADapi.parse_datetime("22:00:00", today = True, aware=True)

        local_tz = datetime.datetime.now().astimezone().tzinfo

        # Todays prices
        for item in nordpool_todays_prices:
            calculated_support:float = 0.0 # Power support calculation
            item['value'] = (float(item['value']) / 1000) * self.VAT # convert price from pr mega to kilo and adds VAT
            item['start'] = item['start'].astimezone(local_tz)
            item['end'] = item['end'].astimezone(local_tz)

            if float(item['value']) > self.power_support_above:
                calculated_support = (float(item['value']) - self.power_support_above ) * self.support_amount

            if (
                item['end'] <= beforesix
                or item['start'] >= aftertwentytwo
                or datetime.datetime.today().weekday() > 4
                or isNotWorkday
            ):
                item['value'] = round(float(item['value']) + self.nighttax - calculated_support, 3)
                self.sorted_elprices_today.append(item['value'])
            else:
                item['value'] = round(float(item['value']) + self.daytax - calculated_support, 3)
                self.sorted_elprices_today.append(item['value'])

        self.sorted_elprices_today = sorted(self.sorted_elprices_today)
        self.todayslength = len(self.sorted_elprices_today)

        # Tomorrows prices if available
        if (
            len(nordpool_tomorrow_prices) > 0
            and nordpool_todays_prices != nordpool_tomorrow_prices
        ):
            isNotWorkday:bool = self._is_holiday(datetime.date.today() + datetime.timedelta(days = 1))
            for item in nordpool_tomorrow_prices:
                calculated_support:float = 0.0 # Power support calculation
                item['value'] = (float(item['value']) / 1000) * self.VAT # convert price from pr mega to kilo and adds VAT
                item['start'] = item['start'].astimezone(local_tz)
                item['end'] = item['end'].astimezone(local_tz)

                if float(item['value']) > self.power_support_above:
                    calculated_support = (float(item['value']) - self.power_support_above ) * self.support_amount

                """ TODO: Does not check if tomorrow is holiday when applying day or night tax to tomorrows prices. 
                """
                if (
                    item['end'] <= beforesix
                    or item['start'] >= aftertwentytwo
                    or datetime.datetime.today().weekday() == 4
                    or datetime.datetime.today().weekday() == 5
                    or isNotWorkday
                ):
                    item['value'] = round(float(item['value']) + self.nighttax - calculated_support, 3)
                    self.sorted_elprices_tomorrow.append(item['value'])
                else:
                    item['value'] = round(float(item['value']) + self.daytax - calculated_support, 3)
                    self.sorted_elprices_tomorrow.append(item['value'])

            self.sorted_elprices_tomorrow = sorted(self.sorted_elprices_tomorrow)
        self.elpricestoday = nordpool_todays_prices + nordpool_tomorrow_prices

    def update_price_rundaily(self, entity, attribute, old, new, kwargs) -> None:
        """ Calls fetchNordpoolPrices() on sensor change.
        """
        self._fetchNordpoolPrices(0)


    def _fetchNordpoolPrices(self, kwargs) -> None:
        """ Fetches prices from Nordpool sensor and adds day and night tax.
        """
        nordpool_todays_prices:list = []
        nordpool_tomorrow_prices:list = []
        self.sorted_elprices_today = []
        self.sorted_elprices_tomorrow = []

        isNotWorkday:bool = self._is_holiday(datetime.date.today())
        beforesix = self.ADapi.parse_datetime("06:00:00", today = True, aware=True)
        aftertwentytwo = self.ADapi.parse_datetime("22:00:00", today = True, aware=True)

        # Todays prices
        try:
            nordpool_todays_prices = self.ADapi.get_state(entity_id = self.nordpool_prices, attribute = 'raw_today')
            for item in nordpool_todays_prices:
                calculated_support:float = 0.0 # Power support calculation
                item['start'] = self.ADapi.convert_utc(item['start'])
                item['end'] = self.ADapi.convert_utc(item['end'])

                if float(item['value']) > self.power_support_above:
                    calculated_support = (float(item['value']) - self.power_support_above ) * self.support_amount

                if (
                    item['end'] <= beforesix
                    or item['start'] >= aftertwentytwo
                    or datetime.datetime.today().weekday() > 4
                    or isNotWorkday
                ):
                    item['value'] = round(float(item['value']) + self.nighttax - calculated_support, 3)
                    self.sorted_elprices_today.append(item['value'])
                else:
                    item['value'] = round(float(item['value']) + self.daytax - calculated_support, 3)
                    self.sorted_elprices_today.append(item['value'])

        except Exception as e:
            self.ADapi.log(f"Nordpool prices today failed. Exception: {e}", level = 'DEBUG')
            self.ADapi.run_in(self._fetchNordpoolPrices, 1800)
            self.sorted_elprices_today = []
        else:
            self.sorted_elprices_today = sorted(self.sorted_elprices_today)

        self.todayslength = len(self.sorted_elprices_today)

        # Tomorrows prices if available
        if self.ADapi.get_state(entity_id = self.nordpool_prices, attribute = 'tomorrow_valid'):
            isNotWorkday:bool = self._is_holiday(datetime.date.today() + datetime.timedelta(days = 1))
            try:
                nordpool_tomorrow_prices = self.ADapi.get_state(entity_id = self.nordpool_prices, attribute = 'raw_tomorrow')
                if (
                    len(nordpool_tomorrow_prices) > 0
                    and nordpool_todays_prices != nordpool_tomorrow_prices
                ):
                    for item in nordpool_tomorrow_prices:
                        calculated_support:float = 0.0 # Power support calculation
                        item['start'] = self.ADapi.convert_utc(item['start'])
                        item['end'] = self.ADapi.convert_utc(item['end'])

                        if float(item['value']) > self.power_support_above:
                            calculated_support = (float(item['value']) - self.power_support_above ) * self.support_amount

                        """ TODO: Does not check if tomorrow is holiday when applying day or night tax to tomorrows prices. 
                        """
                        if (
                            item['end'] <= beforesix
                            or item['start'] >= aftertwentytwo
                            or datetime.datetime.today().weekday() == 4
                            or datetime.datetime.today().weekday() == 5
                            or isNotWorkday
                        ):
                            item['value'] = round(float(item['value']) + self.nighttax - calculated_support, 3)
                            self.sorted_elprices_tomorrow.append(item['value'])
                        else:
                            item['value'] = round(float(item['value']) + self.daytax - calculated_support, 3)
                            self.sorted_elprices_tomorrow.append(item['value'])

            except IndexError as ie:
                self.ADapi.log(f"Failed to get tomorrows prices. Index Error: {ie}", level = 'WARNING')
            except Exception as e:
                self.ADapi.log(f"Nordpool prices tomorrow failed. Exception: {e}", level = 'WARNING')

            else:
                self.sorted_elprices_tomorrow = sorted(self.sorted_elprices_tomorrow)

        self.elpricestoday = nordpool_todays_prices + nordpool_tomorrow_prices

    def getContinuousCheapestTime(self,
                                  hoursTotal:float,
                                  calculateBeforeNextDayPrices:bool,
                                  finishByHour:int
                                  ) -> Tuple[datetime, datetime, float]:
        """ Returns starttime, endtime and price for cheapest continuous hours with different options depenting on time the call was made.
        """
        indexesToFinish = math.ceil(hoursTotal / 24 * self.todayslength)
        if indexesToFinish == 0:
            indexesToFinish = 1

        finishAt = self.ADapi.datetime(aware=True).replace(hour = 0, minute = 0, second = 0, microsecond = 0) + datetime.timedelta(hours = finishByHour)

        if (
            self.ADapi.now_is_between('13:00:00', '23:59:59')
            and len(self.elpricestoday) > self.todayslength
            or finishAt < self.ADapi.datetime(aware=True)
        ):
            finishAt += datetime.timedelta(days = 1)

        elif (
            self.ADapi.now_is_between('06:00:00', '15:00:00')
            and len(self.elpricestoday) == self.todayslength
        ):
            if not calculateBeforeNextDayPrices:
                return None, None, self.sorted_elprices_today[indexesToFinish]


        priceToComplete:float = 0.0
        avgPriceToComplete:float = 1000.0

        checkTime = self.ADapi.datetime(aware=True).replace(minute = 0, second = 0, microsecond = 0)
        start_times = [item['start'] for item in self.elpricestoday]
        end_times = [item['end'] for item in self.elpricestoday]

        index_start = bisect.bisect_left(start_times, checkTime)
        index_end = bisect.bisect_right(end_times, finishAt)
        index_end -= indexesToFinish

        startTime = None
        endTime = None

        if index_start < index_end:
            while index_start <= index_end:
                for item in self.elpricestoday[index_start:index_start + indexesToFinish]:
                    priceToComplete += item['value']
                if priceToComplete < avgPriceToComplete:
                    avgPriceToComplete = priceToComplete
                    startTime = self.elpricestoday[index_start]['start']
                    endTime = self.elpricestoday[index_start+indexesToFinish-1]['end']

                priceToComplete = 0.0
                index_start += 1
        else:
            for item in self.elpricestoday[index_start:index_start + indexesToFinish]:
                priceToComplete += item['value']
            startTime = self.elpricestoday[index_start]['start']
            endTime = self.elpricestoday[index_start+indexesToFinish-1]['end']
            avgPriceToComplete = priceToComplete

        return startTime, endTime, round(avgPriceToComplete/indexesToFinish, 3)

    def get_lowest_prices(self,
                          checkitem:int = 1,
                          hours:int = 6,
                          min_change:float = 0.1
                          ) -> float:
        """ Compares the X hour lowest price to a minimum change and retuns the highest price of those two.
        """
        hours = int(hours / 24 * self.todayslength)
        if checkitem <= self.todayslength - (2 / 24 * self.todayslength):
            if min_change is not None:
                if self.sorted_elprices_today[hours] < self.sorted_elprices_today[0] + min_change:
                    return self.sorted_elprices_today[0] + min_change
            return self.sorted_elprices_today[hours]
        else:
            if min_change is not None:
                if self.sorted_elprices_tomorrow[hours] < self.sorted_elprices_tomorrow[0] + min_change:
                    return self.sorted_elprices_tomorrow[0] + min_change
        return self.sorted_elprices_tomorrow[hours]

    def findpeakhours(self,
                      pricedrop: float,
                      max_continuous_hours: int,
                      on_for_minimum: int,
                      pricedifference_increase: float,
                      reset_continuous_hours: bool,
                      prev_peak_hours: list
                      ) -> Tuple[datetime.timedelta, datetime, list]:
        """Finds peak variations in electricity price for saving purposes and returns list with datetime objects,
           int with continuous hours off, and int with hour turned back on for when to save.
        """
        checkTime = self.ADapi.datetime(aware=True).replace(minute=0, second=0, microsecond=0)
        start_times = [item['start'] for item in self.elpricestoday]
        #end_times = [item['end'] for item in self.elpricestoday] ###

        index_now = bisect.bisect_left(start_times, checkTime)
        #index_end = bisect.bisect_right(end_times, finishAt) ###
        peak_hours:list = []
        continuous_hours_from_old_calc = 0

        if (
            len(self.elpricestoday) > self.todayslength
            and prev_peak_hours
        ):
            peak_hours, continuous_hours_from_old_calc = self._keep_already_calculated_save_hours(
                index_now = index_now,
                prev_peak_hours = prev_peak_hours,
                reset_continuous_hours = reset_continuous_hours,
                continuous_hours_from_old_calc = continuous_hours_from_old_calc,
                max_continuous_hours = max_continuous_hours,
                on_for_minimum = on_for_minimum
            )
        peak_hours = self._find_peak_hours(
            index_now = index_now,
            pricedrop = pricedrop,
            peak_hours = peak_hours
        )

        if peak_hours:
            peak_hours = self._remove_save_hours_too_low(
                index_now = index_now,
                peak_hours = peak_hours,
                on_for_minimum = on_for_minimum,
                pricedrop = pricedrop
            )

            highest_continuous_hours, turn_on_at, peak_hours = self._calculate_save_hours(
                index_now = index_now,
                pricedrop = pricedrop,
                max_continuous_hours = max_continuous_hours,
                continuous_hours_from_old_calc = continuous_hours_from_old_calc,
                on_for_minimum = on_for_minimum,
                pricedifference_increase = pricedifference_increase,
                peak_hours = peak_hours,
                reset_continuous_hours = reset_continuous_hours
            )
            return highest_continuous_hours, turn_on_at, peak_hours
        else:
            return datetime.timedelta(0), None, []

    def findLowPriceHours(self,
                          priceincrease:float
                          ) -> list:
        """ Finds low price variations in electricity price for spending purposes and returns list with datetime objects.
        """
        checkTime = self.ADapi.datetime(aware=True).replace(minute=0, second=0, microsecond=0)
        start_times = [item['start'] for item in self.elpricestoday]

        index_now = bisect.bisect_left(start_times, checkTime)
        low_priced_items = []

        for i, current in enumerate(self.elpricestoday[index_now:-2]):
            original_index = index_now + i
            prev_item = self.elpricestoday[original_index - 1] if original_index > 0 else None
            next_item = self.elpricestoday[original_index + 1] if original_index < len(self.elpricestoday) - 1 else None
                # Checks if price increases more than wanted peak difference
            if (
                next_item['value'] - current['value'] >= priceincrease
                and current['value'] <= self.get_lowest_prices(checkitem = original_index, hours = 3, min_change = None)
            ):
                if not current['start'] in low_priced_items:
                    low_priced_items.append(current['start'])
                if (
                    prev_item['value'] < current['value']
                    and not prev_item['start'] in low_priced_items
                ):
                    low_priced_items.append(prev_item['start'])
                # Checks if price increases x1,4 peak difference during two hours
            elif (
                next_item['value'] - current['value'] >= (priceincrease * 0.6)
                and next_item['value'] - prev_item['value'] >= (priceincrease * 1.4)
                and prev_item['value'] <= self.get_lowest_prices(checkitem = original_index, hours = 3, min_change = 0.1)
                and not prev_item['start'] in low_priced_items
            ):
                low_priced_items.append(prev_item['start'])

        return low_priced_items

    def print_peaks(self,
                    peak_hours:list = []
                    ) -> None:
        """ Formats save and spend list to readable string for easy logging/testing of settings.
        """
        print_peak_hours:str = ''
        if peak_hours:
            for i, current in enumerate(self.elpricestoday):
                if current['start'] in peak_hours:
                    prev_item = self.elpricestoday[i - 1] if i > 0 else None
                    next_item = self.elpricestoday[i + 1] if i < len(self.elpricestoday) - 1 else None
                    if (
                        prev_item is not None
                        and next_item is not None
                    ):
                        if (
                            prev_item['start'] in peak_hours
                            and next_item['start'] in peak_hours
                        ):
                            continue

                        print_peak_hours += str(f"{self.currency} {current['value']} at {current['start']}")
                        if (
                            not prev_item['start'] in peak_hours
                            and next_item['start'] in peak_hours
                        ):
                            print_peak_hours += " until "
                        elif (
                            prev_item['start'] in peak_hours
                            and not next_item['start'] in peak_hours
                        ):
                            print_peak_hours += str(
                                f". Goes back to normal with {self.currency} {next_item['value']} at {current['end']}. "
                            )
                        else:
                            print_peak_hours += ". "
        return print_peak_hours

    def _keep_already_calculated_save_hours(self,
                                            index_now,
                                            prev_peak_hours,
                                            reset_continuous_hours,
                                            continuous_hours_from_old_calc,
                                            max_continuous_hours,
                                            on_for_minimum
                                            ):
        peak_hours = []
        continue_from_peak = False
        continuous_hours_from_old_calc = 0
        continuous_hours_int = 0

        for i, current in enumerate(self.elpricestoday[:index_now]):
            if current['start'] in [item for item in prev_peak_hours]:
                self.ADapi.log(f"Found peak in previous: {current['start']}") ###
                peak_hours.append(current['start'])
                if not continue_from_peak:
                    start_of_peak = current['start']
                continue_from_peak = True

            elif not reset_continuous_hours:
                if continue_from_peak:
                    continuous_hours = current['start'] -start_of_peak
                    continue_from_peak = False
                    continuous_hours_int = (continuous_hours.days * 24 * 60 + continuous_hours.seconds // 60) / 60
                    continuous_hours_from_old_calc += continuous_hours_int
                    self.ADapi.log(f"Cont from old #1: {continuous_hours_from_old_calc}") ###
                elif (
                    continuous_hours_int > 0
                    and continuous_hours_from_old_calc > 0
                ):
                    difference = max_continuous_hours - continuous_hours_int
                    remove = difference / on_for_minimum
                    continuous_hours_from_old_calc -= remove
                    self.ADapi.log(f"Cont from old #2: {continuous_hours_from_old_calc}. Removed: {remove}") ###
                else:
                    continuous_hours_from_old_calc = 0
            else:
                continuous_hours_from_old_calc = 0

        self.ADapi.log(f"Prev peak cont form old: {continuous_hours_from_old_calc}") ###
        return peak_hours, math.ceil(continuous_hours_from_old_calc)

    def _find_peak_hours(self,
                         index_now,
                         pricedrop,
                         peak_hours
                         ):
        for i, current in enumerate(self.elpricestoday[index_now:-1]):
            original_index = index_now + i
            prev_item = self.elpricestoday[original_index - 1] if original_index > 0 else None
            next_item = self.elpricestoday[original_index + 1] if original_index < len(self.elpricestoday) - 1 else None

            # If price drops more than wanted peak difference
            if current['value'] - next_item['value'] >= pricedrop and current['start'] not in peak_hours:
                peak_hours.append(current['start'])
            # If price drops during 2 hours
            elif prev_item is not None:
                if prev_item['value'] - next_item['value'] >= pricedrop * 1.3 and prev_item['start'] not in peak_hours:
                    peak_hours.append(prev_item['start'])

        return peak_hours

    def _determine_stop_calculating_at(self, peak_hours):
        stop_calculating_at = int(40 / 24 * self.todayslength)
        after_peak_price = 100
        last_peak_end_time = self.elpricestoday[0]['start']
        calculate_from = len(self.elpricestoday)
        for i, current in enumerate(reversed(self.elpricestoday)):
            if i < len(self.elpricestoday):
                if current['start'] in peak_hours:
                    last_peak_end_time = current['end']
                    original_index = len(self.elpricestoday) - i -1
                    after_peak_price = float(self.elpricestoday[original_index +1]['value'])
                    calculate_from -= i
                    break

        stop_calculating_at = (
            self.todayslength if len(self.elpricestoday) == self.todayslength else
            min(stop_calculating_at, calculate_from)
        )
        return stop_calculating_at, after_peak_price, last_peak_end_time

    def _remove_save_hours_too_low(self,
                                   index_now,
                                   peak_hours,
                                   on_for_minimum,
                                   pricedrop
                                   ):
        for i, current in enumerate(self.elpricestoday[index_now:-2]):
            if current['start'] in peak_hours:
                original_index = index_now + i
                prev_item = self.elpricestoday[original_index-1]
                next_item = self.elpricestoday[original_index+1]
                if (
                    current['value'] < self.get_lowest_prices(checkitem = original_index, hours = on_for_minimum, min_change = pricedrop)
                    or prev_item['value'] < next_item['value']
                ):
                    peak_hours.remove(current['start'])

        return peak_hours

    def _calculate_save_hours(self,
                              index_now,
                              pricedrop,
                              max_continuous_hours,
                              continuous_hours_from_old_calc,
                              on_for_minimum,
                              pricedifference_increase,
                              peak_hours,
                              reset_continuous_hours
                              ):
        continuous_hours = datetime.timedelta(0)
        highest_continuous_hours = datetime.timedelta(0)
        turn_on_at = None
        peakdiff = pricedrop
        current_max_continuous_hours = max_continuous_hours

        stop_calculating_at, after_peak_price, last_peak_end_time = self._determine_stop_calculating_at(peak_hours = peak_hours)
        continue_from_peak = False
        continuous_hours_int:float = 0

        check_index_now = stop_calculating_at - index_now -1

        for i, current in enumerate(reversed(self.elpricestoday[index_now:stop_calculating_at])):
            if current['start'] in peak_hours:
                if not continue_from_peak:
                    last_peak_end_time = current['end']
                    original_index = stop_calculating_at - i -1
                    after_peak_price = float(self.elpricestoday[original_index +1]['value'])
                continuous_hours = last_peak_end_time - current['start']
                continue_from_peak = True
            elif current['value'] > after_peak_price + peakdiff and continue_from_peak:
                # Price is higher that peakdiff. Add to save
                peakdiff *= pricedifference_increase  # Adds a x% increase in price difference per hour saving.
                continuous_hours = last_peak_end_time - current['start']
                if current['start'] not in peak_hours:
                    peak_hours.append(current['start'])
            elif continuous_hours > datetime.timedelta(0) or continue_from_peak:
                # If no peak/save found; reset
                continue_from_peak = False
                peak_hours, last_peak_end_time, continuous_hours_int = self._calculate_continuous_hours(
                    peak_hours = peak_hours,
                    max_continuous_hours = current_max_continuous_hours,
                    on_for_minimum = on_for_minimum,
                    continuous_hours = continuous_hours,
                    continuous_hours_int = continuous_hours_int,
                    start_peak_time = current['start'],
                    last_peak_end_time = last_peak_end_time,
                    pricedrop = pricedrop,
                    pricedifference_increase = pricedifference_increase,
                    reset_continuous_hours = reset_continuous_hours
                )

                if current['start'].date() == self.ADapi.datetime(aware=True).date():
                    if continuous_hours > datetime.timedelta(hours = max_continuous_hours):
                        continuous_hours = datetime.timedelta(hours = max_continuous_hours)
                    if highest_continuous_hours < continuous_hours:
                        highest_continuous_hours = continuous_hours
                        turn_on_at = last_peak_end_time

                continuous_hours = datetime.timedelta(0)
                peakdiff = pricedrop
            elif continue_from_peak: ###
                self.ADapi.log(f"Found no entry in calculating peak hours in: {stop_calculating_at - i -1}. Continue should not be true?") ###
                continue_from_peak = False ###

            if continuous_hours_int > 0:
                difference = max_continuous_hours - continuous_hours_int
                remove = (difference / on_for_minimum) / self.todayslength * 24
                #self.ADapi.log(f"Removing {remove} in {stop_calculating_at - i -1} from cont: {continuous_hours_int}") ###
                continuous_hours_int -= remove

            if current_max_continuous_hours < max_continuous_hours:
                td = last_peak_end_time - current['start']
                normal_on_timedelta = (td.days * 24 * 60 + td.seconds // 60) / 60
                current_max_continuous_hours += math.ceil(normal_on_timedelta / on_for_minimum)
                self.ADapi.log(f"Current max cont hours: {current_max_continuous_hours}") ###
            elif current_max_continuous_hours > max_continuous_hours:
                current_max_continuous_hours = max_continuous_hours

            if i == check_index_now and continue_from_peak:
                self.ADapi.log(f"Adding old cont {continuous_hours_from_old_calc} to {continuous_hours}") ###
                continuous_hours += datetime.timedelta(hours = continuous_hours_from_old_calc)
                peak_hours, last_peak_end_time, continuous_hours_int = self._calculate_continuous_hours(
                    peak_hours = peak_hours,
                    max_continuous_hours = current_max_continuous_hours,
                    on_for_minimum = on_for_minimum,
                    continuous_hours = continuous_hours,
                    continuous_hours_int = math.ceil(continuous_hours_int),
                    start_peak_time = current['start'],
                    last_peak_end_time = last_peak_end_time,
                    pricedrop = pricedrop,
                    pricedifference_increase = pricedifference_increase,
                    reset_continuous_hours = reset_continuous_hours
                )

                if current['start'].date() == self.ADapi.datetime(aware=True).date():
                    if continuous_hours > datetime.timedelta(hours = max_continuous_hours):
                        continuous_hours = datetime.timedelta(hours = max_continuous_hours)
                    if highest_continuous_hours < continuous_hours:
                        highest_continuous_hours = continuous_hours
                        turn_on_at = last_peak_end_time

        return highest_continuous_hours, turn_on_at, peak_hours

    def _calculate_continuous_hours(self,
                                    peak_hours,
                                    max_continuous_hours,
                                    on_for_minimum,
                                    continuous_hours,
                                    continuous_hours_int,
                                    start_peak_time,
                                    last_peak_end_time,
                                    pricedrop,
                                    pricedifference_increase,
                                    reset_continuous_hours
                                    ):
        continuous_hours_int += (continuous_hours.days * 24 * 60 + continuous_hours.seconds // 60) / 60

        if continuous_hours_int > max_continuous_hours:
            self.ADapi.log(f"Start to remove hours from list: Continuous is > max: {continuous_hours_int}") ###
            continuous_hours_to_remove = continuous_hours_int - max_continuous_hours
            peak_hours, last_peak_end_time = self._remove_too_many_continous_hours(
                peak_hours = peak_hours,
                continuous_hours_to_remove = continuous_hours_to_remove,
                start_peak_time = start_peak_time,
                last_peak_end_time = last_peak_end_time,
                pricedrop = pricedrop,
                pricedifference_increase = pricedifference_increase,
                reset_continuous_hours = reset_continuous_hours
            )
            continuous_hours_int -= continuous_hours_to_remove

        return peak_hours, last_peak_end_time, continuous_hours_int

    def _remove_too_many_continous_hours(self,
                                         peak_hours,
                                         continuous_hours_to_remove,
                                         start_peak_time,
                                         last_peak_end_time,
                                         pricedrop,
                                         pricedifference_increase,
                                         reset_continuous_hours
                                         ):
        start_times = [item['start'] for item in self.elpricestoday]
        end_times = [item['end'] for item in self.elpricestoday]

        index_start = bisect.bisect_left(start_times, start_peak_time)
        index_end = bisect.bisect_right(end_times, last_peak_end_time)
        price_start = self.elpricestoday[index_start]['value']
        price_end = self.elpricestoday[index_end]['value']
        continuous_items_to_remove =  int((continuous_hours_to_remove/24 * self.todayslength))

        was_able_to_remove_in_price_check:bool = False

        # Find the least expencive hour in peak_hour.
        list_with_lower_prices:list = []
        list_with_lower_prices_before_price_increase:list = []
        for i, current in enumerate(self.elpricestoday[index_start:index_end]):
            if (
                current['value'] < price_start
                and current['value'] < price_end
            ):
                list_with_lower_prices.append(i)

        if list_with_lower_prices:
            sorted_list = sorted(self.elpricestoday[index_start:index_end], key=lambda x: x['value'])
            remove_price_below = sorted_list[list_with_lower_prices]

            if len(list_with_lower_prices) > continuous_items_to_remove:
                remove_price_below = sorted_list[continuous_items_to_remove]

            index_start_corrected = index_start
            for i, current in enumerate(self.elpricestoday[index_start:index_end]):
                if current['value'] <= remove_price_below:
                    self.ADapi.log(f"Remove {current['start']} in too many continious hours") ###
                    peak_hours.remove(current['start'])
                    continuous_items_to_remove -= 1
                    if i == index_start_corrected - index_start:
                        index_start_corrected += 1
            if (
                continuous_items_to_remove <= 0 
                or reset_continuous_hours
            ):
                return peak_hours, last_peak_end_time
            
            for current in reversed(self.elpricestoday[index_start_corrected:index_end]):
                if not current['start'] in test_list:
                    index_end -= 1
                    last_peak_end_time = current['start']
                else:
                    break
            index_start = index_start_corrected

        peak_hours, last_peak_end_time = self._remove_first_or_last_peak_hours(
            peak_hours = peak_hours,
            pricedrop = pricedrop,
            pricedifference_increase = pricedifference_increase,
            continuous_items_to_remove = continuous_items_to_remove,
            index_start = index_start,
            index_end = index_end,
            last_peak_end_time = last_peak_end_time
        )
        return peak_hours, last_peak_end_time

    def _remove_first_or_last_peak_hours(self,
                                         peak_hours:list,
                                         pricedrop:float,
                                         pricedifference_increase:float,
                                         continuous_items_to_remove:int,
                                         index_start:int,
                                         index_end:int,
                                         last_peak_end_time:datetime
                                         ):
        while continuous_items_to_remove > 0:
            start_pricedrop:float = self._calculate_difference_over_given_time(
                pricedrop = pricedrop,
                multiplier = pricedifference_increase,
                iterations = index_end - index_start
            )
            if (
                self.elpricestoday[index_start]['value'] + start_pricedrop > self.elpricestoday[index_end]['value'] + pricedrop
            ):
                if self.elpricestoday[index_end]['start'] in peak_hours:
                    peak_hours.remove(self.elpricestoday[index_end]['start'])
                    last_peak_end_time = self.elpricestoday[index_end]['start']
                    continuous_items_to_remove -= 1
                index_end -= 1
            else:
                if self.elpricestoday[index_start]['start'] in peak_hours:
                    peak_hours.remove(self.elpricestoday[index_start]['start'])
                    continuous_items_to_remove -= 1
                index_start += 1
            
            if index_start == index_end:
                self.ADapi.log(f"Removed all hours possible and now end in near: {index_end}") ###
                break

        return peak_hours, last_peak_end_time

    def _calculate_difference_over_given_time(self,
                                              pricedrop: float,
                                              multiplier: float,
                                              iterations: int
                                              ) -> float:
        start_hour_price = pricedrop * (multiplier ** iterations)
        return start_hour_price


    def _is_holiday(self, date):
        return date in self.holidays