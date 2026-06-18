"""Platform for Sesame BLE number integration."""

import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .__init__ import SesameDeviceWrapper

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]

    # Keypads and Touch devices do not have lock auto-lock settings to configure
    if "TOUCH" in wrapper.model_name:
        logger.debug("Skipping number setup for Sesame Touch device %s", entry.unique_id)
        return

    async_add_entities([SesameAutoLockNumber(wrapper)])


class SesameAutoLockNumber(NumberEntity):
    """Number entity to configure the auto lock delay of the Sesame lock."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the number entity."""
        self.wrapper = wrapper
        self.device = wrapper.device
        self._attr_name = "Auto Lock Delay"
        self._attr_unique_id = f"{wrapper.entry.unique_id}_auto_lock_delay"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_icon = "mdi:lock-clock"
        self._attr_native_unit_of_measurement = "s"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 3600
        self._attr_native_step = 1
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
    def native_value(self) -> float | None:
        """Return the current auto lock delay in seconds."""
        try:
            val = self.device.auto_lock_second
            return float(val) if val is not None else None
        except Exception:
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set new auto lock delay."""
        if not self.device.is_logged_in:
            raise HomeAssistantError("Device is not connected/logged in")

        try:
            await self.device.set_auto_lock_second(int(value))
        except Exception as err:
            raise HomeAssistantError(f"Failed to configure auto lock delay: {err}") from err

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info matching the main Sesame device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.wrapper.entry.unique_id)},
            name=f"Sesame {self.wrapper.model_name}",
            manufacturer="CANDY HOUSE",
            model=self.wrapper.model_name,
        )
