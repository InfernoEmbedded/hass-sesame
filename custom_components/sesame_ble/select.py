"""Platform for Sesame BLE select integration."""

import logging
from typing import Any
from uuid import UUID

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from . import SesameDeviceWrapper, is_keypad_model, on_demand_connection


logger = logging.getLogger(__name__)

PLACEHOLDER = "Select lock..."


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]

    # Only Keypads and Touch devices support lock pairing management
    if not is_keypad_model(wrapper.model_name):
        logger.debug("Skipping select setup for Sesame Lock device %s", entry.unique_id)
        return

    async_add_entities(
        [
            SesameTouchPairLockSelect(wrapper),
            SesameTouchUnpairLockSelect(wrapper),
        ]
    )


class SesameTouchBaseSelect(SelectEntity):
    """Base class for Sesame Touch selects."""

    def __init__(
        self, wrapper: SesameDeviceWrapper, name_suffix: str, unique_id_suffix: str
    ) -> None:
        """Initialize the select entity."""
        self.wrapper = wrapper
        self.device = wrapper.device
        self._attr_name = name_suffix
        self._attr_unique_id = f"{wrapper.entry.unique_id}_{unique_id_suffix}"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_current_option = PLACEHOLDER
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
        """Return true if the device is connected or available via BLE advertisements."""
        if is_keypad_model(self.wrapper.model_name):
            return self.wrapper.is_available
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


class SesameTouchPairLockSelect(SesameTouchBaseSelect):
    """Select entity to pair a new Sesame lock to the keypad."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the select."""
        super().__init__(wrapper, "Pair Lock", "pair_lock")
        self._attr_icon = "mdi:lock-plus"

    @property
    def options(self) -> list[str]:
        """Return the list of locks that can be paired."""
        # Get list of already paired locks
        paired_uuids = {
            l["uuid"].lower() for l in getattr(self.device, "paired_locks", [])
        }

        # Find all locks in Home Assistant not currently paired
        options = [PLACEHOLDER]
        for entry_id, other_wrapper in self.wrapper.hass.data[DOMAIN].items():
            if not hasattr(other_wrapper, "model_name") or is_keypad_model(
                other_wrapper.model_name
            ):
                continue

            lock_uuid = str(other_wrapper.adv_data.device_uuid).lower()
            if lock_uuid not in paired_uuids:
                options.append(other_wrapper.entry.title)

        return options

    async def async_select_option(self, option: str) -> None:
        """Pair the selected lock."""
        if option == PLACEHOLDER:
            return

        # Resolve wrapper for target lock
        target_wrapper = None
        for entry_id, other_wrapper in self.wrapper.hass.data[DOMAIN].items():
            if hasattr(other_wrapper, "entry") and other_wrapper.entry.title == option:
                target_wrapper = other_wrapper
                break

        if not target_wrapper:
            raise HomeAssistantError(
                f"Could not find configured lock matching '{option}'"
            )

        try:
            lock_uuid = target_wrapper.adv_data.device_uuid
            secret_key_bytes = bytes.fromhex(target_wrapper.secret_key)
            async with on_demand_connection(self.wrapper, timeout=30.0) as dev:
                await dev.add_paired_lock(lock_uuid, secret_key_bytes)
                self.wrapper._handle_status_update(
                    dev, getattr(dev, "mech_status", None)
                )
        except Exception as err:
            raise HomeAssistantError(f"Failed to pair lock: {err}") from err


class SesameTouchUnpairLockSelect(SesameTouchBaseSelect):
    """Select entity to unpair a Sesame lock from the keypad."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the select."""
        super().__init__(wrapper, "Unpair Lock", "unpair_lock")
        self._attr_icon = "mdi:lock-minus"

    @property
    def options(self) -> list[str]:
        """Return the list of paired locks that can be unpaired."""
        options = [PLACEHOLDER]
        for lock_info in getattr(self.device, "paired_locks", []):
            lock_uuid_str = lock_info["uuid"].lower()

            # Resolve name from HA if possible
            resolved_name = None
            for entry_id, other_wrapper in self.wrapper.hass.data[DOMAIN].items():
                if str(other_wrapper.adv_data.device_uuid).lower() == lock_uuid_str:
                    resolved_name = other_wrapper.entry.title
                    break

            options.append(resolved_name or lock_info["uuid"])

        return options

    async def async_select_option(self, option: str) -> None:
        """Unpair the selected lock."""
        if option == PLACEHOLDER:
            return

        # Resolve UUID from selection option
        target_uuid = None
        for lock_info in getattr(self.device, "paired_locks", []):
            lock_uuid_str = lock_info["uuid"]

            # Match by uuid directly
            if lock_uuid_str == option:
                target_uuid = UUID(lock_uuid_str)
                break

            # Match by resolved name
            resolved_name = None
            for entry_id, other_wrapper in self.wrapper.hass.data[DOMAIN].items():
                if (
                    str(other_wrapper.adv_data.device_uuid).lower()
                    == lock_uuid_str.lower()
                ):
                    resolved_name = other_wrapper.entry.title
                    break

            if resolved_name == option:
                target_uuid = UUID(lock_uuid_str)
                break

        if not target_uuid:
            raise HomeAssistantError(
                f"Could not resolve lock UUID for option '{option}'"
            )

        try:
            async with on_demand_connection(self.wrapper, timeout=30.0) as dev:
                await dev.remove_paired_lock(target_uuid)
                self.wrapper._handle_status_update(
                    dev, getattr(dev, "mech_status", None)
                )
        except Exception as err:
            raise HomeAssistantError(f"Failed to unpair lock: {err}") from err
