"""Sensor entities: a cumulative meter reading + a live consumption rate.

Two entities per meter:
  * <type> total — a total_increasing register the Energy dashboard consumes
    directly (HA derives long-term statistics from it automatically).
  * <type> rate  — the latest per-hour consumption, for live display.
Both read from the coordinator's shared data; they never call the API themselves.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower, UnitOfVolumeFlowRate
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SOURCE_UNITS, convert_volume

# Map our meter type -> HA device class. The device class drives the dashboard
# category (gas/water source), the icon, and which units HA considers valid.
DEVICE_CLASS = {
    "gas": SensorDeviceClass.GAS,
    "water_indoor": SensorDeviceClass.WATER,
    "water_outdoor": SensorDeviceClass.WATER,
    "electric": SensorDeviceClass.ENERGY,
}


async def async_setup_entry(hass, entry, async_add_entities):
    """Create two sensor entities for each discovered meter."""
    # The coordinator was stashed in __init__.async_setup_entry.
    coordinator = entry.runtime_data
    entities = []
    for meter in coordinator.meters:
        entities.append(CopperMeterSensor(coordinator, meter))  # cumulative total
        entities.append(CopperRateSensor(coordinator, meter))   # live rate
    async_add_entities(entities)


class _CopperBase(CoordinatorEntity, SensorEntity):
    """Shared plumbing for both sensor types.

    Subclassing CoordinatorEntity wires the entity to coordinator updates
    (availability + state refresh) for free.
    """

    # Entity name is combined with the device name by HA (modern naming).
    _attr_has_entity_name = True

    def __init__(self, coordinator, meter):
        super().__init__(coordinator)
        self._meter = meter
        self._mid = meter["id"]  # cache the meter id used for lookups + unique_id
        # Group both sensors under one device per physical meter, so HA shows a
        # proper device page and prefixes entity names with the device name.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mid)},
            name=f"Copper {meter['type'].replace('_', ' ')} meter",
            manufacturer="Copper Labs",
            model=meter["type"],
        )

    def _data(self) -> dict:
        """This meter's latest reading from the coordinator ({} if not present yet)."""
        return (self.coordinator.data or {}).get(self._mid, {})


class CopperMeterSensor(_CopperBase):
    """Cumulative meter reading — consumed directly by the Energy dashboard."""

    # total_increasing = a monotonically rising meter register (HA handles the
    # occasional rollover and computes per-period usage from it).
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    # Round the (possibly long) converted decimals for display only; stored value
    # keeps full precision.
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, meter):
        super().__init__(coordinator, meter)
        # Stable unique_id so HA remembers the entity across restarts; ':' isn't
        # allowed in the tail so replace it with '_'.
        self._attr_unique_id = f"{DOMAIN}_{self._mid.replace(':', '_')}_total"
        # Short name; HA prefixes the device name ("Copper gas meter Total").
        self._attr_name = "Total"
        # device_class drives dashboard category/valid units; None for unknown types.
        self._attr_device_class = DEVICE_CLASS.get(meter["type"])
        # Display unit = the user's chosen unit (the coordinator already converted
        # the value into it).
        self._attr_native_unit_of_measurement = coordinator.units.get(meter["type"])

    @property
    def native_value(self):
        # The cumulative register; None when there's no reading -> unavailable.
        return self._data().get("value")

    @property
    def extra_state_attributes(self):
        # Handy extras for debugging/automations, not shown as the main state.
        return {"meter_id": self._mid, "last_reading": self._data().get("time")}


class CopperRateSensor(_CopperBase):
    """Latest consumption expressed as a per-hour rate."""

    # measurement = an instantaneous value (not accumulated).
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, meter):
        super().__init__(coordinator, meter)
        self._attr_unique_id = f"{DOMAIN}_{self._mid.replace(':', '_')}_rate"
        # Short name; HA prefixes the device name ("Copper gas meter Rate").
        self._attr_name = "Rate"
        if meter["type"] == "electric":
            # Electric "power" is kWh consumed per hour — that's just kilowatts,
            # so give it the proper power device class instead of "kWh/h".
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
        else:
            # A real volume-flow-rate entity: valid unit (m³/h), Energy
            # dashboard accepts it as "gas flow rate", and users can switch the
            # displayed unit (gal/min, L/min, ...) in HA's entity settings —
            # made-up units like "gal/h" support none of that.
            self._attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
            self._attr_native_unit_of_measurement = (
                UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR
            )

    @property
    def native_value(self):
        power = self._data().get("power")  # native units per hour (coordinator)
        if power is None:
            return None
        if self._meter["type"] == "electric":
            return round(float(power), 3)  # kWh/h == kW, no conversion
        # native volume per hour -> m³ per hour.
        return round(convert_volume(float(power), SOURCE_UNITS[self._meter["type"]], "m³"), 5)
