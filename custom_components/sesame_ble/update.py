"""Update platform for Sesame BLE integration."""

import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SesameDeviceWrapper
from .const import DOMAIN

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up update entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SesameFirmwareUpdateEntity(wrapper)])


class SesameFirmwareUpdateEntity(UpdateEntity):
    """Representation of a Sesame Firmware Update entity for Home Assistant Update Notifications."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the update entity."""
        self.wrapper = wrapper
        self.device = wrapper.device
        self._attr_name = "Firmware Update"
        self._attr_unique_id = f"{wrapper.entry.unique_id}_firmware_update"
        self._attr_title = f"Sesame {wrapper.model_name} Firmware"
        self._attr_release_url = "https://candyhouse.co/"
        self._unregister_status_callback = None
        self._in_progress = False

    async def async_added_to_hass(self) -> None:
        """Register update listener when added to Home Assistant."""
        self._unregister_status_callback = self.wrapper.register_update_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update listener."""
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
            sw_version=self.installed_version,
        )

    @property
    def installed_version(self) -> str | None:
        """Return the installed firmware version on the Sesame device."""
        return self.device.firmware_version

    @property
    def latest_version(self) -> str | None:
        """Return the latest available firmware version for this model."""
        return self.wrapper.latest_firmware_version or self.device.firmware_version

    @property
    def release_summary(self) -> str | None:
        """Return summary of release notes."""
        installed = self.installed_version
        latest = self.latest_version
        if installed and latest and installed != latest:
            return f"A new firmware update ({latest}) is available for Sesame {self.wrapper.model_name}."
        return "Firmware is up to date."

    @property
    def in_progress(self) -> bool:
        """Return whether a firmware update / DFU mode switch is in progress."""
        return self._in_progress

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install firmware update by initiating BLE DFU mode on the device."""
        self._in_progress = True
        self.async_write_ha_state()

        try:
            logger.info("Initiating firmware DFU mode update on Sesame device %s", self.device.mac_address)
            await self.device.enable_dfu()
        except Exception as err:
            logger.error("Failed to trigger DFU firmware update on %s: %s", self.device.mac_address, err)
            raise
        finally:
            self._in_progress = False
            self.async_write_ha_state()
