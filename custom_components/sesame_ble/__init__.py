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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store


from .const import (
    CONF_API_KEY,
    CONF_COGNITO_POOL_ID,
    CONF_MODEL,
    CONF_SECRET_KEY,
    CONF_DEVICE_UUID,
    DOMAIN,
)
from . import firmware
from pysesame_ble import (
    COMPANY_ID,
    ProductModels,
    SesameAdData,
    SesameLock,
    SesameKeypad,
    BaseKeypad,
    is_keypad_model,
    create_sesame_device,
)

logger = logging.getLogger(__name__)

PLATFORMS = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.UPDATE,
]



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


def get_supported_auth_methods(model_name: str | None) -> list[str]:
    """Returns list of supported auth methods ('passcode', 'card', 'fingerprint', 'face', 'palm') for a given model."""
    if not model_name or not is_keypad_model(model_name):
        return []
    
    m = str(model_name).upper()

    # Face AI / Face Pro AI models: Pure AI biometrics (Face, Palm, Passcode). No NFC card, No Fingerprint scanner.
    if "AI" in m:
        return ["passcode", "face", "palm"]

    # Face 1 / Face Pro / Face 2 models: Full biometrics + NFC + Fingerprint + PIN (Face, Fingerprint, Card, Passcode).
    if "FACE" in m:
        return ["passcode", "card", "fingerprint", "face"]

    # Touch / Touch Pro / Touch 2 models: Fingerprint + Card + PIN.
    return ["passcode", "card", "fingerprint"]




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

        self.update_listeners = []
        self.store = Store(hass, 1, f"sesame_ble_{entry.entry_id}")
        self.logical_passcodes = {}
        self.logical_cards = {}
        self.logical_fingerprints = {}
        self.history_records = []
        self.scheduler_task = None
        self._sync_lock = asyncio.Lock()
        self.recently_deleted_passcodes = {}
        self.recently_deleted_cards = {}
        self.recently_deleted_fingerprints = {}
        self._latest_firmware_version: str | None = None
        self._latest_firmware_info: dict[str, Any] | None = None

        # Instantiates the correct device type using library factory
        self.device = create_sesame_device(
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
        if (
            is_keypad_model(self.model_name)
            and getattr(self.device, "is_connected", False)
            and getattr(self.device, "is_logged_in", False)
            and not getattr(self.device, "_auto_reconnect_paused", False)
            and not getattr(self, "is_updating", False)
        ):
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

    def _get_firmware_credentials(self) -> tuple[str | None, str | None]:
        """Retrieve configured firmware credentials from options, entry data, or environment."""
        api_key = self.entry.options.get(CONF_API_KEY) or self.entry.data.get(CONF_API_KEY)
        pool_id = self.entry.options.get(CONF_COGNITO_POOL_ID) or self.entry.data.get(CONF_COGNITO_POOL_ID)
        return api_key, pool_id

    @property
    def latest_firmware_version(self) -> str | None:
        """Return the latest firmware version available for this device model, or None if not fetched or unavailable."""
        return self._latest_firmware_version

    async def async_fetch_latest_firmware(self, force: bool = False) -> str | None:
        """Fetch the latest firmware version dynamically from Candy House cloud servers or local storage.

        Gracefully skips cloud checks if credentials are not configured.
        """
        product_type = firmware.get_product_type_id(self.model_name)
        if product_type is None:
            logger.warning("Could not determine productType ID for model %s", self.model_name)
            self._latest_firmware_version = None
            return None

        config_dir = getattr(self.hass.config, "config_dir", None) if hasattr(self.hass, "config") else None
        api_key, pool_id = self._get_firmware_credentials()

        try:
            version = await self.hass.async_add_executor_job(
                firmware.fetch_latest_firmware_version,
                product_type,
                force,
                api_key,
                pool_id,
                config_dir,
                self.model_name,
            )
            self._latest_firmware_version = version
            for listener in list(self.update_listeners):
                try:
                    listener()
                except Exception:
                    pass
            return version
        except firmware.FirmwareCredentialsMissingError as err:
            logger.info("Firmware check skipped for %s: %s", self.model_name, err)
            self._latest_firmware_version = None
            return None
        except Exception as err:
            self._latest_firmware_version = None
            logger.error(
                "Failed to fetch latest firmware for %s (productType %d) from Candy House servers: %s",
                self.model_name,
                product_type,
                err,
            )
            raise

    async def async_connect(self) -> None:
        """Connect and authenticate with the device."""
        try:
            await self.device.connect()
            await self.device.login()
            if is_keypad_model(self.model_name):
                await self._async_auto_pair_keypad()
        except Exception as e:
            logger.warning("Failed to connect to Sesame device %s: %s", self.ble_device.address, e)
            raise

    async def _async_auto_pair_keypad(self) -> None:
        """Check if any lock configured in Home Assistant needs to be paired to this keypad."""
        try:
            # Wait for keypad to finish receiving initial publish packets
            await asyncio.sleep(2.0)
            
            locks = [
                other_wrapper for other_entry_id, other_wrapper in self.hass.data[DOMAIN].items()
                if not hasattr(other_wrapper, "model_name") or not is_keypad_model(other_wrapper.model_name)
            ]
            
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
                    if is_keypad_model(self.model_name):
                        await self._sync_and_apply_schedules()
                    else:
                        await self.fetch_and_flush_history()
                except Exception:
                    logger.exception("Error in scheduler loop for %s", self.device.mac_address)
            await asyncio.sleep(60)

    def resolve_history_record(self, record_id: int, history_type: int, timestamp: int, tag: int, raw_param: str) -> dict[str, Any]:
        """Resolves a raw history record into human-readable details, including person linkage."""
        is_unlock = (history_type % 2 == 0) and (history_type != 0)
        event_type = "Unlock" if is_unlock else "Lock"

        method = "Manual"
        caller = "Manual"
        person_id = None

        # Resolve keypad wrapper that is paired to this lock
        keypad_wrapper = None
        for other_entry_id, other_wrapper in self.hass.data[DOMAIN].items():
            if hasattr(other_wrapper, "model_name") and is_keypad_model(other_wrapper.model_name):
                for lock_info in getattr(other_wrapper.device, "paired_locks", []):
                    if lock_info["uuid"].lower() == str(self.adv_data.device_uuid).lower():
                        keypad_wrapper = other_wrapper
                        break
                if keypad_wrapper:
                    break

        if history_type in (1, 2):
            method = "Manual"
            caller = "Manual"
        elif history_type in (3, 4):
            method = "BLE"
            caller = "Home Assistant"
        elif history_type in (5, 6):
            method = "Auto-Lock"
            caller = "Auto"
        elif history_type in (7, 8):
            method = "Web API"
            caller = "Cloud API"
        elif history_type in (11, 12):
            method = "Keypad"
            caller = "Keypad"

        if keypad_wrapper:
            if tag == 0 and "card" in get_supported_auth_methods(keypad_wrapper.model_name):
                method = "NFC Card"
                card_info = keypad_wrapper.logical_cards.get(raw_param, {})
                caller = card_info.get("name", f"NFC Card ({raw_param[:8]})")
                person_id = card_info.get("person_id")
            elif tag == 1 and "fingerprint" in get_supported_auth_methods(keypad_wrapper.model_name):
                method = "Fingerprint"
                finger_info = keypad_wrapper.logical_fingerprints.get(raw_param, {})
                caller = finger_info.get("name", f"Fingerprint ({raw_param[:8]})")
                person_id = finger_info.get("person_id")
            elif tag == 2:
                method = "Passcode"
                pass_info = keypad_wrapper.logical_passcodes.get(raw_param, {})
                caller = pass_info.get("name", f"Passcode ({raw_param[:8]})")
                person_id = pass_info.get("person_id")
            elif tag == 3 or (tag == 0 and "face" in get_supported_auth_methods(keypad_wrapper.model_name) and "card" not in get_supported_auth_methods(keypad_wrapper.model_name)):
                method = "Face"
                face_info = getattr(keypad_wrapper, "logical_faces", {}).get(raw_param, {})
                caller = face_info.get("name", f"Face ({raw_param[:8]})")
                person_id = face_info.get("person_id")
            elif tag == 4 or (tag == 1 and "palm" in get_supported_auth_methods(keypad_wrapper.model_name) and "fingerprint" not in get_supported_auth_methods(keypad_wrapper.model_name)):
                method = "Palm"
                palm_info = getattr(keypad_wrapper, "logical_palms", {}).get(raw_param, {})
                caller = palm_info.get("name", f"Palm ({raw_param[:8]})")
                person_id = palm_info.get("person_id")
            elif tag in (5, 6):  # TouchPro UUID, Touch UUID
                method = "Keypad"
                caller = keypad_wrapper.entry.title

        else:
            # Fallback when no keypad is paired or found
            name_str = None
            try:
                raw_bytes = bytes.fromhex(raw_param)
                if len(raw_bytes) >= 16:
                    try:
                        if len(raw_bytes) > 16:
                            name_str = raw_bytes[16:].decode("utf-8", errors="ignore").strip().rstrip("\x00")
                    except Exception:
                        pass
                if not name_str:
                    try:
                        name_str = raw_bytes.decode("utf-8", errors="replace").strip().rstrip("\x00")
                    except Exception:
                        name_str = raw_param
            except ValueError:
                name_str = raw_param

            if name_str and name_str != raw_param:
                caller = name_str
                if name_str == "Home Assistant":
                    method = "BLE"

        # Resolve person name if person_id is set
        if person_id:
            person_state = self.hass.states.get(person_id)
            if person_state:
                caller = person_state.name

        return {
            "record_id": record_id,
            "timestamp": timestamp,
            "event_type": event_type,
            "method": method,
            "caller": caller,
            "person_id": person_id,
        }

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

                        # Fire Home Assistant event for the new history record
                        try:
                            resolved = self.resolve_history_record(
                                r["record_id"],
                                r["type"],
                                r["timestamp"],
                                r["tag"],
                                r["raw_parameter"],
                            )
                            event_data = {
                                "device_id": self.entry.entry_id,
                                "device_name": self.entry.title,
                                "record_id": r["record_id"],
                                "event_type": resolved["event_type"],
                                "method": resolved["method"],
                                "caller": resolved["caller"],
                            }
                            if resolved["person_id"]:
                                event_data["person_id"] = resolved["person_id"]
                            self.hass.bus.async_fire("sesame_ble_event", event_data)
                        except Exception as ev_err:
                            logger.exception("Failed to fire history event: %s", ev_err)

                        # Delete OTP passcode if this is the matching use event
                        if r["tag"] == 2:  # Passcode
                            used_uid = r["raw_parameter"]
                            # Find keypad wrapper paired to this lock
                            keypad_wrapper = None
                            for other_entry_id, other_wrapper in self.hass.data[DOMAIN].items():
                                if hasattr(other_wrapper, "model_name") and is_keypad_model(other_wrapper.model_name):
                                    for lock_info in getattr(other_wrapper.device, "paired_locks", []):
                                        if lock_info["uuid"].lower() == str(self.adv_data.device_uuid).lower():
                                            keypad_wrapper = other_wrapper
                                            break
                                    if keypad_wrapper:
                                        break

                            if keypad_wrapper and used_uid in keypad_wrapper.logical_passcodes:
                                passcode_info = keypad_wrapper.logical_passcodes[used_uid]
                                if passcode_info.get("one_time"):
                                    logger.info("OTP Passcode '%s' (uid: %s) used. Deleting immediately.", passcode_info["name"], used_uid)
                                    del keypad_wrapper.logical_passcodes[used_uid]
                                    import time
                                    keypad_wrapper.recently_deleted_passcodes[used_uid] = time.time()
                                    try:
                                        await keypad_wrapper.device.delete_passcode(used_uid)
                                    except Exception as e:
                                        logger.error("Failed to delete used OTP passcode %s: %s", used_uid, e)
                                    # Trigger keypad sync to write state
                                    asyncio.create_task(keypad_wrapper._sync_and_apply_schedules())

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
        if (
            getattr(self, "is_updating", False)
            or not getattr(self.device, "is_logged_in", False)
            or getattr(self.device, "_auto_reconnect_paused", False)
        ):
            return

        mac_addr = getattr(self.device, "mac_address", getattr(getattr(self, "ble_device", None), "address", "keypad"))
        if self._sync_lock.locked():
            logger.info("[_sync_and_apply_schedules] Sync already in progress for keypad %s, skipping duplicate trigger", mac_addr)
            return

        logger.info("[_sync_and_apply_schedules] Starting sync for keypad %s", mac_addr)
        async with self._sync_lock:
            logger.info("[_sync_and_apply_schedules] Acquired sync lock for %s", mac_addr)
            # Ensure we have latest physical passcodes
            logger.debug("[_sync_and_apply_schedules] Calling get_passcodes()")
            await self.device.get_passcodes()
            logger.debug("[_sync_and_apply_schedules] Calling get_cards()")
            await self.device.get_cards()
            logger.debug("[_sync_and_apply_schedules] Calling get_fingerprints()")
            await self.device.get_fingerprints()
            logger.debug("[_sync_and_apply_schedules] Calling get_faces()")
            try:
                await self.device.get_faces()
            except NotImplementedError:
                pass
            logger.debug("[_sync_and_apply_schedules] Calling get_palms()")
            try:
                await self.device.get_palms()
            except NotImplementedError:
                pass

            physical_passcodes = self.device.passcodes
            physical_cards = self.device.cards
            physical_fingerprints = self.device.fingerprints
            physical_faces = getattr(self.device, "faces", {})
            physical_palms = getattr(self.device, "palms", {})

            logger.info(
                "[_sync_and_apply_schedules] Fetched physical data. passcodes count=%d, cards count=%d, fingerprints count=%d, faces count=%d, palms count=%d",
                len(physical_passcodes),
                len(physical_cards),
                len(physical_fingerprints),
                len(physical_faces),
                len(physical_palms)
            )


            import time
            now_ts = time.time()
            self.recently_deleted_passcodes = {
                uid: ts for uid, ts in self.recently_deleted_passcodes.items()
                if (now_ts - ts < 30.0) or (uid in physical_passcodes and now_ts - ts < 86400.0)
            }
            self.recently_deleted_cards = {
                uid: ts for uid, ts in self.recently_deleted_cards.items()
                if (now_ts - ts < 30.0) or (uid in physical_cards and now_ts - ts < 86400.0)
            }
            self.recently_deleted_fingerprints = {
                uid: ts for uid, ts in self.recently_deleted_fingerprints.items()
                if (now_ts - ts < 30.0) or (uid in physical_fingerprints and now_ts - ts < 86400.0)
            }

            now = datetime.datetime.now()
            changed = False

            # Check for OTP (One-Time Passcode) usage in the paired lock's history
            paired_lock_wrapper = None
            for other_entry_id, other_wrapper in self.hass.data[DOMAIN].items():
                if not hasattr(other_wrapper, "model_name") or not is_keypad_model(other_wrapper.model_name):
                    lock_uuid_str = str(other_wrapper.adv_data.device_uuid).lower()
                    for lock_info in getattr(self.device, "paired_locks", []):
                        if lock_info["uuid"].lower() == lock_uuid_str:
                            paired_lock_wrapper = other_wrapper
                            break
                    if paired_lock_wrapper:

                        break

            if paired_lock_wrapper:
                for record in paired_lock_wrapper.history_records:
                    if record.get("tag") == 2:  # Passcode tag
                        used_uid = record.get("raw_parameter")
                        if used_uid in self.logical_passcodes:
                            passcode_info = self.logical_passcodes[used_uid]
                            if passcode_info.get("one_time"):
                                logger.info("OTP Passcode '%s' (uid: %s) has been used. Deleting...", passcode_info["name"], used_uid)
                                del self.logical_passcodes[used_uid]
                                self.recently_deleted_passcodes[used_uid] = now_ts
                                try:
                                    await self.device.delete_passcode(used_uid)
                                except Exception as e:
                                    logger.error("Failed to delete used OTP passcode %s from physical device: %s", used_uid, e)
                                changed = True

            # 1. Sync physical passcodes to logical (added via app)
            for uid, phys_info in list(physical_passcodes.items()):
                if uid in self.recently_deleted_passcodes:
                    logger.info("[_sync_and_apply_schedules] Passcode %s was deleted logically, pushing deletion to physical device", uid)
                    try:
                        await self.device.delete_passcode(uid)
                        self.recently_deleted_passcodes[uid] = time.time()
                        changed = True
                    except Exception as e:
                        logger.warning("Failed to delete recently deleted passcode %s from physical device: %s", uid, e)
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
            for uid, phys_info in list(physical_cards.items()):
                if uid in self.recently_deleted_cards:
                    logger.info("[_sync_and_apply_schedules] Card %s was deleted logically, pushing deletion to physical device", uid)
                    try:
                        await self.device.delete_card(uid)
                        self.recently_deleted_cards[uid] = time.time()
                        changed = True
                    except Exception as e:
                        logger.warning("Failed to delete recently deleted card %s from physical device: %s", uid, e)
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
            for uid, phys_info in list(physical_fingerprints.items()):
                if uid in self.recently_deleted_fingerprints:
                    logger.info("[_sync_and_apply_schedules] Fingerprint %s was deleted logically, pushing deletion to physical device", uid)
                    try:
                        await self.device.delete_fingerprint(uid)
                        self.recently_deleted_fingerprints[uid] = time.time()
                        changed = True
                    except Exception as e:
                        logger.warning("Failed to delete recently deleted fingerprint %s from physical device: %s", uid, e)
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

    async def async_unload(self) -> None:
        """Unload entry resources."""
        if self._scheduler_task:
            self._scheduler_task.cancel()
        if self.device:
            await self.device.disconnect()

    def get_qr_code_data(self) -> dict[str, str] | None:
        """Generates QR code setup URL and metadata for importing into the official Sesame mobile app."""
        try:
            from urllib import parse
            from uuid import UUID
            from .const import CONF_SECRET_KEY, CONF_DEVICE_UUID
            from pysesame_ble import SesameQRCode, ProductModels

            secret_hex = self.entry.data.get(CONF_SECRET_KEY, "")
            if not secret_hex:
                return None
            secret_bytes = bytes.fromhex(secret_hex)

            uuid_str = self.entry.data.get(CONF_DEVICE_UUID)
            if not uuid_str and hasattr(self.device, "ad_data") and self.device.ad_data:
                uuid_str = str(self.device.ad_data.device_uuid)
            if not uuid_str and hasattr(self, "adv_data") and self.adv_data:
                uuid_str = str(self.adv_data.device_uuid)
            if not uuid_str:
                uuid_str = "00000000-0000-0000-0000-000000000000"

            device_uuid = UUID(uuid_str)
            model_name = getattr(self, "model_name", "SESAME5")
            model_id = ProductModels[model_name].value if model_name in ProductModels.__members__ else 5
            device_name = self.entry.title or model_name

            qr_obj = SesameQRCode(
                device_name=device_name,
                key_level=0,  # Owner level key
                model_id=model_id,
                device_uuid=device_uuid,
                secret_key=secret_bytes,
            )
            qr_url = qr_obj.to_url()

            qr_data_uri = None
            try:
                import io
                import base64
                import qrcode
                qr = qrcode.QRCode(version=1, box_size=8, border=2)
                qr.add_data(qr_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                b64_png = base64.b64encode(buf.getvalue()).decode("ascii")
                qr_data_uri = f"data:image/png;base64,{b64_png}"
            except Exception as e:
                logger.debug("Local QR code PNG generation fallback: %s", e)

            return {
                "qr_url": qr_url,
                "qr_image_url": qr_data_uri,
                "qr_data_uri": qr_data_uri,
                "secret_key": secret_hex,
                "device_uuid": str(device_uuid),
                "model_name": model_name,
            }

        except Exception as e:
            logger.warning("Failed to generate QR code data for %s: %s", self.entry.title, e)
            return None

    async def async_disconnect(self) -> None:
        """Disconnect from the device."""
        if self.scheduler_task:
            self.scheduler_task.cancel()
        await self.device.disconnect()


async def async_cleanup_orphaned_keypad_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove orphaned entities from entity registry if keypad device model does not support them."""
    model_name = entry.data.get(CONF_MODEL)
    if not is_keypad_model(model_name):
        return

    try:
        from homeassistant.helpers import entity_registry as er
        entity_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(entity_reg, entry.entry_id)
        supported_methods = get_supported_auth_methods(model_name)

        # Lock-only unique ID suffixes that must not exist for keypads
        lock_only_suffixes = (
            "_door",
            "_auto_lock_delay",
            "_opensensor_auto_lock_delay",
            "_set_locked_position",
            "_set_unlocked_position",
            "_calibrate_magnet",
            "_locked_position",
            "_unlocked_position",
            "_current_position",
            "_current_angle",
            "_setup_qr_code",
        )

        for entity_entry in entries:
            # 0. Lock domain cleanup for keypads
            if entity_entry.domain == "lock":
                logger.info("Removing lock entity %s from keypad device", entity_entry.entity_id)
                entity_reg.async_remove(entity_entry.entity_id)
                continue

            uid = entity_entry.unique_id

            # 1. Lock-only entity cleanup
            if any(uid.endswith(s) for s in lock_only_suffixes):
                logger.info("Removing orphaned lock-only entity %s (%s) from keypad device", entity_entry.entity_id, uid)
                entity_reg.async_remove(entity_entry.entity_id)
                continue


            # 2. Unsupported auth count sensor cleanup
            if uid.endswith("_registered_cards") and "card" not in supported_methods:
                logger.info("Removing orphaned cards sensor %s (%s) from keypad", entity_entry.entity_id, uid)
                entity_reg.async_remove(entity_entry.entity_id)
            elif uid.endswith("_registered_fingerprints") and "fingerprint" not in supported_methods:
                logger.info("Removing orphaned fingerprints sensor %s (%s) from keypad", entity_entry.entity_id, uid)
                entity_reg.async_remove(entity_entry.entity_id)
            elif uid.endswith("_registered_faces") and "face" not in supported_methods:
                logger.info("Removing orphaned faces sensor %s (%s) from keypad", entity_entry.entity_id, uid)
                entity_reg.async_remove(entity_entry.entity_id)
            elif uid.endswith("_registered_palms") and "palm" not in supported_methods:
                logger.info("Removing orphaned palms sensor %s (%s) from keypad", entity_entry.entity_id, uid)
                entity_reg.async_remove(entity_entry.entity_id)
    except Exception as e:
        logger.warning("Failed to clean up orphaned keypad entities: %s", e)


def resolve_target_wrapper(hass: HomeAssistant, target_id: str) -> "SesameDeviceWrapper":
    """Resolves a target identifier (entity_id, device_id, unique_id, UUID, MAC, entry_id) to a SesameDeviceWrapper."""
    if not target_id:
        raise HomeAssistantError("Missing device or entity ID")

    target_id = str(target_id).strip()
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device_entry = None
    target_wrapper = None

    # 1. Check if target_id is an entity_id in the entity registry
    ent_entry = ent_reg.async_get(target_id)
    if ent_entry and ent_entry.device_id:
        device_entry = dev_reg.async_get(ent_entry.device_id)

    # 2. Check if target_id is directly a device_id in the device registry
    if not device_entry:
        device_entry = dev_reg.async_get(target_id)

    # 3. If device_entry found, resolve its wrapper from config_entries
    if device_entry:
        for config_entry_id in device_entry.config_entries:
            if config_entry_id in hass.data.get(DOMAIN, {}):
                target_wrapper = hass.data[DOMAIN][config_entry_id]
                break

    # 4. If not found via device registry, check matching entry_id, unique_id, MAC, UUID, or title
    if not target_wrapper:
        for entry_id, w in hass.data.get(DOMAIN, {}).items():
            if not hasattr(w, "model_name"):
                continue
            if entry_id == target_id:
                target_wrapper = w
                break
            if hasattr(w, "entry") and (
                getattr(w.entry, "unique_id", None) == target_id
                or getattr(w.entry, "title", None) == target_id
                or getattr(w.entry, "entry_id", None) == target_id
            ):
                target_wrapper = w
                break
            if hasattr(w, "adv_data") and (
                str(getattr(w.adv_data, "device_uuid", "")).lower() == target_id.lower()
                or str(getattr(w.device, "mac_address", "")).lower() == target_id.lower()
            ):
                target_wrapper = w
                break

    if not target_wrapper:
        raise HomeAssistantError(f"Could not resolve Sesame device from '{target_id}'")

    return target_wrapper


def resolve_target_keypad(hass: HomeAssistant, target_id: str) -> "SesameDeviceWrapper":
    """Resolves a target identifier to a keypad wrapper.

    If the target is a lock entity/device, it automatically finds the keypad paired to that lock.
    """
    try:
        wrapper = resolve_target_wrapper(hass, target_id)
    except HomeAssistantError:
        # Fallback: check if there is only 1 keypad configured in Home Assistant
        keypads = [
            w for k, w in hass.data.get(DOMAIN, {}).items()
            if hasattr(w, "model_name") and is_keypad_model(w.model_name)
        ]
        if len(keypads) == 1:
            return keypads[0]
        raise

    if is_keypad_model(wrapper.model_name):
        return wrapper

    # The resolved device is a lock (e.g. SESAME5, SESAME6_PRO). Find the keypad paired with this lock.
    lock_uuid = str(getattr(wrapper.adv_data, "device_uuid", "")).lower()
    for entry_id, other_w in hass.data.get(DOMAIN, {}).items():
        if not hasattr(other_w, "model_name"):
            continue
        if is_keypad_model(other_w.model_name):
            for lock_info in getattr(other_w.device, "paired_locks", []):
                if str(lock_info.get("uuid", "")).lower() == lock_uuid:
                    return other_w

    # If not explicitly paired yet, but there is only 1 keypad in HA, return it as the logical paired keypad
    keypads = [
        w for k, w in hass.data.get(DOMAIN, {}).items()
        if hasattr(w, "model_name") and is_keypad_model(w.model_name)
    ]
    if len(keypads) == 1:
        return keypads[0]

    raise HomeAssistantError(
        f"Device '{target_id}' is a {wrapper.model_name} lock, but no paired keypad was found."
    )


def resolve_target_lock(hass: HomeAssistant, target_id: str) -> "SesameDeviceWrapper":
    """Resolves a target identifier to a lock wrapper."""
    wrapper = resolve_target_wrapper(hass, target_id)
    if is_keypad_model(wrapper.model_name):
        raise HomeAssistantError(
            f"Device '{target_id}' is a keypad ({wrapper.model_name}), expected a lock device or entity."
        )
    return wrapper


def resolve_passcode_uid(target_wrapper: "SesameDeviceWrapper", code_or_id: Any) -> str | None:
    """Resolves a passcode PIN, UID, or numeric ID to the matching hex UID.

    Checks logical_passcodes first (by UID, code, or zero-padded code/UID),
    then checks device.passcodes, and falls back to byte-encoding or device._resolve_code.
    """
    if code_or_id is None:
        return None

    code_str = str(code_or_id).strip()
    if not code_str:
        return None

    logical_passcodes = getattr(target_wrapper, "logical_passcodes", {}) if target_wrapper else {}
    device = getattr(target_wrapper, "device", None) if target_wrapper else None
    physical_passcodes = getattr(device, "passcodes", {}) if device else {}

    # 1. Exact UID match in logical_passcodes
    if code_str in logical_passcodes:
        return code_str
    for uid in logical_passcodes:
        if uid.lower() == code_str.lower():
            return uid

    # 2. Exact match on 'code' field in logical_passcodes
    for uid, info in logical_passcodes.items():
        if isinstance(info, dict) and str(info.get("code", "")).strip() == code_str:
            return uid

    # 3. If numeric, check zero-padded variants in logical_passcodes
    if code_str.isdigit():
        padded_6 = code_str.zfill(6)
        # Check 'code' matching padded_6
        for uid, info in logical_passcodes.items():
            if isinstance(info, dict) and str(info.get("code", "")).strip() == padded_6:
                return uid
        # Check UID matching padded_6 directly
        if padded_6 in logical_passcodes:
            return padded_6
        for uid in logical_passcodes:
            if uid.lower() == padded_6.lower():
                return uid
        # Check UID matching byte-hex representation of padded_6 (e.g. '072764' -> '000702070604')
        padded_hex = bytes(int(c) for c in padded_6).hex()
        if padded_hex in logical_passcodes:
            return padded_hex
        for uid in logical_passcodes:
            if uid.lower() == padded_hex.lower():
                return uid

        # Check UID matching byte-hex of raw code_str (e.g. '1234' -> '01020304')
        raw_hex = bytes(int(c) for c in code_str).hex()
        if raw_hex in logical_passcodes:
            return raw_hex
        for uid in logical_passcodes:
            if uid.lower() == raw_hex.lower():
                return uid

    # 4. Check active passcodes on physical device
    if code_str in physical_passcodes:
        return code_str
    for uid in physical_passcodes:
        if uid.lower() == code_str.lower():
            return uid
    for uid, info in physical_passcodes.items():
        if isinstance(info, dict) and str(info.get("code", "")).strip() == code_str:
            return uid

    if code_str.isdigit():
        padded_6 = code_str.zfill(6)
        for uid, info in physical_passcodes.items():
            if isinstance(info, dict) and str(info.get("code", "")).strip() == padded_6:
                return uid
        if padded_6 in physical_passcodes:
            return padded_6
        for uid in physical_passcodes:
            if uid.lower() == padded_6.lower():
                return uid
        padded_hex = bytes(int(c) for c in padded_6).hex()
        if padded_hex in physical_passcodes:
            return padded_hex
        for uid in physical_passcodes:
            if uid.lower() == padded_hex.lower():
                return uid
        raw_hex = bytes(int(c) for c in code_str).hex()
        if raw_hex in physical_passcodes:
            return raw_hex
        for uid in physical_passcodes:
            if uid.lower() == raw_hex.lower():
                return uid

    # 5. Delegate to device._resolve_code if available and produces valid result
    if hasattr(device, "_resolve_code"):
        try:
            res = device._resolve_code(code_str)
            if isinstance(res, (bytes, bytearray)):
                return res.hex()
            if isinstance(res, str):
                return res
        except Exception:
            pass

    # 6. Fallback for numeric PIN
    if code_str.isdigit():
        return bytes(int(c) for c in code_str).hex()

    return code_str


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
    entry.async_on_unload(entry.add_update_listener(update_listener))

    views_reg_key = f"{DOMAIN}_views_registered"
    if views_reg_key not in hass.data:
        hass.data[views_reg_key] = True
        from .views import (
            SesamePasscodesView,
            SesameCardsView,
            SesameFingerprintsView,
            SesameFacesView,
            SesamePalmsView,
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
            hass.http.register_view(SesameFacesView(hass))
        except Exception:
            pass
        try:
            hass.http.register_view(SesamePalmsView(hass))
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

    # Clean up orphaned entities from entity registry if keypad device model does not support them
    await async_cleanup_orphaned_keypad_entities(hass, entry)

    # Register passcode services if this is a keypad device

    if is_keypad_model(model_name):

        async def handle_add_passcode(call: ServiceCall) -> None:
            """Service to add a passcode to a Sesame Touch."""
            target_id = call.data.get("device_id") or call.data.get("entity_id")
            code = call.data["passcode"]
            name = call.data["name"]
            start = call.data.get("start", "").strip()
            end = call.data.get("end", "").strip()
            days = call.data.get("days", [])
            time_start = call.data.get("time_start", "").strip()
            time_end = call.data.get("time_end", "").strip()
            one_time = bool(call.data.get("one_time", False))
            person_id = call.data.get("person_id")
            if person_id == "":
                person_id = None

            # Validate datetimes
            if start:
                start_dt = parse_datetime(start)
                if not start_dt:
                    raise HomeAssistantError(f"Invalid start datetime format: '{start}'. Use YYYY-MM-DD HH:MM")
            if end:
                end_dt = parse_datetime(end)
                if not end_dt:
                    raise HomeAssistantError(f"Invalid end datetime format: '{end}'. Use YYYY-MM-DD HH:MM")

            # Validate time_start and time_end
            if time_start:
                try:
                    h, m = map(int, time_start.split(":"))
                    if not (0 <= h <= 23 and 0 <= m <= 59):
                        raise ValueError()
                except ValueError:
                    raise HomeAssistantError(f"Invalid daily start time format: '{time_start}'. Use HH:MM")
            if time_end:
                try:
                    h, m = map(int, time_end.split(":"))
                    if not (0 <= h <= 23 and 0 <= m <= 59):
                        raise ValueError()
                except ValueError:
                    raise HomeAssistantError(f"Invalid daily end time format: '{time_end}'. Use HH:MM")

            target_wrapper: SesameDeviceWrapper = resolve_target_keypad(hass, target_id)
            sesame_touch: SesameKeypad = target_wrapper.device

            # Check for duplicate passcode name
            for other_uid, other_info in target_wrapper.logical_passcodes.items():
                if other_info["name"].strip().lower() == name.lower():
                    raise HomeAssistantError(f"A passcode named '{name}' already exists")

            if not sesame_touch.is_logged_in:
                # Attempt to connect/login if not connected
                await target_wrapper.async_connect()

            try:
                # Update logical database immediately
                uid = bytes(int(c) for c in code).hex()
                target_wrapper.logical_passcodes[uid] = {
                    "name": name,
                    "code": code,
                    "start": start,
                    "end": end,
                    "days": [int(d) for d in days] if days else [],
                    "time_start": time_start,
                    "time_end": time_end,
                    "one_time": one_time,
                    "person_id": person_id,
                }
                await target_wrapper.store.async_save({
                    "passcodes": target_wrapper.logical_passcodes,
                    "cards": target_wrapper.logical_cards,
                    "fingerprints": target_wrapper.logical_fingerprints
                })

                # If it's already active physically (e.g. renamed), update name on physical device
                if sesame_touch.is_logged_in and uid in sesame_touch.passcodes:
                    try:
                        await sesame_touch.update_passcode_name(uid, name)
                    except Exception as e:
                        logger.warning("Failed to rename passcode on physical device: %s", e)

                # Force immediate sync & schedule evaluation
                await target_wrapper._sync_and_apply_schedules()
                target_wrapper._handle_status_update(sesame_touch, sesame_touch.mech_status)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to add passcode: {ex}") from ex


        async def handle_delete_passcode(call: ServiceCall) -> None:
            """Service to delete a passcode from a Sesame Touch."""
            target_id = call.data.get("device_id") or call.data.get("entity_id")
            code_or_id = call.data["passcode_or_id"]

            target_wrapper: SesameDeviceWrapper = resolve_target_keypad(hass, target_id)
            sesame_touch: SesameKeypad = target_wrapper.device

            # Resolve the UID first before updating databases or attempting connection
            resolved_uid = resolve_passcode_uid(target_wrapper, code_or_id)
            target_code_or_id = resolved_uid or str(code_or_id).strip()

            import time
            now_ts = time.time()

            # 1. Update logical database and storage immediately
            if resolved_uid:
                target_wrapper.recently_deleted_passcodes[resolved_uid] = now_ts
                if resolved_uid in target_wrapper.logical_passcodes:
                    del target_wrapper.logical_passcodes[resolved_uid]

            raw_str = str(code_or_id).strip()
            if raw_str in target_wrapper.logical_passcodes:
                target_wrapper.recently_deleted_passcodes[raw_str] = now_ts
                del target_wrapper.logical_passcodes[raw_str]

            try:
                await target_wrapper.store.async_save({
                    "passcodes": target_wrapper.logical_passcodes,
                    "cards": target_wrapper.logical_cards,
                    "fingerprints": target_wrapper.logical_fingerprints,
                })
            except Exception as store_err:
                logger.error("Failed to save updated passcodes to storage: %s", store_err)

            # 2. Attempt bounded connection if not already logged in
            if not getattr(sesame_touch, "is_logged_in", False):
                try:
                    await asyncio.wait_for(target_wrapper.async_connect(), timeout=6.0)
                except Exception as conn_err:
                    logger.warning(
                        "Keypad %s is offline or unreachable (%s); passcode %s will be deleted when reconnected",
                        getattr(getattr(target_wrapper, "ble_device", None), "address", getattr(target_wrapper, "model_name", "keypad")),
                        conn_err,
                        target_code_or_id,
                    )

            # 3. If connected and logged in, delete from physical device with bounded timeout
            if getattr(sesame_touch, "is_logged_in", False):
                try:
                    await asyncio.wait_for(sesame_touch.delete_passcode(target_code_or_id), timeout=6.0)
                    try:
                        await asyncio.wait_for(sesame_touch.get_passcodes(), timeout=6.0)
                    except Exception as refresh_err:
                        logger.debug("Failed to refresh passcodes after deletion: %s", refresh_err)
                except Exception as del_err:
                    logger.warning(
                        "Keypad was connected but failed to delete passcode %s immediately (%s); sync will retry on reconnect",
                        target_code_or_id,
                        del_err,
                    )

            # 4. Notify UI / entity listeners of state update
            try:
                target_wrapper._handle_status_update(sesame_touch, getattr(sesame_touch, "mech_status", None))
            except Exception:
                pass

        async def handle_update_passcode(call: ServiceCall) -> None:
            """Service to rename a passcode on a Sesame Touch."""
            target_id = call.data.get("device_id") or call.data.get("entity_id")
            code_or_id = call.data["passcode_or_id"]
            name = call.data["name"]

            target_wrapper: SesameDeviceWrapper = resolve_target_keypad(hass, target_id)
            sesame_touch: SesameKeypad = target_wrapper.device

            # Check for duplicate passcode name
            resolved_uid = resolve_passcode_uid(target_wrapper, code_or_id)
            target_code_or_id = resolved_uid or str(code_or_id).strip()

            for other_uid, other_info in target_wrapper.logical_passcodes.items():
                if other_uid != resolved_uid and other_info["name"].strip().lower() == name.lower():
                    raise HomeAssistantError(f"A passcode named '{name}' already exists")

            if not getattr(sesame_touch, "is_logged_in", False):
                try:
                    await asyncio.wait_for(target_wrapper.async_connect(), timeout=6.0)
                except Exception as conn_err:
                    logger.warning("Failed to connect to keypad for passcode update: %s", conn_err)

            try:
                if getattr(sesame_touch, "is_logged_in", False):
                    await asyncio.wait_for(sesame_touch.update_passcode_name(target_code_or_id, name), timeout=6.0)
                
                # Update logical database immediately
                if resolved_uid and resolved_uid in target_wrapper.logical_passcodes:
                    target_wrapper.logical_passcodes[resolved_uid]["name"] = name
                    await target_wrapper.store.async_save({
                        "passcodes": target_wrapper.logical_passcodes,
                        "cards": target_wrapper.logical_cards,
                        "fingerprints": target_wrapper.logical_fingerprints
                    })
                
                if getattr(sesame_touch, "is_logged_in", False):
                    try:
                        await asyncio.wait_for(sesame_touch.get_passcodes(), timeout=6.0)
                    except Exception:
                        pass
                target_wrapper._handle_status_update(sesame_touch, getattr(sesame_touch, "mech_status", None))
            except Exception as ex:
                raise HomeAssistantError(f"Failed to update passcode name: {ex}") from ex

        async def handle_pair_lock(call: ServiceCall) -> None:
            """Service to pair a Sesame Lock to a Sesame Touch keypad."""
            keypad_id = call.data.get("device_id") or call.data.get("entity_id")
            lock_id = call.data.get("lock_device_id") or call.data.get("lock_entity_id") or call.data.get("lock_id")

            target_wrapper: SesameDeviceWrapper = resolve_target_keypad(hass, keypad_id)
            sesame_touch: SesameKeypad = target_wrapper.device

            lock_wrapper: SesameDeviceWrapper = resolve_target_lock(hass, lock_id)

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
            keypad_id = call.data.get("device_id") or call.data.get("entity_id")
            lock_uuid_str = call.data["lock_uuid"]

            target_wrapper: SesameDeviceWrapper = resolve_target_keypad(hass, keypad_id)
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


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)

