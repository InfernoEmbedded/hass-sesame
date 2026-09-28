"""Platform for Sesame BLE lock integration."""

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .__init__ import SesameDeviceWrapper, is_keypad_model
from pysesame_ble import SesameLock

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lock entity for Candy House Sesame BLE lock."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]
    model_name = wrapper.model_name

    # Keypads and Touch devices do not have lock entities
    if is_keypad_model(model_name):
        logger.debug("Skipping lock entity setup for Sesame Touch device %s", entry.unique_id)
        return

    async_add_entities([SesameBLELock(wrapper)])


class SesameBLELock(LockEntity):
    """Representation of a Candy House Sesame BLE lock."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the lock."""
        self.wrapper = wrapper
        self.sesame: SesameLock = wrapper.device
        self._attr_name = None
        self._attr_unique_id = f"{wrapper.entry.unique_id}_lock"
        self._attr_device_class = None  # standard lock
        
        # Unregister callback tracker
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
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        try:
            return self.sesame.is_locked
        except Exception:
            return None

    @property
    def is_locking(self) -> bool:
        """Return true if lock is locking."""
        # The protocol doesn't have a specific locking state, but we can infer it
        # if the motor status is moving towards locked
        return False

    @property
    def is_unlocking(self) -> bool:
        """Return true if lock is unlocking."""
        return False

    @property
    def available(self) -> bool:
        """Return true if the device is connected and logged in."""
        return self.sesame.is_logged_in

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this Sesame lock."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.wrapper.entry.unique_id)},
            name=f"Sesame {self.wrapper.model_name}",
            manufacturer="CANDY HOUSE",
            model=self.wrapper.model_name,
            connections={(dr.CONNECTION_BLUETOOTH, self.wrapper.ble_device.address)},
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "current_angle": self.sesame.current_angle,
            "lock_position": self.sesame.lock_position,
            "unlock_position": self.sesame.unlock_position,
        }

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the device."""
        try:
            await self.sesame.lock(history_name="Home Assistant")
        except Exception as e:
            logger.error("Failed to lock Sesame %s: %s", self.wrapper.ble_device.address, e)
            raise

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the device."""
        try:
            await self.sesame.unlock(history_name="Home Assistant")
        except Exception as e:
            logger.error("Failed to unlock Sesame %s: %s", self.wrapper.ble_device.address, e)
            raise
