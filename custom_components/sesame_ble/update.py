"""Update platform for Sesame BLE integration."""

import asyncio
import logging
from typing import Any

from bleak import BleakClient
from homeassistant.components import bluetooth
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
from .firmware import download_firmware_zip, get_product_type_id
from .nordic_dfu import DFU_SERVICE_UUID, get_bootloader_mac, perform_nordic_dfu

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
        self._attr_update_percentage = None

    async def async_added_to_hass(self) -> None:
        """Register update listener when added to Home Assistant."""
        self._unregister_status_callback = self.wrapper.register_update_listener(
            self.async_write_ha_state
        )
        hass = getattr(self, "hass", None) or getattr(self.wrapper, "hass", None)
        if self.wrapper.latest_firmware_version is None and hass:
            hass.async_create_task(self.async_update())

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update listener."""
        if self._unregister_status_callback:
            self._unregister_status_callback()

    @property
    def available(self) -> bool:
        """Return true if the device is connected and logged in, or if an update is in progress."""
        if self._in_progress:
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
            sw_version=self.installed_version,
        )

    @property
    def installed_version(self) -> str | None:
        """Return the installed firmware version on the Sesame device."""
        return self.device.firmware_version

    @property
    def latest_version(self) -> str | None:
        """Return the latest available firmware version from Candy House servers, or None if unavailable."""
        return self.wrapper.latest_firmware_version

    @property
    def release_summary(self) -> str | None:
        """Return summary of release notes."""
        installed = self.installed_version
        latest = self.latest_version
        if latest is None:
            return "Firmware update information unavailable."
        if installed and installed != latest:
            return f"A new firmware update ({latest}) is available for Sesame {self.wrapper.model_name}."
        if installed and installed == latest:
            return "Firmware is up to date."
        return "Checking for firmware updates."

    async def async_update(self) -> None:
        """Fetch latest firmware release dynamically from Candy House cloud servers."""
        try:
            await self.wrapper.async_fetch_latest_firmware(force=True)
        except Exception as err:
            logger.warning(
                "Unable to fetch latest firmware update for %s from Candy House servers: %s",
                self.wrapper.model_name,
                err,
            )
        self.async_write_ha_state()

    @property
    def in_progress(self) -> bool:
        """Return whether a firmware update / DFU mode switch is in progress."""
        return self._in_progress

    @property
    def update_percentage(self) -> int | None:
        """Return the update progress percentage (0-100) or None."""
        return self._attr_update_percentage

    async def _async_find_dfu_target(self, original_mac: str, timeout: float = 30.0) -> Any:
        """Finds the Nordic DFU bootloader target device."""
        from bleak.backends.device import BLEDevice
        inc_mac = get_bootloader_mac(original_mac).upper()
        orig_mac = original_mac.upper()

        hass = getattr(self, "hass", None) or getattr(self.wrapper, "hass", None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        if hass:
            while loop.time() < deadline:
                # 1. Check active BLE advertisements for DFU service or DFU name
                for service_info in bluetooth.async_discovered_service_info(hass, connectable=True):
                    s_addr = service_info.address.upper()
                    uuids = [str(u).lower() for u in getattr(service_info, "service_uuids", [])]
                    is_dfu_uuid = any(u in uuids for u in (DFU_SERVICE_UUID.lower(), "fe59", "0000fe59-0000-1000-8000-00805f9b34fb"))
                    is_dfu_name = bool(service_info.name and "dfu" in service_info.name.lower())

                    if (is_dfu_uuid or is_dfu_name) and s_addr in (orig_mac, inc_mac):
                        logger.info("Found DFU bootloader target via advertisement (%s, %s) matching %s", service_info.name, uuids, s_addr)
                        return service_info.device

                    if is_dfu_uuid or is_dfu_name:
                        logger.info("Found DFU bootloader target via advertisement (%s, %s) at %s", service_info.name, uuids, s_addr)
                        return service_info.device

                # 2. Check for connectable device in Bluetooth cache (Sesame bootloader keeps orig_mac; standard Nordic unbonded uses inc_mac)
                for addr in (orig_mac, inc_mac):
                    dev = (
                        bluetooth.async_ble_device_from_address(hass, addr, connectable=True)
                        or bluetooth.async_ble_device_from_address(hass, addr.lower(), connectable=True)
                    )
                    if dev:
                        logger.info("Found DFU bootloader target by connectable address: %s", addr)
                        return dev

                await asyncio.sleep(0.5)

            # Check if any scanner saw orig_mac or inc_mac even if connectable flag hasn't settled
            for addr in (orig_mac, inc_mac):
                dev = (
                    bluetooth.async_ble_device_from_address(hass, addr, connectable=False)
                    or bluetooth.async_ble_device_from_address(hass, addr.lower(), connectable=False)
                )
                if dev:
                    logger.info("Found DFU bootloader target via scanner (non-connectable cache): %s", addr)
                    return dev

        logger.warning(
            "DFU bootloader not detected in Bluetooth scan after %.1fs. Attempting direct connection to %s",
            timeout,
            orig_mac,
        )
        return BLEDevice(orig_mac, name="DfuTarg", details={})

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install firmware update by initiating BLE DFU mode on the device and transferring image."""
        self._in_progress = True
        self._attr_update_percentage = 0
        self.async_write_ha_state()

        product_type = (
            get_product_type_id(getattr(self.device, "product_model", None))
            or get_product_type_id(getattr(self.wrapper, "model_name", None))
        )
        if product_type is None:
            self._in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()
            raise ValueError(f"Unknown or unsupported Sesame product model: {self.wrapper.model_name}")

        hass = getattr(self, "hass", None) or getattr(self.wrapper, "hass", None)
        self.wrapper.is_updating = True
        try:
            logger.info("Starting firmware update process for %s (productType=%d)", self.device.address, product_type)
            self._attr_update_percentage = 2
            self.async_write_ha_state()

            # 1. Download official DFU zip package from Candy House cloud or local storage
            logger.info("Locating firmware zip package for %s...", self.wrapper.model_name)
            api_key, pool_id = self.wrapper._get_firmware_credentials()
            config_dir = getattr(hass.config, "config_dir", None) if (hass and hasattr(hass, "config")) else None
            if hass:
                target_version, zip_bytes = await hass.async_add_executor_job(
                    download_firmware_zip,
                    product_type,
                    True,
                    api_key,
                    pool_id,
                    config_dir,
                    self.wrapper.model_name,
                )
            else:
                target_version, zip_bytes = download_firmware_zip(
                    product_type,
                    True,
                    api_key=api_key,
                    pool_id=pool_id,
                    config_dir=config_dir,
                    model_name=self.wrapper.model_name,
                )
            logger.info("Firmware package ready: version %s (%d bytes)", target_version, len(zip_bytes))
            self._attr_update_percentage = 5
            self.async_write_ha_state()

            # 2. Pause auto-reconnect on Sesame device and any other Sesame devices to free ESP32 BLE proxy airtime
            other_devices = []
            if hass and DOMAIN in hass.data:
                for entry_id, wrapper in hass.data[DOMAIN].items():
                    dev = getattr(wrapper, "device", None)
                    if dev and dev != self.device and hasattr(dev, "pause_auto_reconnect"):
                        dev.pause_auto_reconnect()
                        if getattr(dev, "is_connected", False):
                            try:
                                await dev.disconnect()
                            except Exception:
                                pass
                        other_devices.append(dev)

            if hasattr(self.device, "pause_auto_reconnect"):
                self.device.pause_auto_reconnect()

            # 3. Enter DFU bootloader mode
            if not self.device.is_connected:
                logger.info("Connecting to %s to send ENABLE_DFU command...", self.device.address)
                try:
                    await self.device.connect()
                except Exception as err:
                    logger.info("Could not connect to Sesame GATT (device may already be in DFU bootloader mode): %s", err)

            if self.device.is_connected:
                logger.info("Sending ENABLE_DFU command to %s", self.device.address)
                try:
                    await self.device.enable_dfu()
                    await asyncio.sleep(0.5)
                except Exception as err:
                    logger.warning("Error sending enable_dfu (device may already be entering bootloader): %s", err)

                try:
                    await self.device.disconnect()
                except Exception:
                    pass

            self._attr_update_percentage = 10
            self.async_write_ha_state()

            # 4. Search and connect to Nordic DFU target
            # Allow bootloader time to reboot, initialize BLE radio, and advertise cleanly
            logger.info("Waiting 3.0s for device to enter DFU bootloader mode...")
            await asyncio.sleep(3.0)

            logger.info("Searching for Nordic DFU bootloader target for %s...", self.device.address)
            target_device = await self._async_find_dfu_target(self.device.address, timeout=30.0)
            logger.info("Connecting to DFU bootloader at %s...", target_device.address)

            # Clear BLE services cache before connecting so the new DFU GATT table is discovered
            try:
                import bleak_retry_connector
                if hasattr(bleak_retry_connector, "clear_cache"):
                    res = bleak_retry_connector.clear_cache(target_device.address)
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass

            client = None
            try:
                from bleak_retry_connector import establish_connection
                client = await establish_connection(
                    BleakClient,
                    target_device,
                    f"sesame_dfu_{target_device.address}",
                    max_attempts=3,
                    use_services_cache=False,
                )
            except (ModuleNotFoundError, ImportError, TypeError):
                client = BleakClient(target_device)
                await client.connect()

            # 5. Perform Nordic Secure DFU transfer
            def progress_callback(pct: float) -> None:
                # Map 0..100% of DFU transfer to 10..95% of overall progress
                overall_pct = int(10 + (pct * 0.85))
                if overall_pct != self._attr_update_percentage:
                    self._attr_update_percentage = overall_pct
                    self.async_write_ha_state()

            logger.info("Executing Nordic Secure DFU update...")
            try:
                await perform_nordic_dfu(client, zip_bytes, progress_callback=progress_callback)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            self._attr_update_percentage = 100
            self.async_write_ha_state()
            logger.info("Firmware update successfully completed on %s!", self.device.address)

            if target_version and hasattr(self.device, "_firmware_version"):
                self.device._firmware_version = target_version

            # Wait for bootloader to reboot into updated application
            await asyncio.sleep(4.0)

            # Clear BLE services cache for application mode so it rediscovers services
            try:
                import bleak_retry_connector
                if hasattr(bleak_retry_connector, "clear_cache"):
                    res = bleak_retry_connector.clear_cache(self.device.address)
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass

            # Resume auto-reconnection and trigger reconnect
            if hasattr(self.device, "resume_auto_reconnect"):
                self.device.resume_auto_reconnect()
            if hasattr(self.device, "_auto_reconnect"):
                asyncio.create_task(self.device._auto_reconnect())

        except Exception as err:
            logger.error("Firmware update failed on %s: %s", self.device.address, err)
            if hasattr(self.device, "resume_auto_reconnect"):
                self.device.resume_auto_reconnect()
            if hasattr(self.device, "_auto_reconnect"):
                asyncio.create_task(self.device._auto_reconnect())
            raise
        finally:
            for dev in other_devices:
                if hasattr(dev, "resume_auto_reconnect"):
                    dev.resume_auto_reconnect()
                if hasattr(dev, "_auto_reconnect"):
                    asyncio.create_task(dev._auto_reconnect())
            self.wrapper.is_updating = False
            self._in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()
