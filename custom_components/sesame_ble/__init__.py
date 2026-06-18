"""The Candy House Sesame BLE integration."""

import asyncio
import logging
import datetime
from typing import Any, Callable

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
    BaseKeypad,
)

logger = logging.getLogger(__name__)

PLATFORMS = [Platform.LOCK, Platform.SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.BINARY_SENSOR]


def parse_datetime(dt_str: str) -> datetime.datetime | None:
    """Helper to parse datetime strings in a robust way."""
    if not dt_str:
        return None
    dt_str = dt_str.strip()
    try:
        return datetime.datetime.fromisoformat(dt_str)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(dt_str, fmt)
        except ValueError:
            pass
    return None


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
        self.store = None
        self.logical_passcodes = {}
        self.logical_cards = {}
        self.logical_fingerprints = {}
        self.history_records = []
        self.scheduler_task = None
        self._sync_lock = asyncio.Lock()
        self.recently_deleted_passcodes = {}
        self.recently_deleted_cards = {}
        self.recently_deleted_fingerprints = {}

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
        logger.info(
            "[_handle_status_update] Device %s status updated. card_reg_mode=%s, fp_reg_mode=%s, passcode_reg_mode=%s, scanned_card=%s, scanned_fp=%s, scanned_passcode=%s",
            device.mac_address,
            getattr(device, "card_registration_mode", None),
            getattr(device, "fingerprint_registration_mode", None),
            getattr(device, "passcode_registration_mode", None),
            getattr(device, "scanned_card", None),
            getattr(device, "scanned_fingerprint", None),
            getattr(device, "scanned_passcode", None)
        )
        # Notify Home Assistant entities to write their state
        for listener in self.update_listeners:
            listener()

        # Trigger immediate sync for keypads to make Home Assistant instantly aware of updates (e.g. card/fingerprint scans)
        if "TOUCH" in self.model_name:
            if self._sync_lock.locked():
                logger.info("[_handle_status_update] Sync already in progress for keypad %s, skipping immediate trigger", device.mac_address)
            else:
                logger.info("[_handle_status_update] Triggering immediate database sync for keypad %s", device.mac_address)
                asyncio.create_task(self._sync_and_apply_schedules())

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
            if "TOUCH" in self.model_name:
                await self._async_auto_pair_keypad()
        except Exception as e:
            logger.warning("Failed to connect to Sesame device %s: %s", self.ble_device.address, e)
            raise

    async def _async_auto_pair_keypad(self) -> None:
        """Check if any lock configured in Home Assistant needs to be paired to this keypad."""
        try:
            # Wait for keypad to finish receiving initial publish packets
            await asyncio.sleep(2.0)
            
            locks = []
            for other_entry_id, other_wrapper in self.hass.data[DOMAIN].items():
                if other_entry_id == "views_registered":
                    continue
                if not hasattr(other_wrapper, "model_name") or "TOUCH" not in other_wrapper.model_name:
                    locks.append(other_wrapper)
            
            if not locks:
                logger.debug("Auto-pairing: No locks registered in sesame_ble integration yet.")
                return

            lock_wrapper = locks[0]
            lock_uuid = lock_wrapper.adv_data.device_uuid
            
            is_paired = False
            for lock_info in getattr(self.device, "paired_locks", []):
                if str(lock_info["uuid"]).lower() == str(lock_uuid).lower():
                    is_paired = True
                    break
            
            if not is_paired:
                logger.info(
                    "Auto-pairing keypad %s to lock %s (UUID: %s)...",
                    self.device.mac_address,
                    lock_wrapper.entry.title,
                    lock_uuid,
                )
                secret_key_bytes = bytes.fromhex(lock_wrapper.secret_key)
                await self.device.add_paired_lock(lock_uuid, secret_key_bytes)
                logger.info("Auto-pairing command sent successfully.")
            else:
                logger.debug("Keypad %s is already paired to lock %s.", self.device.mac_address, lock_uuid)
        except Exception as e:
            logger.warning("Failed to execute auto-pairing: %s", e)


    async def _async_connect_background(self) -> None:
        """Connect in background and start reconnect loop if it fails."""
        try:
            await self.async_connect()
        except Exception:
            try:
                await self.device.disconnect()
            except Exception:
                pass
            if self.device._reconnect_limit and (not self.device._reconnect_task or self.device._reconnect_task.done()):
                self.device._reconnect_task = asyncio.create_task(self.device._auto_reconnect())

    async def _scheduler_loop(self) -> None:
        """Background loop to sync schedules/fetch history periodically."""
        while True:
            if self.device.is_logged_in:
                try:
                    if "TOUCH" in self.model_name:
                        await self._sync_and_apply_schedules()
                    else:
                        await self.fetch_and_flush_history()
                except Exception:
                    logger.exception("Error in scheduler loop for %s", self.device.mac_address)
            await asyncio.sleep(60)

    async def fetch_and_flush_history(self) -> None:
        """Fetch history records from the lock, update the local database, and save to store."""
        if not hasattr(self.device, "fetch_and_flush_history"):
            return

        async with self._sync_lock:
            try:
                new_records = await self.device.fetch_and_flush_history()
                if not new_records:
                    return

                # Merge new records into local database
                existing_ids = {r["record_id"] for r in self.history_records}
                added_any = False
                for r in new_records:
                    if r["record_id"] not in existing_ids:
                        self.history_records.append(r)
                        existing_ids.add(r["record_id"])
                        added_any = True

                if added_any:
                    # Sort by timestamp descending (newest first)
                    self.history_records.sort(key=lambda x: x["timestamp"], reverse=True)
                    # Cap at 500 entries
                    self.history_records = self.history_records[:500]
                    # Save to store
                    await self.store.async_save({
                        "passcodes": self.logical_passcodes,
                        "cards": self.logical_cards,
                        "fingerprints": self.logical_fingerprints,
                        "history": self.history_records,
                    })
                    logger.debug("Successfully saved %d history records to store", len(self.history_records))
            except Exception as e:
                logger.warning("Failed to fetch/flush history for %s: %s", self.device.mac_address, e)

    async def _sync_and_apply_schedules(self) -> None:
        """Sync with physical keypad and apply passcode schedules."""
        if not isinstance(self.device, BaseKeypad):
            return

        logger.info("[_sync_and_apply_schedules] Starting sync for keypad %s", self.device.mac_address)
        async with self._sync_lock:
            logger.info("[_sync_and_apply_schedules] Acquired sync lock for %s", self.device.mac_address)
            # Ensure we have latest physical passcodes
            logger.debug("[_sync_and_apply_schedules] Calling get_passcodes()")
            await self.device.get_passcodes()
            logger.debug("[_sync_and_apply_schedules] Calling get_cards()")
            await self.device.get_cards()
            logger.debug("[_sync_and_apply_schedules] Calling get_fingerprints()")
            await self.device.get_fingerprints()
            
            physical_passcodes = self.device.passcodes
            physical_cards = self.device.cards
            physical_fingerprints = self.device.fingerprints

            logger.info(
                "[_sync_and_apply_schedules] Fetched physical data. passcodes count=%d, cards count=%d, fingerprints count=%d",
                len(physical_passcodes),
                len(physical_cards),
                len(physical_fingerprints)
            )

            import time
            now_ts = time.time()
            self.recently_deleted_passcodes = {uid: ts for uid, ts in self.recently_deleted_passcodes.items() if now_ts - ts < 30.0}
            self.recently_deleted_cards = {uid: ts for uid, ts in self.recently_deleted_cards.items() if now_ts - ts < 30.0}
            self.recently_deleted_fingerprints = {uid: ts for uid, ts in self.recently_deleted_fingerprints.items() if now_ts - ts < 30.0}

            now = datetime.datetime.now()
            changed = False

            # 1. Sync physical passcodes to logical (added via app)
            for uid, phys_info in physical_passcodes.items():
                if uid in self.recently_deleted_passcodes:
                    continue
                if uid not in self.logical_passcodes:
                    if getattr(self.device, "passcode_registration_mode", False):
                        logger.info("[_sync_and_apply_schedules] Skipping auto-adding physical passcode %s to logical list because passcode registration mode is active", uid)
                        if not getattr(self.device, "scanned_passcode", None) or self.device.scanned_passcode.get("uid") != uid:
                            self.device.scanned_passcode = {
                                "uid": uid,
                                "code": phys_info["code"],
                                "type": phys_info.get("type", 0),
                            }
                            logger.info("[_sync_and_apply_schedules] Set scanned_passcode in wrapper: %s", self.device.scanned_passcode)
                        continue
                    logger.info("[_sync_and_apply_schedules] Auto-adding new physical passcode %s (%s) to logical list", uid, phys_info["name"])
                    self.logical_passcodes[uid] = {
                        "name": phys_info["name"],
                        "code": phys_info["code"],
                        "start": "",
                        "end": "",
                    }
                    changed = True
                    
            # Sync physical cards to logical
            for uid, phys_info in physical_cards.items():
                if uid in self.recently_deleted_cards:
                    continue
                if uid not in self.logical_cards:
                    if getattr(self.device, "card_registration_mode", False):
                        logger.info("[_sync_and_apply_schedules] Skipping auto-adding physical card %s to logical list because card registration mode is active", uid)
                        if not getattr(self.device, "scanned_card", None) or self.device.scanned_card.get("uid") != uid:
                            self.device.scanned_card = {
                                "uid": uid,
                                "type": phys_info["type"],
                            }
                            logger.info("[_sync_and_apply_schedules] Set scanned_card in wrapper: %s", self.device.scanned_card)
                        continue
                    logger.info("[_sync_and_apply_schedules] Auto-adding physical card %s (%s) to logical list", uid, phys_info["name"])
                    self.logical_cards[uid] = {
                        "name": phys_info["name"],
                        "type": phys_info["type"],
                    }
                    changed = True
                    
            # Clean up deleted logical cards
            for uid in list(self.logical_cards.keys()):
                if uid not in physical_cards:
                    logger.info("[_sync_and_apply_schedules] Removing logical card %s because it was deleted physically", uid)
                    del self.logical_cards[uid]
                    changed = True

            # Sync physical fingerprints to logical
            for uid, phys_info in physical_fingerprints.items():
                if uid in self.recently_deleted_fingerprints:
                    continue
                if uid not in self.logical_fingerprints:
                    if getattr(self.device, "fingerprint_registration_mode", False):
                        logger.info("[_sync_and_apply_schedules] Skipping auto-adding physical fingerprint %s to logical list because fingerprint registration mode is active", uid)
                        if not getattr(self.device, "scanned_fingerprint", None) or self.device.scanned_fingerprint.get("uid") != uid:
                            self.device.scanned_fingerprint = {
                                "uid": uid,
                                "type": phys_info["type"],
                            }
                            logger.info("[_sync_and_apply_schedules] Set scanned_fingerprint in wrapper: %s", self.device.scanned_fingerprint)
                        continue
                    logger.info("[_sync_and_apply_schedules] Auto-adding physical fingerprint %s (%s) to logical list", uid, phys_info["name"])
                    self.logical_fingerprints[uid] = {
                        "name": phys_info["name"],
                        "type": phys_info["type"],
                    }
                    changed = True

            # Clean up deleted logical fingerprints
            for uid in list(self.logical_fingerprints.keys()):
                if uid not in physical_fingerprints:
                    logger.info("[_sync_and_apply_schedules] Removing logical fingerprint %s because it was deleted physically", uid)
                    del self.logical_fingerprints[uid]
                    changed = True

            # 2. Evaluate schedules via virtualization or hardware implementation
            if await self.device.apply_passcode_schedules(self.logical_passcodes):
                changed = True

            if changed:
                await self.store.async_save({
                    "passcodes": self.logical_passcodes,
                    "cards": self.logical_cards,
                    "fingerprints": self.logical_fingerprints
                })
                self._handle_status_update(self.device, self.device.mech_status)

    async def async_disconnect(self) -> None:
        """Disconnect from the device."""
        if self.scheduler_task:
            self.scheduler_task.cancel()
        await self.device.disconnect()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Candy House Sesame BLE from a config entry."""
    mac_address = entry.data["mac_address"]
    secret_key = entry.data[CONF_SECRET_KEY]
    model_name = entry.data[CONF_MODEL]

    if not entry.unique_id:
        hass.config_entries.async_update_entry(entry, unique_id=dr.format_mac(mac_address))
        return False

    # Retrieve the BLEDevice from Home Assistant's bluetooth manager
    ble_device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)
    if not ble_device:
        from bleak.backends.device import BLEDevice
        logger.warning(
            "Sesame BLE device not found in Bluetooth cache: %s. Using fallback BLEDevice.",
            mac_address,
        )
        ble_device = BLEDevice(mac_address, name=f"Sesame Fallback ({mac_address[-5:]})", details={})


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

    from homeassistant.helpers.storage import Store
    wrapper.store = Store(hass, 1, f"sesame_ble_{entry.entry_id}_passcodes")
    stored_data = await wrapper.store.async_load()
    if stored_data:
        wrapper.logical_passcodes = stored_data.get("passcodes", {})
        wrapper.logical_cards = stored_data.get("cards", {})
        wrapper.logical_fingerprints = stored_data.get("fingerprints", {})
        wrapper.history_records = stored_data.get("history", [])
    else:
        wrapper.logical_passcodes = {}
        wrapper.logical_cards = {}
        wrapper.logical_fingerprints = {}
        wrapper.history_records = []

    # Run background scheduler for both locks (history polling) and keypads (schedules & status polling)
    wrapper.scheduler_task = asyncio.create_task(wrapper._scheduler_loop())

    try:
        # Connect to lock/touch in a background task so Home Assistant setup doesn't block
        asyncio.create_task(wrapper._async_connect_background())
    except Exception as e:
        raise ConfigEntryNotReady(f"Failed to initiate connection: {e}") from e

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = wrapper

    # Register APIs, static path, and custom sidebar panel (once)
    if "views_registered" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["views_registered"] = True
        from .views import (
            SesamePasscodesView,
            SesameCardsView,
            SesameFingerprintsView,
            SesameCardsRegisterView,
            SesameFingerprintsRegisterView,
            SesamePasscodesRegisterView,
            SesameCardsAddView,
            SesameLockHistoryView,
            SesameKeypadPairView,
            SesameKeypadUnpairView,
        )
        try:
            hass.http.register_view(SesamePasscodesView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameCardsView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameFingerprintsView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameCardsRegisterView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameFingerprintsRegisterView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesamePasscodesRegisterView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameCardsAddView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameLockHistoryView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameKeypadPairView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesameKeypadUnpairView(hass))
        except Exception:
            pass

        import os
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        os.makedirs(static_dir, exist_ok=True)
        try:
            from homeassistant.components.http import StaticPathConfig
            await hass.http.async_register_static_paths([
                StaticPathConfig("/sesame_static", static_dir, cache_headers=False)
            ])
        except ImportError:
            try:
                hass.http.register_static_path("/sesame_static", static_dir, cache_headers=False)
            except Exception as err:
                logger.exception("Failed to register static path with fallback: %s", err)
        except Exception as err:
            logger.exception("Failed to register static path: %s", err)

        try:
            from homeassistant.components import panel_custom
            await panel_custom.async_register_panel(
                hass,
                frontend_url_path="keypads",
                webcomponent_name="sesame-keypad-panel",
                sidebar_title="Keypad Manager",
                sidebar_icon="mdi:dialpad",
                module_url="/sesame_static/panel.js",
                require_admin=False,
            )
        except ImportError:
            pass
        except Exception as err:
            logger.exception("Failed to register Keypad Manager panel: %s", err)

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

            # Check for duplicate passcode name
            for other_uid, other_info in target_wrapper.logical_passcodes.items():
                if other_info["name"].strip().lower() == name.lower():
                    raise HomeAssistantError(f"A passcode named '{name}' already exists")

            if not sesame_touch.is_logged_in:
                # Attempt to connect/login if not connected
                await target_wrapper.async_connect()

            try:
                await sesame_touch.add_passcode(code, name)
                # Update logical database immediately
                uid = bytes(int(c) for c in code).hex()
                target_wrapper.logical_passcodes[uid] = {
                    "name": name,
                    "code": code,
                    "start": "",
                    "end": "",
                }
                await target_wrapper.store.async_save({
                    "passcodes": target_wrapper.logical_passcodes,
                    "cards": target_wrapper.logical_cards,
                    "fingerprints": target_wrapper.logical_fingerprints
                })
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
                
                # Update logical database immediately
                resolved_uid = None
                try:
                    id_bytes = sesame_touch._resolve_code(code_or_id)
                    resolved_uid = id_bytes.hex()
                except Exception:
                    pass
                    
                if resolved_uid:
                    import time
                    target_wrapper.recently_deleted_passcodes[resolved_uid] = time.time()
                    if resolved_uid in target_wrapper.logical_passcodes:
                        del target_wrapper.logical_passcodes[resolved_uid]
                        await target_wrapper.store.async_save({
                            "passcodes": target_wrapper.logical_passcodes,
                            "cards": target_wrapper.logical_cards,
                            "fingerprints": target_wrapper.logical_fingerprints
                        })
                
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

            # Check for duplicate passcode name
            resolved_uid = None
            try:
                id_bytes = sesame_touch._resolve_code(code_or_id)
                resolved_uid = id_bytes.hex()
            except Exception:
                pass

            for other_uid, other_info in target_wrapper.logical_passcodes.items():
                if other_uid != resolved_uid and other_info["name"].strip().lower() == name.lower():
                    raise HomeAssistantError(f"A passcode named '{name}' already exists")

            if not sesame_touch.is_logged_in:
                await target_wrapper.async_connect()

            try:
                await sesame_touch.update_passcode_name(code_or_id, name)
                
                # Update logical database immediately
                if resolved_uid and resolved_uid in target_wrapper.logical_passcodes:
                    target_wrapper.logical_passcodes[resolved_uid]["name"] = name
                    await target_wrapper.store.async_save({
                        "passcodes": target_wrapper.logical_passcodes,
                        "cards": target_wrapper.logical_cards,
                        "fingerprints": target_wrapper.logical_fingerprints
                    })
                
                await sesame_touch.get_passcodes()
                target_wrapper._handle_status_update(sesame_touch, sesame_touch.mech_status)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to update passcode name: {ex}") from ex

        async def handle_pair_lock(call: ServiceCall) -> None:
            """Service to pair a Sesame Lock to a Sesame Touch keypad."""
            device_id = call.data["device_id"]
            lock_device_id = call.data["lock_device_id"]

            dev_reg = dr.async_get(hass)
            
            # Resolve keypad wrapper
            device_entry = dev_reg.async_get(device_id)
            if not device_entry:
                raise HomeAssistantError("Invalid keypad device ID")

            target_entry_id = None
            for config_entry_id in device_entry.config_entries:
                if config_entry_id in hass.data[DOMAIN]:
                    target_entry_id = config_entry_id
                    break

            if not target_entry_id:
                raise HomeAssistantError("Keypad is not configured or active")

            target_wrapper: SesameDeviceWrapper = hass.data[DOMAIN][target_entry_id]
            sesame_touch: SesameKeypad = target_wrapper.device

            # Resolve lock wrapper
            lock_device_entry = dev_reg.async_get(lock_device_id)
            if not lock_device_entry:
                raise HomeAssistantError("Invalid lock device ID")

            lock_entry_id = None
            for config_entry_id in lock_device_entry.config_entries:
                if config_entry_id in hass.data[DOMAIN]:
                    lock_entry_id = config_entry_id
                    break

            if not lock_entry_id:
                raise HomeAssistantError("Lock is not configured or active")

            lock_wrapper: SesameDeviceWrapper = hass.data[DOMAIN][lock_entry_id]

            if not sesame_touch.is_logged_in:
                await target_wrapper.async_connect()

            try:
                from uuid import UUID
                lock_uuid = lock_wrapper.adv_data.device_uuid
                secret_key_bytes = bytes.fromhex(lock_wrapper.secret_key)
                
                await sesame_touch.add_paired_lock(lock_uuid, secret_key_bytes)
                # Force status update notification to HA entities
                target_wrapper._handle_status_update(sesame_touch, sesame_touch.mech_status)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to pair lock: {ex}") from ex

        async def handle_unpair_lock(call: ServiceCall) -> None:
            """Service to unpair a Sesame Lock from a Sesame Touch keypad."""
            device_id = call.data["device_id"]
            lock_uuid_str = call.data["lock_uuid"]

            dev_reg = dr.async_get(hass)
            device_entry = dev_reg.async_get(device_id)
            if not device_entry:
                raise HomeAssistantError("Invalid keypad device ID")

            target_entry_id = None
            for config_entry_id in device_entry.config_entries:
                if config_entry_id in hass.data[DOMAIN]:
                    target_entry_id = config_entry_id
                    break

            if not target_entry_id:
                raise HomeAssistantError("Keypad is not configured or active")

            target_wrapper: SesameDeviceWrapper = hass.data[DOMAIN][target_entry_id]
            sesame_touch: SesameKeypad = target_wrapper.device

            if not sesame_touch.is_logged_in:
                await target_wrapper.async_connect()

            try:
                from uuid import UUID
                lock_uuid = UUID(lock_uuid_str)
                await sesame_touch.remove_paired_lock(lock_uuid)
                # Force status update notification to HA entities
                target_wrapper._handle_status_update(sesame_touch, sesame_touch.mech_status)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to unpair lock: {ex}") from ex

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
        hass.services.async_register(
            DOMAIN, "pair_lock", handle_pair_lock
        )
        hass.services.async_register(
            DOMAIN, "unpair_lock", handle_unpair_lock
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        wrapper: SesameDeviceWrapper = hass.data[DOMAIN].pop(entry.entry_id)
        await wrapper.async_disconnect()

    return unload_ok
