"""Platform for Sesame BLE sensor integration."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .__init__ import SesameDeviceWrapper, is_keypad_model, get_supported_auth_methods

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SesameSensorDescription(SensorEntityDescription):
    """Class describing Sesame BLE sensor entities."""

    value_fn: Callable[[SesameDeviceWrapper], Any]
    attrs_fn: Callable[[SesameDeviceWrapper], dict[str, Any]] | None = None
    always_available: bool = False
    init_task_fn: Callable[[Any], Coroutine[Any, Any, None]] | None = None


async def _fetch_passcodes_on_start(sensor: Any) -> None:
    """Fetch registered passcodes list via BLE in the background."""
    for _ in range(30):
        if sensor.device.is_logged_in:
            break
        await asyncio.sleep(1)

    if sensor.device.is_logged_in:
        try:
            await sensor.device.get_passcodes()
            sensor.async_write_ha_state()
        except Exception as e:
            logger.warning("Failed to fetch initial passcode list: %s", e)


def _get_rssi(wrapper: SesameDeviceWrapper) -> int | None:
    from homeassistant.components import bluetooth
    service_info = bluetooth.async_last_service_info(
        wrapper.hass, wrapper.ble_device.address
    )
    if service_info:
        return service_info.rssi
    return None


SENSOR_DESCRIPTIONS_COMMON: tuple[SesameSensorDescription, ...] = (
    SesameSensorDescription(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda w: getattr(w.device, "battery_percentage", None),
    ),
)

SENSOR_DESC_CARD = SesameSensorDescription(
    key="registered_cards",
    name="Registered Cards",
    icon="mdi:credit-card-outline",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda w: getattr(w.device, "cards_count", None),
)

SENSOR_DESC_FINGERPRINT = SesameSensorDescription(
    key="registered_fingerprints",
    name="Registered Fingerprints",
    icon="mdi:fingerprint",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda w: getattr(w.device, "fingerprints_count", None),
)

SENSOR_DESC_PASSCODE = SesameSensorDescription(
    key="registered_passcodes",
    name="Registered Passcodes",
    icon="mdi:keyboard-outline",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda w: getattr(w.device, "passcodes_count", None),
    attrs_fn=lambda w: {"passcodes": getattr(w.device, "passcodes", {})},
    init_task_fn=_fetch_passcodes_on_start,
)

SENSOR_DESC_FACE = SesameSensorDescription(
    key="registered_faces",
    name="Registered Faces",
    icon="mdi:face-recognition",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda w: len(w.logical_faces) if hasattr(w, "logical_faces") else getattr(w.device, "faces_count", None),
)

SENSOR_DESC_PALM = SesameSensorDescription(
    key="registered_palms",
    name="Registered Palms",
    icon="mdi:hand-wave",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda w: len(w.logical_palms) if hasattr(w, "logical_palms") else getattr(w.device, "palms_count", None),
)

SENSOR_DESC_PAIRED_LOCKS = SesameSensorDescription(
    key="paired_locks",
    name="Paired Locks",
    icon="mdi:lock-link",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda w: len(w.device.paired_locks) if hasattr(w.device, "paired_locks") and w.device.paired_locks else (0 if hasattr(w.device, "paired_locks") else None),
    attrs_fn=lambda w: {"paired_locks": getattr(w.device, "paired_locks", [])},
)

SENSOR_DESCRIPTIONS_LOCK: tuple[SesameSensorDescription, ...] = (
    SesameSensorDescription(
        key="locked_position",
        name="Locked Position",
        icon="mdi:lock",
        native_unit_of_measurement="°",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda w: getattr(w.device, "lock_position", None),
    ),
    SesameSensorDescription(
        key="unlocked_position",
        name="Unlocked Position",
        icon="mdi:lock-open",
        native_unit_of_measurement="°",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda w: getattr(w.device, "unlock_position", None),
    ),
    SesameSensorDescription(
        key="current_angle",
        name="Current Angle",
        icon="mdi:rotate-right",
        native_unit_of_measurement="°",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda w: getattr(w.device, "current_angle", None),
    ),
)

SENSOR_DESC_RSSI = SesameSensorDescription(
    key="rssi",
    name="Signal Strength",
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="dBm",
    entity_category=EntityCategory.DIAGNOSTIC,
    always_available=True,
    value_fn=_get_rssi,
)

SENSOR_DESC_CONNECTION = SesameSensorDescription(
    key="connection_state",
    name="Connection State",
    icon="mdi:bluetooth-connect",
    entity_category=EntityCategory.DIAGNOSTIC,
    always_available=True,
    value_fn=lambda w: "connected" if w.device.is_connected else "disconnected",
    attrs_fn=lambda w: {
        "is_connected": w.device.is_connected,
        "is_logged_in": w.device.is_logged_in,
    },
)

SENSOR_DESCRIPTIONS_DIAGNOSTIC: tuple[SesameSensorDescription, ...] = (
    SENSOR_DESC_RSSI,
    SENSOR_DESC_CONNECTION,
)


def SesameRSSISensor(wrapper: SesameDeviceWrapper) -> SesameSensor:
    """Backward compatibility helper for tests."""
    return SesameSensor(wrapper, SENSOR_DESC_RSSI)


def SesameConnectionSensor(wrapper: SesameDeviceWrapper) -> SesameSensor:
    """Backward compatibility helper for tests."""
    return SesameSensor(wrapper, SENSOR_DESC_CONNECTION)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]

    descriptions: list[SesameSensorDescription] = list(SENSOR_DESCRIPTIONS_COMMON)

    if is_keypad_model(wrapper.model_name):
        supported_methods = get_supported_auth_methods(wrapper.model_name)
        if "card" in supported_methods:
            descriptions.append(SENSOR_DESC_CARD)
        if "fingerprint" in supported_methods:
            descriptions.append(SENSOR_DESC_FINGERPRINT)
        descriptions.append(SENSOR_DESC_PASSCODE)
        if "face" in supported_methods:
            descriptions.append(SENSOR_DESC_FACE)
        if "palm" in supported_methods:
            descriptions.append(SENSOR_DESC_PALM)
        descriptions.append(SENSOR_DESC_PAIRED_LOCKS)
    else:
        descriptions.extend(SENSOR_DESCRIPTIONS_LOCK)

    descriptions.extend(SENSOR_DESCRIPTIONS_DIAGNOSTIC)

    async_add_entities([SesameSensor(wrapper, desc) for desc in descriptions])


class SesameSensor(SensorEntity):
    """Representation of a Sesame BLE sensor entity."""

    entity_description: SesameSensorDescription

    def __init__(self, wrapper: SesameDeviceWrapper, description: SesameSensorDescription) -> None:
        """Initialize the sensor."""
        self.wrapper = wrapper
        self.device = wrapper.device
        self.entity_description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{wrapper.entry.unique_id}_{description.key}"
        self._attr_icon = description.icon
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_entity_category = description.entity_category
        self._unregister_status_callback = None
        self._init_task = None

    async def async_added_to_hass(self) -> None:
        """Register callbacks when added to Home Assistant."""
        self._unregister_status_callback = self.wrapper.register_update_listener(
            self.async_write_ha_state
        )
        if self.entity_description.init_task_fn:
            self._init_task = asyncio.create_task(self.entity_description.init_task_fn(self))

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callbacks when removed."""
        if self._unregister_status_callback:
            self._unregister_status_callback()
        if self._init_task:
            self._init_task.cancel()

    @property
    def available(self) -> bool:
        """Return true if the device is connected and logged in, or if sensor is always available."""
        if self.entity_description.always_available:
            return True
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

    @property
    def native_value(self) -> Any:
        """Return the native sensor value."""
        try:
            return self.entity_description.value_fn(self.wrapper)
        except Exception:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes if configured."""
        if self.entity_description.attrs_fn:
            try:
                return self.entity_description.attrs_fn(self.wrapper)
            except Exception:
                return {}
        return None
