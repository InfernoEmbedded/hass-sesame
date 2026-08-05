"""Platform for Sesame BLE sensor integration."""

import asyncio
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .__init__ import SesameDeviceWrapper
from .sesame_client import SesameKeypad

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]
    
    entities = [
        SesameBatterySensor(wrapper),
    ]
    
    # Keypad specific sensors
    if "TOUCH" in wrapper.model_name:
        entities.extend([
            SesameTouchCardSensor(wrapper),
            SesameTouchFingerprintSensor(wrapper),
            SesameTouchPasscodeSensor(wrapper),
            SesameTouchPairedLocksSensor(wrapper),
        ])
    else:
        # Lock specific diagnostic sensors
        entities.extend([
            SesameLockLockedPositionSensor(wrapper),
            SesameLockUnlockedPositionSensor(wrapper),
            SesameLockCurrentPositionSensor(wrapper),
        ])

    # Append diagnostic/diagnostics sensors at the end
    entities.extend([
        SesameRSSISensor(wrapper),
        SesameConnectionSensor(wrapper),
    ])

    async_add_entities(entities)




class SesameBaseSensor(SensorEntity):
    """Base class for Sesame BLE sensors."""

    def __init__(self, wrapper: SesameDeviceWrapper, name_suffix: str, unique_id_suffix: str) -> None:
        """Initialize the sensor."""
        self.wrapper = wrapper
        self.device = wrapper.device
        self._attr_name = name_suffix
        self._attr_unique_id = f"{wrapper.entry.unique_id}_{unique_id_suffix}"
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


class SesameBatterySensor(SesameBaseSensor):
    """Battery sensor for Sesame BLE devices."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the battery sensor."""
        super().__init__(wrapper, "Battery", "battery")
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        """Return the current battery level."""
        try:
            return self.device.battery_percentage
        except Exception:
            return None


class SesameTouchCardSensor(SesameBaseSensor):
    """NFC/IC Cards count sensor for Sesame Touch."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the card sensor."""
        super().__init__(wrapper, "Registered Cards", "registered_cards")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:credit-card-outline"

    @property
    def native_value(self) -> int | None:
        """Return the number of cards registered."""
        try:
            return self.device.cards_count
        except Exception:
            return None


class SesameTouchFingerprintSensor(SesameBaseSensor):
    """Fingerprints count sensor for Sesame Touch."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the fingerprint sensor."""
        super().__init__(wrapper, "Registered Fingerprints", "registered_fingerprints")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:fingerprint"

    @property
    def native_value(self) -> int | None:
        """Return the number of fingerprints registered."""
        try:
            return self.device.fingerprints_count
        except Exception:
            return None


class SesameTouchPasscodeSensor(SesameBaseSensor):
    """Passcodes count sensor for Sesame Touch."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the passcode sensor."""
        super().__init__(wrapper, "Registered Passcodes", "registered_passcodes")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:keyboard-outline"
        self._passcodes_list: dict[str, dict] = {}
        self._sync_task: asyncio.Task | None = None

    async def async_added_to_hass(self) -> None:
        """Register callbacks and trigger passcode sync on startup."""
        await super().async_added_to_hass()
        # Fetch the passcodes list on start in the background
        self._sync_task = asyncio.create_task(self._fetch_passcodes())

    async def _fetch_passcodes(self) -> None:
        """Fetch registered passcodes list via BLE."""
        # Wait until wrapper device is ready and logged in
        for _ in range(30):
            if self.device.is_logged_in:
                break
            await asyncio.sleep(1)

        if self.device.is_logged_in:
            try:
                sesame_touch: SesameKeypad = self.device
                await sesame_touch.get_passcodes()
                self._passcodes_list = sesame_touch.passcodes
                self.async_write_ha_state()
            except Exception as e:
                logger.warning("Failed to fetch initial passcode list: %s", e)

    @property
    def native_value(self) -> int | None:
        """Return the number of passcodes registered."""
        try:
            return self.device.passcodes_count
        except Exception:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return passcode list as attributes."""
        # Sync the latest passcodes list from the device memory
        if hasattr(self.device, "passcodes"):
            self._passcodes_list = self.device.passcodes
        return {"passcodes": self._passcodes_list}


class SesameLockLockedPositionSensor(SesameBaseSensor):
    """Sensor to show the calibrated locked position angle."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the locked position sensor."""
        super().__init__(wrapper, "Locked Position", "locked_position")
        self._attr_native_unit_of_measurement = "°"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:lock"

    @property
    def native_value(self) -> int | None:
        """Return the locked position angle."""
        try:
            return self.device.lock_position
        except Exception:
            return None


class SesameLockUnlockedPositionSensor(SesameBaseSensor):
    """Sensor to show the calibrated unlocked position angle."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the unlocked position sensor."""
        super().__init__(wrapper, "Unlocked Position", "unlocked_position")
        self._attr_native_unit_of_measurement = "°"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:lock-open"

    @property
    def native_value(self) -> int | None:
        """Return the unlocked position angle."""
        try:
            return self.device.unlock_position
        except Exception:
            return None


class SesameLockCurrentPositionSensor(SesameBaseSensor):
    """Sensor to show the current lock angle."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the current position sensor."""
        super().__init__(wrapper, "Current Angle", "current_angle")
        self._attr_native_unit_of_measurement = "°"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:rotate-right"

    @property
    def native_value(self) -> int | None:
        """Return the current position angle."""
        try:
            return self.device.current_angle
        except Exception:
            return None


class SesameTouchPairedLocksSensor(SesameBaseSensor):
    """Paired locks count and details sensor for Sesame Touch."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the paired locks sensor."""
        super().__init__(wrapper, "Paired Locks", "paired_locks")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:lock-link"
        self._paired_locks: list[dict[str, Any]] = []

    @property
    def native_value(self) -> int | None:
        """Return the number of paired locks."""
        try:
            return len(self.device.paired_locks)
        except Exception:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the list of paired locks as attributes."""
        if hasattr(self.device, "paired_locks"):
            self._paired_locks = self.device.paired_locks
        return {"paired_locks": self._paired_locks}


class SesameRSSISensor(SesameBaseSensor):
    """RSSI sensor for Sesame BLE devices."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the RSSI sensor."""
        super().__init__(wrapper, "Signal Strength", "rssi")
        self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "dBm"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """RSSI sensor is always available."""
        return True

    @property
    def native_value(self) -> int | None:
        """Return the RSSI value."""
        from homeassistant.components import bluetooth
        service_info = bluetooth.async_last_service_info(
            self.wrapper.hass, self.wrapper.ble_device.address
        )
        if service_info:
            return service_info.rssi
        return None


class SesameConnectionSensor(SesameBaseSensor):
    """Sensor to report connection status."""

    def __init__(self, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the connection sensor."""
        super().__init__(wrapper, "Connection State", "connection_state")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:bluetooth-connect"

    @property
    def available(self) -> bool:
        """Connection sensor is always available."""
        return True

    @property
    def native_value(self) -> str:
        """Return the connection state."""
        return "connected" if self.device.is_connected else "disconnected"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return connection attributes."""
        return {
            "is_connected": self.device.is_connected,
            "is_logged_in": self.device.is_logged_in,
        }


