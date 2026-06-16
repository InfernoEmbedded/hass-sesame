"""The Candy House Sesame BLE integration."""

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import CONF_MODEL, CONF_SECRET_KEY, CONF_DEVICE_UUID, DOMAIN
from .sesame_client import (
    COMPANY_ID,
    ProductModels,
    SesameAdData,
    SesameLock,
    SesameKeypad,
)

logger = logging.getLogger(__name__)

PLATFORMS = [Platform.LOCK, Platform.SENSOR]


class SesameDeviceWrapper:
    """Wrapper class to coordinate connection and updates for Sesame BLE devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        ble_device,
        adv_data: SesameAdData,
        secret_key: str,
        model_name: str,
    ) -> None:
        """Initialize the wrapper."""
        self.hass = hass
        self.entry = entry
        self.ble_device = ble_device
        self.adv_data = adv_data
        self.secret_key = secret_key
        self.model_name = model_name
        self.device = None
        self.update_listeners = []

        # Instantiates the correct device type
        if "TOUCH" in model_name:
            self.device = SesameKeypad(
                ble_device,
                adv_data,
                secret_key,
                status_callback=self._handle_status_update,
                reconnect_attempts=5,
            )
        else:
            self.device = SesameLock(
                ble_device,
                adv_data,
                secret_key,
                status_callback=self._handle_status_update,
                reconnect_attempts=5,
            )

    def _handle_status_update(self, device: Any, status: Any) -> None:
        """Callback for device mechanical status updates."""
        logger.debug("Received mechanical status update from %s", device.mac_address)
        # Notify Home Assistant entities to write their state
        for listener in self.update_listeners:
            listener()

    def register_update_listener(self, listener: Any) -> Callable[[], None]:
        """Register a listener for state updates."""
        self.update_listeners.append(listener)

        def remove_listener():
            self.update_listeners.remove(listener)

        return remove_listener

    async def async_connect(self) -> None:
        """Connect and authenticate with the device."""
        try:
            await self.device.connect()
            await self.device.login()
        except Exception as e:
            logger.warning("Failed to connect to Sesame device %s: %s", self.ble_device.address, e)
            raise

    async def _async_connect_background(self) -> None:
        """Connect in background and start reconnect loop if it fails."""
        try:
            await self.async_connect()
        except Exception:
            if self.device._reconnect_limit and (not self.device._reconnect_task or self.device._reconnect_task.done()):
                self.device._reconnect_task = asyncio.create_task(self.device._auto_reconnect())

    async def async_disconnect(self) -> None:
        """Disconnect from the device."""
        await self.device.disconnect()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Candy House Sesame BLE from a config entry."""
    mac_address = entry.data["mac_address"]
    secret_key = entry.data[CONF_SECRET_KEY]
    model_name = entry.data[CONF_MODEL]

    # Retrieve the BLEDevice from Home Assistant's bluetooth manager
    ble_device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)
    if not ble_device:
        logger.warning("Sesame BLE device not found in Bluetooth cache: %s. Will retry.", mac_address)
        # We fail setup and HA will retry automatically when discovered
        raise ConfigEntryNotReady(f"Device {mac_address} not discovered yet")

    # Resolve advertisement data
    adv_data = None
    bluetooth_adv = bluetooth.async_last_service_info(hass, mac_address)
    if bluetooth_adv and COMPANY_ID in bluetooth_adv.manufacturer_data:
        mfg_data = bluetooth_adv.manufacturer_data[COMPANY_ID]
        try:
            adv_data = SesameAdData.decode(mfg_data)
        except Exception:
            pass

    if adv_data is None:
        # Fallback placeholder
        import uuid
        product_model = ProductModels[model_name]
        adv_data = SesameAdData(
            model_id=product_model.value,
            is_registered=True,
            device_uuid=uuid.UUID(entry.data.get(CONF_DEVICE_UUID, "00000000-0000-0000-0000-000000000000")),
        )

    wrapper = SesameDeviceWrapper(
        hass,
        entry,
        ble_device,
        adv_data,
        secret_key,
        model_name,
    )

    try:
        # Connect to lock/touch in a background task so Home Assistant setup doesn't block
        asyncio.create_task(wrapper._async_connect_background())
    except Exception as e:
        raise ConfigEntryNotReady(f"Failed to initiate connection: {e}") from e

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = wrapper

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register passcode services if this is a Sesame Touch/Touch Pro device
    if "TOUCH" in model_name:
        async def handle_add_passcode(call: ServiceCall) -> None:
            """Service to add a passcode to a Sesame Touch."""
            device_id = call.data["device_id"]
            code = call.data["passcode"]
            name = call.data["name"]

            # Resolve the config entry / wrapper from the device registry ID
            dev_reg = dr.async_get(hass)
            device_entry = dev_reg.async_get(device_id)
            if not device_entry:
                raise HomeAssistantError("Invalid device ID")

            # Find matching config entry
            target_entry_id = None
            for config_entry_id in device_entry.config_entries:
                if config_entry_id in hass.data[DOMAIN]:
                    target_entry_id = config_entry_id
                    break

            if not target_entry_id:
                raise HomeAssistantError("Device is not configured or active")

            target_wrapper: SesameDeviceWrapper = hass.data[DOMAIN][target_entry_id]
            sesame_touch: SesameKeypad = target_wrapper.device

            if not sesame_touch.is_logged_in:
                # Attempt to connect/login if not connected
                await target_wrapper.async_connect()

            try:
                await sesame_touch.add_passcode(code, name)
                # Force refresh passcodes list
                await sesame_touch.get_passcodes()
                target_wrapper._handle_status_update(sesame_touch, sesame_touch.mech_status)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to add passcode: {ex}") from ex

        async def handle_delete_passcode(call: ServiceCall) -> None:
            """Service to delete a passcode from a Sesame Touch."""
            device_id = call.data["device_id"]
            code_or_id = call.data["passcode_or_id"]

            dev_reg = dr.async_get(hass)
            device_entry = dev_reg.async_get(device_id)
            if not device_entry:
                raise HomeAssistantError("Invalid device ID")

            target_entry_id = None
            for config_entry_id in device_entry.config_entries:
                if config_entry_id in hass.data[DOMAIN]:
                    target_entry_id = config_entry_id
                    break

            if not target_entry_id:
                raise HomeAssistantError("Device is not configured or active")

            target_wrapper: SesameDeviceWrapper = hass.data[DOMAIN][target_entry_id]
            sesame_touch: SesameKeypad = target_wrapper.device

            if not sesame_touch.is_logged_in:
                await target_wrapper.async_connect()

            try:
                await sesame_touch.delete_passcode(code_or_id)
                await sesame_touch.get_passcodes()
                target_wrapper._handle_status_update(sesame_touch, sesame_touch.mech_status)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to delete passcode: {ex}") from ex

        async def handle_update_passcode(call: ServiceCall) -> None:
            """Service to rename a passcode on a Sesame Touch."""
            device_id = call.data["device_id"]
            code_or_id = call.data["passcode_or_id"]
            name = call.data["name"]

            dev_reg = dr.async_get(hass)
            device_entry = dev_reg.async_get(device_id)
            if not device_entry:
                raise HomeAssistantError("Invalid device ID")

            target_entry_id = None
            for config_entry_id in device_entry.config_entries:
                if config_entry_id in hass.data[DOMAIN]:
                    target_entry_id = config_entry_id
                    break

            if not target_entry_id:
                raise HomeAssistantError("Device is not configured or active")

            target_wrapper: SesameDeviceWrapper = hass.data[DOMAIN][target_entry_id]
            sesame_touch: SesameKeypad = target_wrapper.device

            if not sesame_touch.is_logged_in:
                await target_wrapper.async_connect()

            try:
                await sesame_touch.update_passcode_name(code_or_id, name)
                await sesame_touch.get_passcodes()
                target_wrapper._handle_status_update(sesame_touch, sesame_touch.mech_status)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to update passcode name: {ex}") from ex

        # Register services
        hass.services.async_register(
            DOMAIN, "add_passcode", handle_add_passcode
        )
        hass.services.async_register(
            DOMAIN, "delete_passcode", handle_delete_passcode
        )
        hass.services.async_register(
            DOMAIN, "update_passcode", handle_update_passcode
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        wrapper: SesameDeviceWrapper = hass.data[DOMAIN].pop(entry.entry_id)
        await wrapper.async_disconnect()

    return unload_ok
