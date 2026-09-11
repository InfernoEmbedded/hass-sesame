"""Platform for Sesame BLE button integration."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from . import SesameDeviceWrapper, is_keypad_model

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SesameButtonDescription(ButtonEntityDescription):
    """Class describing Sesame BLE button entities."""

    press_fn: Callable[[SesameDeviceWrapper], Coroutine[Any, Any, None]]


async def _async_press_set_locked(wrapper: SesameDeviceWrapper) -> None:
    if not wrapper.device.is_logged_in:
        raise HomeAssistantError("Device is not connected/logged in")
    current_angle = wrapper.device.current_angle
    if current_angle is None:
        raise HomeAssistantError("Current position/angle is unknown")
    unlock_position = wrapper.device.unlock_position if wrapper.device.unlock_position is not None else 0
    try:
        await wrapper.device.configure_lock_position(current_angle, unlock_position)
    except Exception as err:
        raise HomeAssistantError(f"Failed to configure locked position: {err}") from err


async def _async_press_set_unlocked(wrapper: SesameDeviceWrapper) -> None:
    if not wrapper.device.is_logged_in:
        raise HomeAssistantError("Device is not connected/logged in")
    current_angle = wrapper.device.current_angle
    if current_angle is None:
        raise HomeAssistantError("Current position/angle is unknown")
    lock_position = wrapper.device.lock_position if wrapper.device.lock_position is not None else 0
    try:
        await wrapper.device.configure_lock_position(lock_position, current_angle)
    except Exception as err:
        raise HomeAssistantError(f"Failed to configure unlocked position: {err}") from err


async def _async_press_calibrate_magnet(wrapper: SesameDeviceWrapper) -> None:
    if not wrapper.device.is_logged_in:
        raise HomeAssistantError("Device is not connected/logged in")
    try:
        await wrapper.device.calibrate_magnet()
    except Exception as err:
        raise HomeAssistantError(f"Failed to calibrate magnet: {err}") from err


BUTTON_DESCRIPTIONS: tuple[SesameButtonDescription, ...] = (
    SesameButtonDescription(
        key="set_locked_position",
        name="Set Locked Position",
        icon="mdi:lock",
        entity_category=EntityCategory.CONFIG,
        press_fn=_async_press_set_locked,
    ),
    SesameButtonDescription(
        key="set_unlocked_position",
        name="Set Unlocked Position",
        icon="mdi:lock-open",
        entity_category=EntityCategory.CONFIG,
        press_fn=_async_press_set_unlocked,
    ),
    SesameButtonDescription(
        key="calibrate_magnet",
        name="Calibrate Magnet",
        icon="mdi:compass",
        entity_category=EntityCategory.CONFIG,
        press_fn=_async_press_calibrate_magnet,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]

    # Keypads and Touch devices do not have lock positions to calibrate
    if is_keypad_model(wrapper.model_name):
        logger.debug("Skipping button setup for Sesame Touch device %s", entry.unique_id)
        return

    async_add_entities([
        SesameButton(wrapper, description) for description in BUTTON_DESCRIPTIONS
    ])


class SesameButton(ButtonEntity):
    """Representation of a Sesame BLE button entity."""

    entity_description: SesameButtonDescription

    def __init__(self, wrapper: SesameDeviceWrapper, description: SesameButtonDescription) -> None:
        """Initialize the button."""
        self.wrapper = wrapper
        self.entity_description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{wrapper.entry.unique_id}_{description.key}"

    @property
    def available(self) -> bool:
        """Return true if the device is connected and logged in."""
        return self.wrapper.device.is_logged_in

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info matching the main Sesame device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.wrapper.entry.unique_id)},
            name=f"Sesame {self.wrapper.model_name}",
            manufacturer="CANDY HOUSE",
            model=self.wrapper.model_name,
        )

    async def async_press(self) -> None:
        """Press the button."""
        await self.entity_description.press_fn(self.wrapper)
