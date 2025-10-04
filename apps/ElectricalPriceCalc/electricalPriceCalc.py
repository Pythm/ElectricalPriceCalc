""" Electrical Price Calculations

    @Pythm / https://github.com/Pythm
"""

__version__ = "0.1.3"

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

        # Detect country and initialize holidays
        self.country_code = None
        if 'country_code' in self.args:
            self.country_code = self.args['country_code']
        elif 'latitude' in self.config and 'longitude' in self.config:
            try:
                geolocator = Nominatim(user_agent="ElectricalPriceCalc")
                location = geolocator.reverse((self.config['latitude'], self.config['longitude']), language='en')
                self.country_code = location.raw['address'].get('country_code', 'NO')
                self.ADapi.log(f"Country code set to {self.country_code.upper()} in {self.name}", level = 'INFO')
            except Exception as e:
                self.ADapi.log(f"Failed to get country code from geolocation: {e}", level='ERROR')

        if self.country_code is not None:
            try:
                holiday_class = getattr(holidays, self.country_code.upper())
                self.holidays = holiday_class(years=[datetime.date.today().year, datetime.date.today().year + 1])
            except AttributeError:
                self.ADapi.log(f"Could not find holidays for {self.country_code}, defaulting to Norway.", level = 'INFO')
                self.holidays = holidays.Norway(years=[datetime.date.today().year, datetime.date.today().year + 1])

        # Set up prices and taxes
        self.daytax = self.args.get('daytax',0)
        self.nighttax = self.args.get('nighttax',0)
        self.additional_tax:float = self.args.get('additional_tax',0)
        self.power_support_above:float = self.args.get('power_support_above', 10)
        self.support_amount:float = self.args.get('support_amount', 0)

        self.elpricestoday:list = []
        self.sorted_elprices_today:list = []
        self.sorted_elprices_tomorrow:list = []
        self.todayslength:int = 0
        self.tomorrow_valid = True

        if 'fixedprice' in self.args:
            fixedprice = self.args['fixedprice']
            self.currency = self.args.get('currency', 'EUR')
            self.VAT = self.args.get('VAT', 0)
            if self.ADapi.now_is_between('12:50:00', '23:59:59'):
                self._create_daily_prices_with_taxes(price = fixedprice, tomorrow = True)
            else:
                self._create_daily_prices_with_taxes(price = fixedprice, tomorrow = False)
            self.ADapi.run_daily(self._create_daily_prices_with_taxes, "00:01:00", price = fixedprice, tomorrow = False)
            self.ADapi.run_daily(self._create_daily_prices_with_taxes, "13:00:00", price = fixedprice, tomorrow = True)

        elif 'pricearea' in self.args:
            self.pricearea = self.args['pricearea']
            self.currency = self.args.get('currency', 'EUR')
            self.VAT = self.args.get('VAT', 1.25)
            self.prices_spot = elspot.Prices(self.currency)
            self._fetchNordpoolSpotPrices(0)
            self.ADapi.run_daily(self._fetchNordpoolSpotPrices, "00:01:00")
            self.ADapi.run_daily(self._fetchNordpoolSpotPrices, "13:00:00")
            
        elif 'nordpool' in self.args:
            self.nordpool_prices = self.args['nordpool']
            self._fetchNordpoolPrices(0)
            self.ADapi.listen_state(self._update_price_rundaily, self.nordpool_prices,
                attribute = 'tomorrow'
            )
        else:
            sensor_states = self.ADapi.get_state()
            for sensor_id, sensor_states in sensor_states.items():
                if 'nordpool' in sensor_id:
                    self.nordpool_prices = sensor_id
                    self._fetchNordpoolPrices(0)
                    self.ADapi.listen_state(self._update_price_rundaily, self.nordpool_prices,
                        attribute = 'tomorrow'
                    )
                    break

    def _update_price_rundaily(self, entity, attribute, old, new, kwargs) -> None:
        self._fetchNordpoolPrices(0)

    # Fetch Nordpool prices with elspot
    def _fetchNordpoolSpotPrices(self, kwargs) -> None:
        nordpool_todays_prices:list = []
        nordpool_tomorrow_prices:list = []
        try:
            todays_prices = self.prices_spot.fetch(
                end_date=datetime.date.today(),
                areas=[self.pricearea],
                resolution=15
            )
        except Exception as e:
            self.ADapi.log(f"Nordpool prices today failed. Exception: {e}", level = 'DEBUG')
            self.ADapi.run_in(self._fetchNordpoolSpotPrices, 1800)
            return
        else:
            nordpool_todays_prices = self._correctDictsNordpoolSpotPrices(nordpool_prices = todays_prices['areas'][self.pricearea]['values'])
        try:
            tomorrow_prices = self.prices_spot.fetch(
                areas=[self.pricearea],
                resolution=15
            )
        except Exception as e:
            self.ADapi.log(f"Nordpool prices tomorrow failed. Exception: {e}", level = 'DEBUG')
            self.ADapi.run_in(self._fetchNordpoolSpotPrices, 1800)
        else:
            if tomorrow_prices is not None:
                nordpool_tomorrow_prices = self._correctDictsNordpoolSpotPrices(nordpool_prices = tomorrow_prices['areas'][self.pricearea]['values'])
            elif self.ADapi.datetime(aware=True) > self.ADapi.parse_datetime('13:00:00', today = True, aware=True):
                self.ADapi.run_in(self._fetchNordpoolSpotPrices, 600)
                return

        self._calculatePrices(nordpool_todays_prices = nordpool_todays_prices,
                              nordpool_tomorrow_prices = nordpool_tomorrow_prices)
        
    def _correctDictsNordpoolSpotPrices(self, nordpool_prices):
        local_tz = datetime.datetime.now().astimezone().tzinfo
        for item in nordpool_prices:
            item['value'] = (float(item['value']) / 1000) * self.VAT # convert price from pr mega to kilo and adds VAT
            item['start'] = item['start'].astimezone(local_tz)
            item['end'] = item['end'].astimezone(local_tz)
        return nordpool_prices

    # Fetch Nordpool prices with Home Assistant integration
    def _fetchNordpoolPrices(self, kwargs) -> None:
        nordpool_todays_prices:list = []
        nordpool_tomorrow_prices:list = []

        # Todays prices
        self.currency = self.ADapi.get_state(entity_id = self.nordpool_prices, attribute = 'currency')
        try:
            todays_prices = self.ADapi.get_state(entity_id = self.nordpool_prices, attribute = 'raw_today')
        except Exception as e:
            self.ADapi.log(f"Nordpool prices today failed. Exception: {e}", level = 'DEBUG')
            self.ADapi.run_in(self._fetchNordpoolPrices, 1800)
            return
        else:
            nordpool_todays_prices = self._correctDictsNordpoolIntegrationPrices(nordpool_prices = todays_prices)

        # Tomorrows prices if available
        if self.ADapi.get_state(entity_id = self.nordpool_prices, attribute = 'tomorrow_valid'):
            try:
                tomorrow_prices = self.ADapi.get_state(entity_id = self.nordpool_prices, attribute = 'raw_tomorrow')
            except IndexError as ie:
                self.ADapi.log(f"Failed to get tomorrows prices. Index Error: {ie}", level = 'WARNING')
            except Exception as e:
                self.ADapi.log(f"Nordpool prices tomorrow failed. Exception: {e}", level = 'WARNING')
            else:
                if (
                    len(tomorrow_prices) > 0
                    and todays_prices != tomorrow_prices
                ):
                    nordpool_tomorrow_prices = self._correctDictsNordpoolIntegrationPrices(nordpool_prices = tomorrow_prices)

        self._calculatePrices(nordpool_todays_prices = nordpool_todays_prices,
                              nordpool_tomorrow_prices = nordpool_tomorrow_prices)

    def _correctDictsNordpoolIntegrationPrices(self, nordpool_prices):
        for item in nordpool_prices:
            item['start'] = self.ADapi.convert_utc(item['start'])
            item['end'] = self.ADapi.convert_utc(item['end'])
        return nordpool_prices

    def _create_daily_prices_with_taxes(self, **kwargs) -> None:
        price = kwargs['price']
        tomorrow = kwargs['tomorrow']
        nordpool_todays_prices:list = self.create_time_slots(today=True, price = price)
        if tomorrow:
            nordpool_tomorrow_prices:list = self.create_time_slots(today=False, price = price)
        else:
            nordpool_tomorrow_prices:list = []

        self._calculatePrices(nordpool_todays_prices = nordpool_todays_prices,
                              nordpool_tomorrow_prices = nordpool_tomorrow_prices)

    def create_time_slots(self, today, price):
        now = self.ADapi.datetime(aware=True)
        if today:
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            start_date = tomorrow
        time_slots = []

        for i in range(24):
            start_time = start_date + datetime.timedelta(hours=i)
            end_time = start_time + datetime.timedelta(hours=1)

            time_slots.append({
                'start': start_time,
                'end': end_time,
                'value': price
            })
        return time_slots

    # Calculates taxes and adjusts datetime
    def _calculatePrices(self,
                         nordpool_todays_prices,
                         nordpool_tomorrow_prices):
        self.sorted_elprices_today = []
        self.sorted_elprices_tomorrow = []

        isNotWorkday:bool = self._is_holiday(datetime.date.today())
        if not isNotWorkday:
            isNotWorkday = datetime.datetime.today().weekday() > 4
        beforesix = self.ADapi.parse_datetime("06:00:00", today = True, aware=True)
        aftertwentytwo = self.ADapi.parse_datetime("22:00:00", today = True, aware=True)

        # Todays prices
        nordpool_todays_prices, self.sorted_elprices_today = self._doCalculationPricesInclVat(nordpool_prices = nordpool_todays_prices,
                                                                                              beforesix = beforesix,
                                                                                              aftertwentytwo = aftertwentytwo,
                                                                                              isNotWorkday = isNotWorkday)
        self.todayslength = len(self.sorted_elprices_today)

        # Tomorrows prices if available
        if len(nordpool_tomorrow_prices) > 0:
            self.tomorrow_valid = True
            isNotWorkday:bool = self._is_holiday(datetime.date.today() + datetime.timedelta(days = 1))
            if (
                datetime.datetime.today().weekday() == 4
                or datetime.datetime.today().weekday() == 5
            ):
                isNotWorkday = True
            beforesix += datetime.timedelta(days = 1)
            aftertwentytwo += datetime.timedelta(days = 1)

            nordpool_tomorrow_prices, self.sorted_elprices_tomorrow = self._doCalculationPricesInclVat(nordpool_prices = nordpool_tomorrow_prices,
                                                                                                       beforesix = beforesix,
                                                                                                       aftertwentytwo = aftertwentytwo,
                                                                                                       isNotWorkday = isNotWorkday)
        else:
            self.tomorrow_valid = False

        self.elpricestoday = nordpool_todays_prices + nordpool_tomorrow_prices

    def _doCalculationPricesInclVat(self,
                                    nordpool_prices,
                                    beforesix,
                                    aftertwentytwo,
                                    isNotWorkday):
        sorted_elprices:list = []
        if type(self.daytax) == dict:
            month_number = nordpool_prices[0]['start'].month
            self.current_daytax = self.daytax[month_number]
        else:
            self.current_daytax = self.daytax
        if type(self.nighttax) == dict:
            month_number = nordpool_prices[0]['start'].month
            self.current_nighttax = self.nighttax[month_number]
        else:
            self.current_nighttax = self.nighttax
        
        for item in nordpool_prices:
            calculated_support:float = 0.0 # Power support calculation

            if float(item['value']) > self.power_support_above:
                calculated_support = (float(item['value']) - self.power_support_above ) * self.support_amount
            if (
                item['end'] <= beforesix
                or item['start'] >= aftertwentytwo
                or isNotWorkday
            ):
                item['value'] = round(float(item['value']) + self.current_nighttax + self.additional_tax - calculated_support, 3)
                sorted_elprices.append(item['value'])
            else:
                item['value'] = round(float(item['value']) + self.current_daytax + self.additional_tax - calculated_support, 3)
                sorted_elprices.append(item['value'])
        sorted_elprices = sorted(sorted_elprices)
        return nordpool_prices, sorted_elprices

    def get_Continuous_Cheapest_Time(self,
                                     hoursTotal:float = 2,
                                     calculateBeforeNextDayPrices:bool = False,
                                     finishByHour:int = 7,
                                     startBeforePrice:float = 0.01,
                                     stopAtPriceIncrease:float = 0.01
                                     ) -> Tuple[datetime, datetime, datetime, float]:
        """ Returns starttime, estimated endtime, Final endtime and price for cheapest continuous hours with different results depenting on time the call was made.
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
            and not calculateBeforeNextDayPrices
        ):
            return None, None, None, self.sorted_elprices_today[indexesToFinish]

        priceToComplete:float = 0.0
        avgPriceToComplete:float = 1000.0

        checkTime = self.ADapi.datetime(aware=True).replace(minute = 0, second = 0, microsecond = 0)
        start_times = [item['start'] for item in self.elpricestoday]
        end_times = [item['end'] for item in self.elpricestoday]

        index_start = bisect.bisect_left(start_times, checkTime)
        index_end = bisect.bisect_right(end_times, finishAt)
        startTime = None
        endTime = None
        start_at_index = index_start

        if index_start < index_end - indexesToFinish:
            index_end -= indexesToFinish
            while index_start <= index_end:
                for item in self.elpricestoday[index_start:index_start + indexesToFinish]:
                    priceToComplete += item['value']
                if priceToComplete < avgPriceToComplete:
                    avgPriceToComplete = priceToComplete
                    startTime = self.elpricestoday[index_start]['start']
                    endTime = self.elpricestoday[index_start+indexesToFinish-1]['end']
                    start_at_index = index_start

                priceToComplete = 0.0
                index_start += 1
        else:
            if index_start + indexesToFinish > len(self.elpricestoday):
                index_end = len(self.elpricestoday)
            else:
                index_end = index_end
            for item in self.elpricestoday[index_start:index_end]:
                priceToComplete += item['value']
            startTime = self.elpricestoday[index_start]['start']
            endTime = self.elpricestoday[index_end-1]['end']
            avgPriceToComplete = priceToComplete
        avgPriceToComplete = round(avgPriceToComplete/indexesToFinish, 3)

        #Get highest price:
        highest_price = avgPriceToComplete
        for item in self.elpricestoday[start_at_index:start_at_index+indexesToFinish]:
            if highest_price < item['value']:
                highest_price = item['value']


        endTime = self._extend_Continuous_Cheapest_EndTime(endTime = endTime,
                                                           price = highest_price,
                                                           stopAtPriceIncrease = stopAtPriceIncrease)

        final_startTime = self._extend_Continuous_Cheapest_StartTime(startTime = startTime,
                                                               price = highest_price,
                                                               startBeforePrice = startBeforePrice,
                                                               stopAtPriceIncrease = stopAtPriceIncrease)
        timediff =  startTime - final_startTime
        est_endTime = endTime - timediff
        return final_startTime, est_endTime, endTime, avgPriceToComplete

    def _extend_Continuous_Cheapest_EndTime(self, endTime, price, stopAtPriceIncrease) -> datetime:
        """ Extends charging time after estimated finish as long as price is lower than stopAtPriceIncrease
        """
        end_times = [item['end'] for item in self.elpricestoday]
        index_start = bisect.bisect_left(end_times, endTime)

        for i, current in enumerate(self.elpricestoday[index_start:]):
            original_index = index_start + i
            next_item = self.elpricestoday[original_index + 1] if original_index < len(self.elpricestoday) - 1 else None

            if next_item is None:
                return current['end']
            if price + stopAtPriceIncrease < next_item['value']:
                return current['end']
        return endTime

    def _extend_Continuous_Cheapest_StartTime(self, startTime, price, startBeforePrice, stopAtPriceIncrease) -> datetime:
        """ Check if charging should be postponed one hour or start earlier due to price.
        """
        startHourPrice = self.electricity_price_now(startTime)
        checkTime = self.ADapi.datetime(aware=True).replace(minute = 0, second = 0, microsecond = 0)
        start_times = [item['start'] for item in self.elpricestoday]
        index_now = bisect.bisect_left(start_times, checkTime)
        stop_index = bisect.bisect_left(start_times, startTime)

        for i, current in enumerate(self.elpricestoday[stop_index: stop_index + 4]):
            original_index = stop_index + i
            next_item = self.elpricestoday[original_index + 1] if original_index < len(self.elpricestoday) - 1 else None
            if current['start'] - startTime <= datetime.timedelta(hours = 1):
                if (
                    price < startHourPrice - (stopAtPriceIncrease * 1.5)
                    and startHourPrice < next_item['value'] - (stopAtPriceIncrease * 1.3)
                ):
                    return next_item['start']

        for i, current in enumerate(reversed(self.elpricestoday[index_now:stop_index + 1])):
            original_index = stop_index - i
            prev_item = self.elpricestoday[original_index - 1] if original_index > 0 else None
            if prev_item is None:
                return current['start']

            if (
                startHourPrice + startBeforePrice < prev_item['value']
                or price + (startBeforePrice * 2) < prev_item['value']
            ):
                return current['start']

        return startTime

    def get_lowest_prices(self,
                          checkitem:int = 1,
                          hours:int = 6,
                          min_change:float = None
                          ) -> float:
        """ Compares the X hour lowest price to a minimum change and retuns the highest price of those two.
        """
        hours = int(hours / 24 * self.todayslength)
        if checkitem <= self.todayslength - (2 / 24 * self.todayslength):
            if min_change is not None:
                if self.sorted_elprices_today[hours] < self.sorted_elprices_today[0] + min_change:
                    return self.sorted_elprices_today[0] + min_change
        elif self.tomorrow_valid:
            if min_change is not None:
                if self.sorted_elprices_tomorrow[hours] < self.sorted_elprices_tomorrow[0] + min_change:
                    return self.sorted_elprices_tomorrow[0] + min_change
            return self.sorted_elprices_tomorrow[hours]
        
        return self.sorted_elprices_today[hours]

    def find_times_to_save(self,
                           pricedrop: float,
                           max_continuous_hours: int,
                           on_for_minimum: int,
                           pricedifference_increase: float,
                           reset_continuous_hours: bool,
                           previous_save_hours: list
                           ) -> list:
        """Finds peak variations in electricity price for saving purposes and returns list with datetime objects;
           'start', 'end' and 'duration' as a timedelta object for how long the electricity has been off.
        """
        checkTime = self.ADapi.datetime(aware=True).replace(minute=0, second=0, microsecond=0)
        start_times = [item['start'] for item in self.elpricestoday]
        index_now = bisect.bisect_left(start_times, checkTime)

        saving_hours_list:list = []
        continuous_hours_from_old_calc = 0
        on_for_minimum = ((on_for_minimum)/ self.todayslength) * 24

        if previous_save_hours:
            saving_hours_list, continuous_hours_from_old_calc = self._keep_already_calculated_save_hours(
                previous_save_hours = previous_save_hours,
                reset_continuous_hours = reset_continuous_hours,
                max_continuous_hours = max_continuous_hours,
                on_for_minimum = on_for_minimum
            )
        saving_hours_list = self._find_peak_hours(
            index_now = index_now,
            pricedrop = pricedrop,
            saving_hours_list = saving_hours_list
        )

        if saving_hours_list:
            saving_hours_list = self._remove_save_hours_too_low(
                index_now = index_now,
                saving_hours_list = saving_hours_list,
                on_for_minimum = on_for_minimum,
                pricedrop = pricedrop
            )

            saving_hours_list = self._calculate_save_hours(
                index_now = index_now,
                pricedrop = pricedrop,
                max_continuous_hours = max_continuous_hours,
                continuous_hours_from_old_calc = continuous_hours_from_old_calc,
                on_for_minimum = on_for_minimum,
                pricedifference_increase = pricedifference_increase,
                saving_hours_list = saving_hours_list,
                reset_continuous_hours = reset_continuous_hours
            )
            peak_list = self._putPeaksInOrder(saving_hours_list)
            return peak_list
        else:
            return []

    def find_times_to_spend(self,
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
                and prev_item['value'] <= self.get_lowest_prices(checkitem = original_index, hours = 3, min_change = None)
                and not prev_item['start'] in low_priced_items
            ):
                low_priced_items.append(prev_item['start'])

        low_priced_list = self._putPeaksInOrder(low_priced_items)
        return low_priced_list

    def electricity_price_now(self, time = None) -> float:
        """ Return current complete electricity price based on now or time given
        """
        if time is None:
            time = self.ADapi.datetime(aware=True)
        for range_item in self.elpricestoday:
            if (start := range_item['start']) <= time < (end := range_item['end']):
                return range_item['value']
        return None

    def print_peaks(self,
                    saving_hours_list:list = []
                    ) -> None:
        """ Formats save and spend list to readable string for easy logging/testing of settings.
        """
        print_saving_hours_list:str = '\n'
        for item in saving_hours_list:
            print_saving_hours_list += str(
                                f"Start at {item['start']} until {item['end']}. Duration {item['duration']}.\n"
                            )
        return print_saving_hours_list

    def _putPeaksInOrder(self, saving_hours_list):
        peak_list:list = []
        continue_from_peak = False

        for current in self.elpricestoday:
            if current['start'] in [item for item in saving_hours_list]:
                if not continue_from_peak:
                    start_of_peak = current['start']
                continue_from_peak = True

            elif continue_from_peak:
                continuous_hours = current['start'] -start_of_peak
                continue_from_peak = False
                peak_dict:dict = {}
                peak_dict.update({'start' : start_of_peak, 'end': current['start'], 'duration' : continuous_hours})
                peak_list.append(peak_dict)
        
        return peak_list

    def _keep_already_calculated_save_hours(self,
                                            previous_save_hours,
                                            reset_continuous_hours,
                                            max_continuous_hours,
                                            on_for_minimum
                                            ):
        saving_hours_list = []
        continuous_hours_from_old_calc = 0
        continuous_hours_int = 0
        start_times = [item['start'] for item in self.elpricestoday]
        end_times = [item['end'] for item in self.elpricestoday]
        checkTime = self.ADapi.datetime(aware=True).replace(minute=0, second=0, microsecond=0)

        for item in previous_save_hours:
            if item['start'] > checkTime:
                if (
                    continuous_hours_int > 0
                    and continuous_hours_from_old_calc > 0
                ):
                    continuous_hours_from_old_calc -= self._calc_remove_hours_after_last_peak(
                        current_time = checkTime,
                        last_end_of_peak = start_of_peak,
                        continuous_hours_int = continuous_hours_int,
                        max_continuous_hours = max_continuous_hours,
                        on_for_minimum = on_for_minimum)

                    if continuous_hours_from_old_calc < 0:
                        continuous_hours_from_old_calc = 0
                return saving_hours_list, math.ceil(continuous_hours_from_old_calc)
            else:
                index_now = bisect.bisect_left(start_times, item['start'])

                # Find previous continuous time and remove.
                if (
                    continuous_hours_int > 0
                    and continuous_hours_from_old_calc > 0
                ):
                    continuous_hours_from_old_calc -= self._calc_remove_hours_after_last_peak(
                        current_time = item['start'],
                        last_end_of_peak = end_of_last_peak,
                        continuous_hours_int = continuous_hours_int,
                        max_continuous_hours = max_continuous_hours,
                        on_for_minimum = on_for_minimum)

                    if continuous_hours_from_old_calc < 0:
                        continuous_hours_from_old_calc = 0

                # Calculate new peak time.
                start_of_peak = item['start']
                end_of_last_peak = item['end']
                if item['end'] > checkTime:
                    end_of_peak = checkTime
                    index_end = bisect.bisect_right(end_times, checkTime)

                    for current in self.elpricestoday[index_now:index_end]:
                        saving_hours_list.append(current['start'])
                    if not reset_continuous_hours:
                        continuous_hours = end_of_peak - start_of_peak
                        continuous_hours_int = (continuous_hours.days * 24 * 60 + continuous_hours.seconds // 60) / 60
                        continuous_hours_from_old_calc += continuous_hours_int
                    return saving_hours_list, math.ceil(continuous_hours_from_old_calc)

                else:
                    index_end = bisect.bisect_right(end_times, item['end'])
                    end_of_peak = item['end']

                    for current in self.elpricestoday[index_now:index_end]:
                        saving_hours_list.append(current['start'])

                    if not reset_continuous_hours:
                        continuous_hours = end_of_peak - start_of_peak
                        continuous_hours_int = (continuous_hours.days * 24 * 60 + continuous_hours.seconds // 60) / 60
                        continuous_hours_from_old_calc += continuous_hours_int
                    else:
                        continuous_hours_from_old_calc = 0

        if end_of_last_peak < checkTime:
            if (
                continuous_hours_int > 0
                and continuous_hours_from_old_calc > 0
            ):
                continuous_hours_from_old_calc -= self._calc_remove_hours_after_last_peak(
                    current_time = checkTime,
                    last_end_of_peak = end_of_last_peak,
                    continuous_hours_int = continuous_hours_int,
                    max_continuous_hours = max_continuous_hours,
                    on_for_minimum = on_for_minimum)

                if continuous_hours_from_old_calc < 0:
                    continuous_hours_from_old_calc = 0

        return saving_hours_list, math.ceil(continuous_hours_from_old_calc)

    def _calc_remove_hours_after_last_peak(self,
                                           current_time,
                                           last_end_of_peak,
                                           continuous_hours_int,
                                           max_continuous_hours,
                                           on_for_minimum):
        time_since_last_peak = current_time - last_end_of_peak
        time_since_last_peak_int = (time_since_last_peak.days * 24 * 60 + time_since_last_peak.seconds // 60) / 60
        difference = max_continuous_hours - continuous_hours_int
        return (difference / on_for_minimum) * time_since_last_peak_int


    def _find_peak_hours(self,
                         index_now,
                         pricedrop,
                         saving_hours_list
                         ):
        for i, current in enumerate(self.elpricestoday[index_now:-1]):
            original_index = index_now + i
            prev_item = self.elpricestoday[original_index - 1] if original_index > 0 else None
            next_item = self.elpricestoday[original_index + 1] if original_index < len(self.elpricestoday) - 1 else None

            # If price drops more than wanted peak difference
            if current['value'] - next_item['value'] >= pricedrop and current['start'] not in saving_hours_list:
                saving_hours_list.append(current['start'])
            # If price drops during 2 hours
            elif prev_item is not None:
                if prev_item['value'] - next_item['value'] >= pricedrop * 1.3 and prev_item['start'] not in saving_hours_list:
                    saving_hours_list.append(prev_item['start'])

        return saving_hours_list

    def _determine_stop_calculating_at(self, saving_hours_list):
        stop_calculating_at = int(40 / 24 * self.todayslength)
        after_peak_price = 100
        last_peak_end_time = self.elpricestoday[0]['start']
        calculate_from = len(self.elpricestoday)
        for i, current in enumerate(reversed(self.elpricestoday)):
            if i < len(self.elpricestoday):
                if current['start'] in saving_hours_list:
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
                                   saving_hours_list,
                                   on_for_minimum,
                                   pricedrop
                                   ):
        for i, current in enumerate(self.elpricestoday[index_now:-2]):
            if current['start'] in saving_hours_list:
                original_index = index_now + i
                prev_item = self.elpricestoday[original_index-1]
                next_item = self.elpricestoday[original_index+1]
                if (
                    current['value'] < self.get_lowest_prices(checkitem = original_index, hours = on_for_minimum, min_change = pricedrop)
                    or prev_item['value'] < next_item['value']
                ):
                    saving_hours_list.remove(current['start'])

        return saving_hours_list

    def _calculate_save_hours(self,
                              index_now,
                              pricedrop,
                              max_continuous_hours,
                              continuous_hours_from_old_calc,
                              on_for_minimum,
                              pricedifference_increase,
                              saving_hours_list,
                              reset_continuous_hours
                              ):
        continuous_hours = datetime.timedelta(0)
        peakdiff = pricedrop
        current_max_continuous_hours = max_continuous_hours

        stop_calculating_at, after_peak_price, last_peak_end_time = self._determine_stop_calculating_at(saving_hours_list = saving_hours_list)
        continue_from_peak = False
        continuous_hours_int:float = 0
        pricedifference_increase = ((pricedifference_increase-1)/ self.todayslength) * 24 + 1

        check_index_now = stop_calculating_at - index_now -1

        for i, current in enumerate(reversed(self.elpricestoday[index_now:stop_calculating_at])):
            if current['start'] in saving_hours_list:
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
                if current['start'] not in saving_hours_list:
                    saving_hours_list.append(current['start'])
            elif continuous_hours > datetime.timedelta(0) or continue_from_peak:
                # If no peak/save found; reset
                continue_from_peak = False
                saving_hours_list, last_peak_end_time, continuous_hours_int = self._calculate_continuous_hours(
                    saving_hours_list = saving_hours_list,
                    max_continuous_hours = current_max_continuous_hours,
                    continuous_hours = continuous_hours,
                    continuous_hours_int = continuous_hours_int,
                    last_peak_end_time = last_peak_end_time,
                    pricedrop = pricedrop,
                    pricedifference_increase = pricedifference_increase,
                    reset_continuous_hours = reset_continuous_hours
                )

                if current['start'].date() == self.ADapi.datetime(aware=True).date():
                    if continuous_hours > datetime.timedelta(hours = max_continuous_hours):
                        continuous_hours = datetime.timedelta(hours = max_continuous_hours)

                continuous_hours = datetime.timedelta(0)
                peakdiff = pricedrop

            if continuous_hours_int > 0:
                difference = max_continuous_hours - continuous_hours_int
                remove = (difference / on_for_minimum) / self.todayslength * 24
                continuous_hours_int -= remove

            if current_max_continuous_hours < max_continuous_hours:
                td = last_peak_end_time - current['start']
                normal_on_timedelta = (td.days * 24 * 60 + td.seconds // 60) / 60
                current_max_continuous_hours += math.ceil(normal_on_timedelta / on_for_minimum)
            elif current_max_continuous_hours > max_continuous_hours:
                current_max_continuous_hours = max_continuous_hours

            if i == check_index_now and continue_from_peak:
                continuous_hours += datetime.timedelta(hours = continuous_hours_from_old_calc)
                saving_hours_list, last_peak_end_time, continuous_hours_int = self._calculate_continuous_hours(
                    saving_hours_list = saving_hours_list,
                    max_continuous_hours = current_max_continuous_hours,
                    continuous_hours = continuous_hours,
                    continuous_hours_int = math.ceil(continuous_hours_int),
                    last_peak_end_time = last_peak_end_time,
                    pricedrop = pricedrop,
                    pricedifference_increase = pricedifference_increase,
                    reset_continuous_hours = reset_continuous_hours
                )

                if current['start'].date() == self.ADapi.datetime(aware=True).date():
                    if continuous_hours > datetime.timedelta(hours = max_continuous_hours):
                        continuous_hours = datetime.timedelta(hours = max_continuous_hours)


        return saving_hours_list

    def _calculate_continuous_hours(self,
                                    saving_hours_list,
                                    max_continuous_hours,
                                    continuous_hours,
                                    continuous_hours_int,
                                    last_peak_end_time,
                                    pricedrop,
                                    pricedifference_increase,
                                    reset_continuous_hours
                                    ):
        continuous_hours_int += int(math.floor(((continuous_hours.days * 24 * 60 + continuous_hours.seconds // 60) / 60)))
        peak_list = self._putPeaksInOrder(saving_hours_list)
        for item in peak_list:
            continuous_hours_from_list = item['end'] - item['start']
            continuous_hours_from_list_int = int(math.floor((continuous_hours_from_list.days * 24 * 60 + continuous_hours_from_list.seconds // 60) / 60))
            if continuous_hours_from_list_int > continuous_hours_int:
                continuous_hours_from_list_int = continuous_hours_int

            if continuous_hours_from_list_int > max_continuous_hours:
                continuous_hours_to_remove = continuous_hours_from_list_int - max_continuous_hours
                saving_hours_list, last_peak_end_time = self._remove_too_many_continous_hours(
                    saving_hours_list = saving_hours_list,
                    continuous_hours_to_remove = continuous_hours_to_remove,
                    start_peak_time = item['start'],
                    last_peak_end_time = item['end'],
                    pricedrop = pricedrop,
                    pricedifference_increase = pricedifference_increase,
                    reset_continuous_hours = reset_continuous_hours
                )
                continuous_hours_int -= continuous_hours_to_remove

        return saving_hours_list, last_peak_end_time, continuous_hours_int

    def _remove_too_many_continous_hours(self,
                                         saving_hours_list,
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
        continuous_items_to_remove =  int((continuous_hours_to_remove/24 * self.todayslength))

        
        # Find the least expencive hour in peak_hour.
        list_with_lower_prices:list = []
        price_start = self.elpricestoday[index_start]['value']
        price_end = self.elpricestoday[index_end]['value']
        for i, current in enumerate(self.elpricestoday[index_start:index_end]):
            if (
                current['value'] < price_start
                and current['value'] < price_end
            ):
                original_index = index_start + i
                list_with_lower_prices.append(original_index)

        if list_with_lower_prices:
            sorted_list = sorted(self.elpricestoday[index_start:index_end], key=lambda x: x['value'])
            remove_price_below = sorted_list[len(list_with_lower_prices)]['value']

            index_start_corrected = index_start
            for i, current in enumerate(self.elpricestoday[index_start:index_end]):
                if current['value'] <= remove_price_below:
                    if current['start'] in saving_hours_list:
                        saving_hours_list.remove(current['start'])
                        continuous_items_to_remove -= 1

                    if i == index_start_corrected - index_start:
                        index_start_corrected += 1
            if (
                continuous_items_to_remove <= 0 
                or reset_continuous_hours
            ):
                return saving_hours_list, last_peak_end_time
            
            for current in reversed(self.elpricestoday[index_start_corrected:index_end]):
                if not current['start'] in saving_hours_list:
                    index_end -= 1
                    last_peak_end_time = current['start']
                else:
                    break
            index_start = index_start_corrected

        while continuous_items_to_remove > 0:
            start_pricedrop:float = self._calculate_difference_over_given_time(
                pricedrop = pricedrop,
                multiplier = pricedifference_increase,
                iterations = index_end - index_start
            )
            if (
                self.elpricestoday[index_start]['value'] > self.elpricestoday[index_end]['value'] + start_pricedrop
            ):
                if self.elpricestoday[index_end]['start'] in saving_hours_list:
                    saving_hours_list.remove(self.elpricestoday[index_end]['start'])
                    last_peak_end_time = self.elpricestoday[index_end]['start']
                    continuous_items_to_remove -= 1
                index_end -= 1
            else:
                if self.elpricestoday[index_start]['start'] in saving_hours_list:
                    saving_hours_list.remove(self.elpricestoday[index_start]['start'])
                    continuous_items_to_remove -= 1
                index_start += 1
            
            if index_start == index_end:
                break

        return saving_hours_list, last_peak_end_time

    def _calculate_difference_over_given_time(self,
                                              pricedrop: float,
                                              multiplier: float,
                                              iterations: int
                                              ) -> float:
        start_pricedrop = pricedrop * (multiplier ** iterations)
        return start_pricedrop


    def _is_holiday(self, date):
        return date in self.holidays
