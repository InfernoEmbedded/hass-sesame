"""API endpoints for managing Sesame Keypad passcodes, cards, and fingerprints."""

import asyncio
import json
import logging
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from . import SesameDeviceWrapper, parse_datetime
from .sesame_client import BaseKeypad

logger = logging.getLogger(__name__)


class SesamePasscodesView(HomeAssistantView):
    """View to handle passcode management."""

    url = "/api/sesame_ble/passcodes"
    name = "api:sesame_ble:passcodes"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Retrieve all keypad devices, passcodes, and lock info."""
        if DOMAIN not in self.hass.data:
            return self.json({"keypads": [], "all_locks": []})

        all_locks = []
        for entry_id, other_wrapper in self.hass.data[DOMAIN].items():
            if entry_id == "views_registered":
                continue
            if not hasattr(other_wrapper, "model_name") or "TOUCH" not in other_wrapper.model_name:
                all_locks.append({
                    "entry_id": entry_id,
                    "name": other_wrapper.entry.title,
                    "uuid": str(other_wrapper.adv_data.device_uuid),
                })

        keypads_list = []
        for entry_id, wrapper in self.hass.data[DOMAIN].items():
            if not hasattr(wrapper, "model_name") or "TOUCH" not in wrapper.model_name:
                continue

            # Trigger background sync of physical keypad in a safe asyncio task,
            # so the HTTP GET request returns instantly and doesn't block the UI.
            if wrapper.device and wrapper.device.is_logged_in:
                import time
                now = time.time()
                if now - getattr(wrapper, "_last_sync_time", 0) > 15.0 and not wrapper._sync_lock.locked():
                    wrapper._last_sync_time = now
                    asyncio.create_task(wrapper._sync_and_apply_schedules())

            passcodes_data = []
            for uid, info in wrapper.logical_passcodes.items():
                passcodes_data.append({
                    "uid": uid,
                    "name": info["name"],
                    "code": info["code"],
                    "start": info.get("start", ""),
                    "end": info.get("end", ""),
                    "is_physical": uid in wrapper.device.passcodes,
                })

            cards_data = []
            for uid, info in wrapper.logical_cards.items():
                cards_data.append({
                    "uid": uid,
                    "name": info["name"],
                    "type": info["type"],
                })

            fingerprints_data = []
            for uid, info in wrapper.logical_fingerprints.items():
                fingerprints_data.append({
                    "uid": uid,
                    "name": info["name"],
                    "type": info["type"],
                })

            paired_locks_data = []
            if hasattr(wrapper.device, "paired_locks"):
                for lock_info in wrapper.device.paired_locks:
                    lock_uuid_str = lock_info["uuid"]
                    status_code = lock_info["status"]
                    
                    configured_name = "Unknown Lock"
                    configured_entry_id = None
                    for other_entry_id, other_wrapper in self.hass.data[DOMAIN].items():
                        if other_entry_id == "views_registered":
                            continue
                        if str(other_wrapper.adv_data.device_uuid) == lock_uuid_str:
                            configured_name = other_wrapper.entry.title
                            configured_entry_id = other_entry_id
                            break
                            
                    paired_locks_data.append({
                        "uuid": lock_uuid_str,
                        "status": status_code,
                        "name": configured_name,
                        "entry_id": configured_entry_id,
                    })

            keypads_list.append({
                "entry_id": entry_id,
                "name": f"Sesame {wrapper.model_name} ({wrapper.ble_device.name or wrapper.ble_device.address})",
                "mac_address": wrapper.ble_device.address,
                "is_connected": wrapper.device.is_connected,
                "is_logged_in": wrapper.device.is_logged_in,
                "passcodes": passcodes_data,
                "cards": cards_data,
                "fingerprints": fingerprints_data,
                "paired_locks": paired_locks_data,
                "scanned_card": wrapper.device.scanned_card if getattr(wrapper.device, "scanned_card", None) else None,
                "scanned_fingerprint": wrapper.device.scanned_fingerprint if getattr(wrapper.device, "scanned_fingerprint", None) else None,
            })

        return self.json({"keypads": keypads_list, "all_locks": all_locks})

    async def post(self, request: web.Request) -> web.Response:
        """Add or update a passcode schedule."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]
        name = data.get("name", "").strip()
        code = data.get("code", "").strip()
        start = data.get("start", "").strip()
        end = data.get("end", "").strip()

        if not name:
            return self.json({"error": "Name is required"}, status_code=400)
        if not code or not code.isdigit() or len(code) < 4 or len(code) > 16:
            return self.json({"error": "PIN must be between 4 and 16 digits"}, status_code=400)

        # Validate datetimes
        if start:
            start_dt = parse_datetime(start)
            if not start_dt:
                return self.json({"error": f"Invalid start datetime format: '{start}'. Use YYYY-MM-DD HH:MM"}, status_code=400)
        if end:
            end_dt = parse_datetime(end)
            if not end_dt:
                return self.json({"error": f"Invalid end datetime format: '{end}'. Use YYYY-MM-DD HH:MM"}, status_code=400)

        # Resolve unique hex ID from PIN
        uid = bytes(int(c) for c in code).hex()
        old_uid = data.get("uid")

        # Check for duplicate passcode name
        for other_uid, other_info in wrapper.logical_passcodes.items():
            if other_uid != old_uid and other_info["name"].strip().lower() == name.lower():
                return self.json({"error": f"A passcode named '{name}' already exists"}, status_code=400)

        try:
            # If we are updating and PIN changed, delete old code
            if old_uid and old_uid != uid:
                if old_uid in wrapper.logical_passcodes:
                    del wrapper.logical_passcodes[old_uid]
                if wrapper.device and wrapper.device.is_logged_in:
                    try:
                        await wrapper.device.delete_passcode(old_uid)
                    except Exception as e:
                        logger.warning("Failed to delete old passcode %s from physical device: %s", old_uid, e)

            # Update/Add to logical
            wrapper.logical_passcodes[uid] = {
                "name": name,
                "code": code,
                "start": start,
                "end": end,
            }

            # Save to store
            await wrapper.store.async_save({
                "passcodes": wrapper.logical_passcodes,
                "cards": wrapper.logical_cards,
                "fingerprints": wrapper.logical_fingerprints
            })

            # Force immediate sync
            asyncio.create_task(wrapper._sync_and_apply_schedules())

            return self.json({"success": True, "uid": uid})
        except Exception as e:
            return self.json({"error": f"Failed to save passcode: {e}"}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """Delete a passcode."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        uid = data.get("uid")

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not uid:
            return self.json({"error": "UID is required"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]

        try:
            import time
            wrapper.recently_deleted_passcodes[uid] = time.time()

            if uid in wrapper.logical_passcodes:
                del wrapper.logical_passcodes[uid]
                await wrapper.store.async_save({
                    "passcodes": wrapper.logical_passcodes,
                    "cards": wrapper.logical_cards,
                    "fingerprints": wrapper.logical_fingerprints
                })

            if wrapper.device and wrapper.device.is_logged_in:
                try:
                    await wrapper.device.delete_passcode(uid)
                except Exception as e:
                    logger.warning("Failed to delete passcode %s from physical device: %s", uid, e)

            # Force immediate sync
            asyncio.create_task(wrapper._sync_and_apply_schedules())

            return self.json({"success": True})
        except Exception as e:
            return self.json({"error": f"Failed to delete passcode: {e}"}, status_code=500)


class SesameCardsView(HomeAssistantView):
    """View to handle card management (renaming and deleting)."""

    url = "/api/sesame_ble/cards"
    name = "api:sesame_ble:cards"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Update a card name."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        uid = data.get("uid")
        name = data.get("name", "").strip()

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not uid:
            return self.json({"error": "Card UID is required"}, status_code=400)
        if not name:
            return self.json({"error": "Name is required"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]

        # Check for duplicate card name
        for other_uid, other_info in wrapper.logical_cards.items():
            if other_uid != uid and other_info["name"].strip().lower() == name.lower():
                return self.json({"error": f"An NFC card named '{name}' already exists"}, status_code=400)

        try:
            if uid in wrapper.logical_cards:
                wrapper.logical_cards[uid]["name"] = name
                await wrapper.store.async_save({
                    "passcodes": wrapper.logical_passcodes,
                    "cards": wrapper.logical_cards,
                    "fingerprints": wrapper.logical_fingerprints
                })
                if wrapper.device and wrapper.device.is_logged_in:
                    try:
                        await wrapper.device.update_card_name(uid, name)
                        await wrapper.device.get_cards()
                    except Exception as e:
                        logger.warning("Failed to rename card on physical device: %s", e)
                wrapper._handle_status_update(wrapper.device, wrapper.device.mech_status)
                return self.json({"success": True})
            else:
                return self.json({"error": "Card not found"}, status_code=404)
        except Exception as e:
            return self.json({"error": f"Failed to rename card: {e}"}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """Delete a card."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        uid = data.get("uid")

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not uid:
            return self.json({"error": "Card UID is required"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]

        try:
            import time
            wrapper.recently_deleted_cards[uid] = time.time()

            if uid in wrapper.logical_cards:
                del wrapper.logical_cards[uid]
                await wrapper.store.async_save({
                    "passcodes": wrapper.logical_passcodes,
                    "cards": wrapper.logical_cards,
                    "fingerprints": wrapper.logical_fingerprints
                })

            if wrapper.device and wrapper.device.is_logged_in:
                try:
                    await wrapper.device.delete_card(uid)
                except Exception as e:
                    logger.warning("Failed to delete card %s from physical device: %s", uid, e)

            asyncio.create_task(wrapper._sync_and_apply_schedules())
            return self.json({"success": True})
        except Exception as e:
            return self.json({"error": f"Failed to delete card: {e}"}, status_code=500)


class SesameFingerprintsView(HomeAssistantView):
    """View to handle fingerprint management (renaming and deleting)."""

    url = "/api/sesame_ble/fingerprints"
    name = "api:sesame_ble:fingerprints"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Update a fingerprint name."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        uid = data.get("uid")
        name = data.get("name", "").strip()

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not uid:
            return self.json({"error": "Fingerprint UID is required"}, status_code=400)
        if not name:
            return self.json({"error": "Name is required"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]

        # Check for duplicate fingerprint name
        for other_uid, other_info in wrapper.logical_fingerprints.items():
            if other_uid != uid and other_info["name"].strip().lower() == name.lower():
                return self.json({"error": f"A fingerprint named '{name}' already exists"}, status_code=400)

        try:
            if uid in wrapper.logical_fingerprints:
                wrapper.logical_fingerprints[uid]["name"] = name
                await wrapper.store.async_save({
                    "passcodes": wrapper.logical_passcodes,
                    "cards": wrapper.logical_cards,
                    "fingerprints": wrapper.logical_fingerprints
                })
                if wrapper.device and wrapper.device.is_logged_in:
                    try:
                        await wrapper.device.update_fingerprint_name(uid, name)
                        await wrapper.device.get_fingerprints()
                    except Exception as e:
                        logger.warning("Failed to rename fingerprint on physical device: %s", e)
                wrapper._handle_status_update(wrapper.device, wrapper.device.mech_status)
                return self.json({"success": True})
            else:
                return self.json({"error": "Fingerprint not found"}, status_code=404)
        except Exception as e:
            return self.json({"error": f"Failed to rename fingerprint: {e}"}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """Delete a fingerprint."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        uid = data.get("uid")

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not uid:
            return self.json({"error": "Fingerprint UID is required"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]

        try:
            import time
            wrapper.recently_deleted_fingerprints[uid] = time.time()

            if uid in wrapper.logical_fingerprints:
                del wrapper.logical_fingerprints[uid]
                await wrapper.store.async_save({
                    "passcodes": wrapper.logical_passcodes,
                    "cards": wrapper.logical_cards,
                    "fingerprints": wrapper.logical_fingerprints
                })

            if wrapper.device and wrapper.device.is_logged_in:
                try:
                    await wrapper.device.delete_fingerprint(uid)
                except Exception as e:
                    logger.warning("Failed to delete fingerprint %s from physical device: %s", uid, e)

            asyncio.create_task(wrapper._sync_and_apply_schedules())
            return self.json({"success": True})
        except Exception as e:
            return self.json({"error": f"Failed to delete fingerprint: {e}"}, status_code=500)


class SesameCardsRegisterView(HomeAssistantView):
    """View to handle card registration mode (start/stop/status)."""

    url = "/api/sesame_ble/cards/register"
    name = "api:sesame_ble:cards:register"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Start or stop card registration mode, or clear scanned card."""
        try:
            data = await request.json()
        except ValueError:
            logger.warning("SesameCardsRegisterView: Invalid JSON payload received")
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        action = data.get("action")  # "start", "stop"
        logger.info("SesameCardsRegisterView: received POST request: entry_id=%s, action=%s", entry_id, action)

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            logger.warning("SesameCardsRegisterView: Invalid or missing entry_id: %s", entry_id)
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if action not in ("start", "stop"):
            logger.warning("SesameCardsRegisterView: Invalid action: %s", action)
            return self.json({"error": "Invalid action (must be 'start' or 'stop')"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]
        if not wrapper.device or not wrapper.device.is_logged_in:
            logger.warning("SesameCardsRegisterView: Device for entry_id %s is not connected or logged in", entry_id)
            return self.json({"error": "Device is not connected or logged in"}, status_code=503)

        try:
            if action == "start":
                wrapper.device.scanned_card = None
                logger.info("SesameCardsRegisterView: Starting card registration mode for device %s", wrapper.device.mac_address)
                await wrapper.device.set_card_registration_mode(True)
                logger.info("SesameCardsRegisterView: Successfully set card registration mode to True on device")
            else:
                logger.info("SesameCardsRegisterView: Stopping card registration mode for device %s", wrapper.device.mac_address)
                await wrapper.device.set_card_registration_mode(False)
                wrapper.device.scanned_card = None
                logger.info("SesameCardsRegisterView: Successfully set card registration mode to False on device")
            return self.json({"success": True})
        except Exception as e:
            logger.exception("SesameCardsRegisterView: Failed to modify registration mode for device %s", wrapper.device.mac_address)
            return self.json({"error": f"Failed to modify registration mode: {e}"}, status_code=500)


class SesameFingerprintsRegisterView(HomeAssistantView):
    """View to handle fingerprint registration mode (start/stop/status)."""

    url = "/api/sesame_ble/fingerprints/register"
    name = "api:sesame_ble:fingerprints:register"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Start or stop fingerprint registration mode, or clear scanned fingerprint."""
        try:
            data = await request.json()
        except ValueError:
            logger.warning("SesameFingerprintsRegisterView: Invalid JSON payload received")
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        action = data.get("action")  # "start", "stop"
        logger.info("SesameFingerprintsRegisterView: received POST request: entry_id=%s, action=%s", entry_id, action)

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            logger.warning("SesameFingerprintsRegisterView: Invalid or missing entry_id: %s", entry_id)
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if action not in ("start", "stop"):
            logger.warning("SesameFingerprintsRegisterView: Invalid action: %s", action)
            return self.json({"error": "Invalid action (must be 'start' or 'stop')"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]
        if not wrapper.device or not wrapper.device.is_logged_in:
            logger.warning("SesameFingerprintsRegisterView: Device for entry_id %s is not connected or logged in", entry_id)
            return self.json({"error": "Device is not connected or logged in"}, status_code=503)

        try:
            if action == "start":
                wrapper.device.scanned_fingerprint = None
                logger.info("SesameFingerprintsRegisterView: Starting fingerprint registration mode for device %s", wrapper.device.mac_address)
                await wrapper.device.set_fingerprint_registration_mode(True)
                logger.info("SesameFingerprintsRegisterView: Successfully set fingerprint registration mode to True on device")
            else:
                logger.info("SesameFingerprintsRegisterView: Stopping fingerprint registration mode for device %s", wrapper.device.mac_address)
                await wrapper.device.set_fingerprint_registration_mode(False)
                wrapper.device.scanned_fingerprint = None
                logger.info("SesameFingerprintsRegisterView: Successfully set fingerprint registration mode to False on device")
            return self.json({"success": True})
        except Exception as e:
            logger.exception("SesameFingerprintsRegisterView: Failed to modify registration mode for device %s", wrapper.device.mac_address)
            return self.json({"error": f"Failed to modify registration mode: {e}"}, status_code=500)


class SesameCardsAddView(HomeAssistantView):
    """View to add/register a scanned card with a name."""

    url = "/api/sesame_ble/cards/add"
    name = "api:sesame_ble:cards:add"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Add a scanned card physically and logically."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        uid = data.get("uid")
        name = data.get("name", "").strip()
        card_type = data.get("type", 0x80)

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not uid:
            return self.json({"error": "Card UID is required"}, status_code=400)
        if not name:
            return self.json({"error": "Name is required"}, status_code=400)

        wrapper = self.hass.data[DOMAIN][entry_id]
        if not wrapper.device or not wrapper.device.is_logged_in:
            return self.json({"error": "Device is not connected or logged in"}, status_code=503)

        # Check for duplicate card name (case-insensitive)
        for other_uid, other_info in wrapper.logical_cards.items():
            if other_info["name"].strip().lower() == name.lower():
                return self.json({"error": f"An NFC card named '{name}' already exists"}, status_code=400)

        try:
            # 1. Add physically via BLE
            await wrapper.device.add_card(uid, name, card_type)

            # 2. Add logically
            wrapper.logical_cards[uid] = {
                "name": name,
                "type": card_type,
            }
            await wrapper.store.async_save({
                "passcodes": wrapper.logical_passcodes,
                "cards": wrapper.logical_cards,
                "fingerprints": wrapper.logical_fingerprints
            })

            # 3. Stop registration mode and clear scanned_card
            try:
                await wrapper.device.set_card_registration_mode(False)
            except Exception:
                pass
            wrapper.device.scanned_card = None

            # 4. Trigger sync and HA refresh
            asyncio.create_task(wrapper._sync_and_apply_schedules())

            return self.json({"success": True})
        except Exception as e:
            return self.json({"error": f"Failed to register card: {e}"}, status_code=500)


class SesameLockHistoryView(HomeAssistantView):
    """View to retrieve history logs for the paired lock."""

    url = "/api/sesame_ble/history"
    name = "api:sesame_ble:history"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Retrieve lock history logs."""
        entry_id = request.query.get("entry_id")
        if not entry_id or DOMAIN not in self.hass.data or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)

        keypad_wrapper = self.hass.data[DOMAIN][entry_id]
        
        # Resolve paired lock wrapper
        lock_wrapper = self._find_paired_lock(entry_id)
        if not lock_wrapper:
            return self.json({"history": [], "lock_name": None})

        # Trigger background fetch/flush of physical history
        if lock_wrapper.device and lock_wrapper.device.is_logged_in:
            asyncio.create_task(lock_wrapper.fetch_and_flush_history())

        # Map raw history records to human-readable format
        formatted_history = []
        for record in lock_wrapper.history_records:
            h_type = record.get("type", 0)
            is_unlock = (h_type % 2 == 0) and (h_type != 0)
            event_type = "Unlock" if is_unlock else "Lock"

            tag = record.get("tag", 0)
            raw_param = record.get("raw_parameter", "")
            
            method = "Manual"
            caller = "Manual"

            if h_type in (1, 2):
                method = "Manual"
                caller = "Manual"
            elif h_type in (3, 4):
                method = "BLE"
                caller = "Home Assistant"
            elif h_type in (5, 6):
                method = "Auto-Lock"
                caller = "Auto"
            elif h_type in (7, 8):
                method = "Web API"
                caller = "Cloud API"
            elif h_type in (11, 12):
                method = "Keypad"
                caller = "Keypad"

            if tag == 0:  # NFC Card
                method = "NFC Card"
                caller = keypad_wrapper.logical_cards.get(raw_param, {}).get("name", f"NFC Card ({raw_param[:8]})")
            elif tag == 1:  # Fingerprint
                method = "Fingerprint"
                caller = keypad_wrapper.logical_fingerprints.get(raw_param, {}).get("name", f"Fingerprint ({raw_param[:8]})")
            elif tag == 2:  # Passcode
                method = "Passcode"
                caller = keypad_wrapper.logical_passcodes.get(raw_param, {}).get("name", f"Passcode ({raw_param[:8]})")
            elif tag in (5, 6):  # TouchPro UUID, Touch UUID
                method = "Keypad"
                caller = keypad_wrapper.entry.title
            else:
                name_str = None
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

                if name_str and name_str != raw_param:
                    caller = name_str
                    if name_str == "Home Assistant":
                        method = "BLE"

            formatted_history.append({
                "record_id": record.get("record_id"),
                "timestamp": record.get("timestamp"),
                "event_type": event_type,
                "method": method,
                "caller": caller,
            })

        return self.json({
            "lock_name": lock_wrapper.entry.title,
            "history": formatted_history
        })

    def _find_paired_lock(self, keypad_entry_id: str) -> SesameDeviceWrapper | None:
        """Find the lock paired with the keypad."""
        if DOMAIN not in self.hass.data:
            return None
            
        keypad_wrapper = self.hass.data[DOMAIN].get(keypad_entry_id)
        if not keypad_wrapper:
            return None
            
        locks = []
        for entry_id, wrapper in self.hass.data[DOMAIN].items():
            if entry_id == "views_registered":
                continue
            if not hasattr(wrapper, "model_name") or "TOUCH" not in wrapper.model_name:
                locks.append(wrapper)
                
        if not locks:
            return None
            
        if len(locks) == 1:
            return locks[0]
            
        keypad_title = keypad_wrapper.entry.title.lower()
        for word in ("keypad", "touch", "pro", "sesame"):
            keypad_title = keypad_title.replace(word, "")
        keypad_title = keypad_title.strip()
        
        best_match = None
        best_score = 0
        for lock in locks:
            lock_title = lock.entry.title.lower()
            for word in ("lock", "sesame"):
                lock_title = lock_title.replace(word, "")
            lock_title = lock_title.strip()
            
            if keypad_title and lock_title and (keypad_title in lock_title or lock_title in keypad_title):
                score = min(len(keypad_title), len(lock_title))
                if score > best_score:
                    best_score = score
                    best_match = lock
                    
        if best_match:
            return best_match
            
        return locks[0]


class SesameKeypadPairView(HomeAssistantView):
    """View to pair a lock with the keypad."""

    url = "/api/sesame_ble/keypad/pair"
    name = "api:sesame_ble:keypad:pair"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Execute lock pairing."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        lock_entry_id = data.get("lock_entry_id")

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not lock_entry_id or lock_entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid lock_entry_id"}, status_code=400)

        keypad_wrapper = self.hass.data[DOMAIN][entry_id]
        lock_wrapper = self.hass.data[DOMAIN][lock_entry_id]

        if not keypad_wrapper.device or not keypad_wrapper.device.is_logged_in:
            return self.json({"error": "Keypad is not connected or logged in"}, status_code=503)

        try:
            from uuid import UUID
            lock_uuid = lock_wrapper.adv_data.device_uuid
            secret_key_bytes = bytes.fromhex(lock_wrapper.secret_key)
            
            await keypad_wrapper.device.add_paired_lock(lock_uuid, secret_key_bytes)
            return self.json({"success": True})
        except Exception as e:
            return self.json({"error": f"Failed to pair lock: {e}"}, status_code=500)


class SesameKeypadUnpairView(HomeAssistantView):
    """View to unpair a lock from the keypad."""

    url = "/api/sesame_ble/keypad/unpair"
    name = "api:sesame_ble:keypad:unpair"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Execute lock unpairing."""
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "Invalid JSON payload"}, status_code=400)

        entry_id = data.get("entry_id")
        lock_uuid = data.get("lock_uuid")

        if not entry_id or entry_id not in self.hass.data[DOMAIN]:
            return self.json({"error": "Invalid entry_id"}, status_code=400)
        if not lock_uuid:
            return self.json({"error": "Lock UUID is required"}, status_code=400)

        keypad_wrapper = self.hass.data[DOMAIN][entry_id]

        if not keypad_wrapper.device or not keypad_wrapper.device.is_logged_in:
            return self.json({"error": "Keypad is not connected or logged in"}, status_code=503)

        try:
            from uuid import UUID
            await keypad_wrapper.device.remove_paired_lock(UUID(lock_uuid))
            return self.json({"success": True})
        except Exception as e:
            return self.json({"error": f"Failed to unpair lock: {e}"}, status_code=500)
