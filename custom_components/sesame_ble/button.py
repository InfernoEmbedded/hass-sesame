"""Platform for Sesame BLE button integration."""

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
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
    """Set up button entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]

    # Keypads and Touch devices do not have lock positions to calibrate
    if "TOUCH" in wrapper.model_name:
        logger.debug("Skipping button setup for Sesame Touch device %s", entry.unique_id)
        return

    async_add_entities([
        SesameSetLockedPositionButton(wrapper),
        SesameSetUnlockedPositionButton(wrapper),
        SesameCalibrateMagnetButton(wrapper),
    ])


class SesameBaseButton(ButtonEntity):
    """Base class for Sesame BLE buttons."""

    def __init__(self, wrapper: SesameDeviceWrapper, name_suffix: str, unique_id_suffix: str) -> None:
        """Initialize the button."""
        self.wrapper = wrapper
        self.device = wrapper.device
        self._attr_name = name_suffix
        self._attr_unique_id = f"{wrapper.entry.unique_id}_{unique_id_suffix}"
        self._attr_entity_category = EntityCategory.CONFIG
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
    def device_info(self) -> DeviceInfo:
        """Return device info matching the main Sesame device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.wrapper.entry.unique_id)},
            name=f"Sesame {self.wrapper.model_name}",
            manufacturer="CANDY HOUSE",
            model=self.wrapper.model_name,
        )


class SesameSetLockedPositionButton(SesameBaseButton):
    """Button to set the current position as the locked position."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the button."""
        super().__init__(wrapper, "Set Locked Position", "set_locked_position")
        self._attr_icon = "mdi:lock"

    async def async_press(self) -> None:
        """Press the button."""
        if not self.device.is_logged_in:
            raise HomeAssistantError("Device is not connected/logged in")

        current_angle = self.device.current_angle
        if current_angle is None:
            raise HomeAssistantError("Current position/angle is unknown")

        # Fallback to current unlock position or 0 if not loaded
        unlock_position = self.device.unlock_position
        if unlock_position is None:
            unlock_position = 0

        try:
            await self.device.configure_lock_position(current_angle, unlock_position)
        except Exception as err:
            raise HomeAssistantError(f"Failed to configure locked position: {err}") from err


class SesameSetUnlockedPositionButton(SesameBaseButton):
    """Button to set the current position as the unlocked position."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the button."""
        super().__init__(wrapper, "Set Unlocked Position", "set_unlocked_position")
        self._attr_icon = "mdi:lock-open"

    async def async_press(self) -> None:
        """Press the button."""
        if not self.device.is_logged_in:
            raise HomeAssistantError("Device is not connected/logged in")

        current_angle = self.device.current_angle
        if current_angle is None:
            raise HomeAssistantError("Current position/angle is unknown")

        # Fallback to current lock position or 0 if not loaded
        lock_position = self.device.lock_position
        if lock_position is None:
            lock_position = 0

        try:
            await self.device.configure_lock_position(lock_position, current_angle)
        except Exception as err:
            raise HomeAssistantError(f"Failed to configure unlocked position: {err}") from err


class SesameCalibrateMagnetButton(SesameBaseButton):
    """Button to trigger magnet calibration/angle correction."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the button."""
        super().__init__(wrapper, "Calibrate Magnet", "calibrate_magnet")
        self._attr_icon = "mdi:compass"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        """Press the button."""
        if not self.device.is_logged_in:
            raise HomeAssistantError("Device is not connected/logged in")

        try:
            await self.device.calibrate_magnet()
        except Exception as err:
            raise HomeAssistantError(f"Failed to calibrate magnet: {err}") from err
