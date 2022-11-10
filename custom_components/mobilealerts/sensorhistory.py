"""Support for the MobileAlerts service."""
from datetime import timedelta
import logging
from typing import Any, Tuple, List, Mapping, Optional

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA

from homeassistant.const import (
    ATTR_ATTRIBUTION,
    CONF_NAME,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_METHOD,
    CONF_MAXIMUM,
    CONF_MINIMUM,
    CONF_MODE,
)

CONF_DURATION = "duration"
CONF_MEAN = "mean"
CONF_DIFFERENCE = "difference"
CONF_WEATHER = "weather"


from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle, dt

import requests
import bs4

from . import extract_start_stop, extract_value_units

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = "Data provided by {0}"

DEFAULT_NAME = "MA"

MIN_TIME_BETWEEN_UPDATES = timedelta(hours=1)

DEVICE_CLASS = {
    "temperature": "Temperature",
    "wind_speed": "Wind speed",
    "humidity": "Humidity",
    "pressure": "Pressure",
    "rain": "Rain",
    "snow": "Snow",
}

# first element is default
MODE_TYPES = [
    "historic",
    "current"
]

METHOD_TYPES: List[str] = [
    CONF_MAXIMUM,
    CONF_MINIMUM,
    CONF_MEAN,
    CONF_DIFFERENCE,
]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_DEVICE_ID, default=""): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): vol.In(DEVICE_CLASS),
        vol.Optional(CONF_MODE, default=MODE_TYPES[0]): vol.In(MODE_TYPES),
        vol.Optional(CONF_DEVICES, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_METHOD): vol.In(METHOD_TYPES),
        vol.Optional(CONF_DURATION, default=24): cv.positive_int,
        vol.Optional(CONF_WEATHER): cv.string
    }
)


async def async_setup_platform(hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback, discovery_info=None):
    name = config.get(CONF_NAME)
    device_class = config.get(CONF_DEVICE_CLASS)
    device_id = config.get(CONF_DEVICE_ID)

    if device_id != "": # historic
        method = config.get(CONF_METHOD)
        duration = config.get(CONF_DURATION)
        mad = MobileAlertsData(hass, device_id, device_class, method, duration)
        async_add_entities(
            [MobileAlertsWeather.historic(name, mad)],
            True,
        )
    else:   # current
        ma_weather = config.get(CONF_WEATHER)
        if hass.states.get(ma_weather) is None:
            raise Exception("weather Entity {0} not found".format(ma_weather))
            
        for device_class in config[CONF_DEVICES]:
            sensor_name = name.lower() + "_" + device_class
            async_add_entities(
                [MobileAlertsWeather.current(sensor_name, device_class, ma_weather)],
                False,
            )


class MobileAlertsData():
    """Get the latest data from MobileAlerts."""

    def __init__(self, hass: HomeAssistant, device_id: str, device_class: str, method: str, duration: int) -> None:
        self._device_id = device_id
        self._device_class = device_class
        self._duration = duration
        self._method = method
        self._time_zone = hass.config.time_zone
        self.unit = None
        self.data = None


    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self, hass: HomeAssistant) -> None:
        # get readings from MA website
        obs, unit = self.get_reading()

        if obs is None:
            _LOGGER.warning("Failed to fetch data from OWM")
            return

        self.data = obs
        self.unit = unit


    def get_reading(self):
        data_table = self.get_results_table(self._device_id, self._duration)

        if data_table is None:
            return None, ""

        column_name = DEVICE_CLASS[self._device_class]
        values, unit = self.get_measurements(data_table, column_name, True)

        if len(values) == 0:
            return 0, ""

        result = 0
        if self._method == CONF_MAXIMUM:
            result = max(values)
        elif self._method == CONF_MINIMUM:
            result = min(values)
        elif self._method == CONF_MEAN:
            result = sum(values) / len(values) 
        elif self._method == CONF_DIFFERENCE:
            end = float(values[0])
            start = float(values[-1])
            result = end - start
        if result > 100:
            result = int(result)
        else:
            result = round(result, 1)
        return result, unit


    def get_device_history_url(self, device_id: str, duration: int) -> str:
        """
        Create url to get the last 24h of readings for this device
        Parameters
            device_id   
        """
        base_url="https://measurements.mobile-alerts.eu/Home/MeasurementDetails"
        params = { "vendorid" : "bb8e868c-e5fd-4130-8d72-b08d1013c98e", "appbundle" : "eu.mobile_alerts.mobilealerts" }

        params.update({"deviceid" : device_id })

        now = dt.now(dt.get_time_zone(self._time_zone))

        start_of_period = now - timedelta(seconds = duration * 60 * 60)

        params.update({"fromepoch" : int(start_of_period.timestamp())})
        params.update({"toepoch" : int(now.timestamp())})

        all_params = '&'.join('{0}={1}'.format(key, val) for key, val in params.items())
        
        return base_url + "?" + all_params


    def get_results_table(self, device_id: str, duration: int):
        url = self.get_device_history_url(device_id, duration)
        headers = {
            'User-Agent': 'Mozilla/5.0'
        }

        try:
            response = requests.get(url, headers=headers)
        except ConnectionError:
            _LOGGER.warning("Unable to connect to MA URL : {}".format(url))
            return None
        except TimeoutError:
            _LOGGER.warning("Timeout connecting to MA URL")
            return None

        if response.status_code != requests.codes.ok:
            raise Exception("requests getting data: {0}, {1}".format(response.status_code, url))

        soup = bs4.BeautifulSoup(response.text, "html.parser")
        tables = soup.find_all('table')
        if len(tables) == 0:
            _LOGGER.warning("No data returned : {}".format(url))
            return None

        data_table = tables[0]
        return data_table


    def get_position(self, data_table, column_name: str) -> int:
        """
        Get the column number of the column_name provided
        """
        head = data_table.thead
        # extract all <td> elements to create a list of all of the column names
        data = map(lambda td : td.contents[0], head.find_all('th'))
        columns = list(data)
        try:
            col_no = columns.index(column_name)
        except:
            raise Exception("Column {0} not found in sensor data {1}".format(column_name, columns))
        
        return col_no


    def get_measurements(self, data_table, column_name: str, is_numeric: bool) -> Tuple[List[Any], str]:
        """
        Get a list of measurements for the column_name provided
        Parameters
            data_table mobile-alerts table (with a tbody containing multiple rows/columns)
            column_name name of the column to extract
            is_numeric if data is numeric, convert it to a list of float

        Returns
            measurements as a list and the units as a string
        """
        col = self.get_position(data_table, column_name)
        data = map(lambda x: x.find_all('td')[col].contents[0], data_table.tbody.find_all('tr'))
        measurements = list(data)

        if len(measurements) == 0:
            return [], ""

        reading_from_end, unit_from_end = extract_start_stop(measurements[0])
        first_value, unit = extract_value_units(measurements[0])

        # remove the units from each element of the list
        if is_numeric:
            measurements = map(lambda s : float(s[:-reading_from_end]), measurements)
        else:
            measurements = map(lambda s : s[:-reading_from_end], measurements)
        measurements = list(measurements)

        return measurements, unit


class MobileAlertsWeather(Entity):
    """Implementation of an MobileAlerts sensor."""

    def __init__(self, name: str, mad: Optional[MobileAlertsData], device_class: Optional[str], weather: Optional[str]):
        """Initialize the sensor."""
        self._name = name
        self._mad = mad
        self._device_class = device_class
        self._weather = weather
        self._state = None
        self._unit_of_measurement = ""
        if self._weather is None:
            source = "MobileAlerts"
        else:
            source = self._weather
        self._attributes = {
            ATTR_ATTRIBUTION: ATTRIBUTION.format(source),
        }

    @classmethod
    def current(cls, name: str, device_class : str, weather: str):
        return cls(name, None, device_class, weather)

    @classmethod
    def historic(cls, name: str, mad: MobileAlertsData):
        return cls(name, mad, None, None)

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self) -> str:
        return self._unit_of_measurement

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        return self._attributes


    def update(self) -> None:
        """Get the latest data from Mobile Alerts and updates the state."""

        if self._weather is None:
            #try:
            self._mad.update(self.hass)
            #except:
            #    _LOGGER.error("Exception when getting MA web update data")
            #    return

            self._state = self._mad.data
            self._unit_of_measurement = self._mad.unit
        else:
            # get current reading from the weather entity
            weather = self.hass.states.get(self._weather)
            sensor_value = weather.attributes.get(self._device_class)
            self._state, self._unit_of_measurement = extract_value_units(sensor_value)