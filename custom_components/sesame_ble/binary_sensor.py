"""Platform for Sesame BLE binary sensor integration."""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .__init__ import SesameDeviceWrapper

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]

    # Door sensor is only for locks (paired with OpenSensor)
    if "TOUCH" not in wrapper.model_name:
        async_add_entities([SesameDoorBinarySensor(wrapper)])


class SesameDoorBinarySensor(BinarySensorEntity):
    """Binary sensor representing the Sesame paired door (OpenSensor) status."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the binary sensor."""
        self.wrapper = wrapper
        self.device = wrapper.device
        self._attr_name = "Door"
        self._attr_unique_id = f"{wrapper.entry.unique_id}_door"
        self._attr_device_class = BinarySensorDeviceClass.DOOR
        self._unregister_status_callback = None

    async def async_added_to_hass(self) -> None:
        """Register callbacks when added to Home Assistant."""
        self._unregister_status_callback = self.wrapper.register_update_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callbacks when removed."""
        if self._unregister_status_callback:
            self._unregister_status_callback()

    @property
    def available(self) -> bool:
        """Return true if the device is connected and logged in."""
        return self.device.is_logged_in

    @property
    def is_on(self) -> bool | None:
        """Return true if the door is open."""
        try:
            status = getattr(self.device, "door_status", None)
            if status == "open":
                return True
            if status == "closed":
                return False
            return None
        except Exception:
            return None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info matching the main Sesame device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.wrapper.entry.unique_id)},
            name=f"Sesame {self.wrapper.model_name}",
            manufacturer="CANDY HOUSE",
            model=self.wrapper.model_name,
        )
