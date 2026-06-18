import logging
from typing import Any
from urllib import parse
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import format_mac

from .const import CONF_MODEL, CONF_QR_URL, CONF_SECRET_KEY, CONF_DEVICE_UUID, DOMAIN
from .sesame_client import COMPANY_ID, ProductModels, SesameQRCode, SesameAdData
from .sesame_client.device import SesameDevice

logger = logging.getLogger(__name__)


FRIENDLY_MODELS = {
    "SESAME5": "5",
    "SESAME5_PRO": "5 Pro",
    "SESAME_TOUCH_PRO": "Touch Pro",
    "SESAME_TOUCH": "Touch",
    "SESAME5_USA": "5 USA",
    "SESAME_TOUCH_2_PRO": "Touch 2 Pro",
}


def parse_qr_code(qr_url: str) -> SesameQRCode:
    """Parses a Candy House ssm:// QR Code URL."""
    return SesameQRCode.from_url(qr_url)


def _decode_uploaded_qr(hass, uploaded_file_id) -> str | None:
    """Read and decode the uploaded QR image in the executor."""
    try:
        with process_uploaded_file(hass, uploaded_file_id) as file_path:
            from PIL import Image
            from pyzbar.pyzbar import decode
            with Image.open(file_path) as img:
                decoded_objects = decode(img)
                for obj in decoded_objects:
                    url = obj.data.decode("utf-8")
                    if url.startswith("ssm://"):
                        return url
    except Exception as err:
        logger.exception("Error decoding QR image: %s", err)
    return None


class SesameBLEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sesame BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._qr_code_info: SesameQRCode | None = None
        self._mac_address: str | None = None
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._sesame_adv_data: SesameAdData | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step by showing options menu."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["discover_unregistered", "import_qr", "manual"],
        )

    async def async_step_discover_unregistered(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle discovery and registration of unregistered Sesame BLE devices."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._mac_address = user_input.get("mac_address")
            
            # Find the service info in discovered devices
            discovered_info = None
            for service_info in async_discovered_service_info(self.hass):
                if service_info.address == self._mac_address:
                    discovered_info = service_info
                    break
                    
            if not discovered_info:
                errors["base"] = "device_not_found"
            else:
                mfg_data = discovered_info.advertisement.manufacturer_data.get(COMPANY_ID)
                try:
                    sesame_adv_data = SesameAdData.decode(mfg_data)
                    model_name = ProductModels(sesame_adv_data.model_id).name
                except Exception:
                    errors["base"] = "invalid_mfg_data"
                    
                if not errors:
                    # Perform registration handshake
                    device = SesameDevice(discovered_info.device, sesame_adv_data)
                    try:
                        await device.connect()
                        secret_key_hex = await device.register()
                        await device.disconnect()
                        
                        formatted_mac = format_mac(self._mac_address)
                        await self.async_set_unique_id(formatted_mac)
                        self._abort_if_unique_id_configured()
                        
                        friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
                        return self.async_create_entry(
                            title=f"Sesame {friendly_model} ({self._mac_address[-5:]})",
                            data={
                                "mac_address": self._mac_address,
                                CONF_SECRET_KEY: secret_key_hex,
                                CONF_MODEL: model_name,
                                CONF_DEVICE_UUID: str(sesame_adv_data.device_uuid),
                            },
                        )
                    except Exception as e:
                        logger.exception("Registration failed over BLE")
                        errors["base"] = "registration_failed"
                        try:
                            await device.disconnect()
                        except Exception:
                            pass

        # Discover unregistered devices (is_registered == False)
        discovered_options = {}
        for service_info in async_discovered_service_info(self.hass):
            if COMPANY_ID in service_info.advertisement.manufacturer_data:
                mfg_data = service_info.advertisement.manufacturer_data[COMPANY_ID]
                try:
                    sesame_adv_data = SesameAdData.decode(mfg_data)
                    if not sesame_adv_data.is_registered:
                        model_name = ProductModels(sesame_adv_data.model_id).name
                        friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
                        discovered_options[service_info.address] = (
                            f"Sesame {friendly_model} ({service_info.name or service_info.address})"
                        )
                except Exception:
                    pass

        if not discovered_options:
            errors["base"] = "no_unregistered_devices"
            return self.async_show_form(
                step_id="discover_unregistered",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        return self.async_show_form(
            step_id="discover_unregistered",
            data_schema=vol.Schema(
                {vol.Required("mac_address"): vol.In(discovered_options)}
            ),
            errors=errors,
        )

    async def async_step_import_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle QR code or setup URL import."""
        errors: dict[str, str] = {}

        if user_input is not None:
            qr_code_image = user_input.get("qr_code_image")
            qr_url = user_input.get(CONF_QR_URL)

            if qr_code_image:
                qr_url = await self.hass.async_add_executor_job(
                    _decode_uploaded_qr, self.hass, qr_code_image
                )
                if not qr_url:
                    errors["base"] = "invalid_qr_code"

            if not errors and qr_url:
                try:
                    self._qr_code_info = parse_qr_code(qr_url)
                except Exception:
                    logger.exception("Failed to parse QR code URL")
                    errors["base"] = "invalid_qr_code"
                
                if not errors:
                    # Resolve BLE MAC address using the UUID from the QR code
                    target_uuid = self._qr_code_info.device_uuid
                    found_info = None
                    for service_info in async_discovered_service_info(self.hass):
                        if COMPANY_ID in service_info.advertisement.manufacturer_data:
                            mfg_data = service_info.advertisement.manufacturer_data[COMPANY_ID]
                            try:
                                sesame_adv_data = SesameAdData.decode(mfg_data)
                                if sesame_adv_data.device_uuid == target_uuid:
                                    found_info = service_info
                                    break
                            except Exception:
                                pass
                    
                    if found_info:
                        self._mac_address = found_info.address
                        formatted_mac = format_mac(self._mac_address)
                        await self.async_set_unique_id(formatted_mac)
                        self._abort_if_unique_id_configured()
                        model_name = ProductModels(self._qr_code_info.model_id).name
                        friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
                        return self.async_create_entry(
                            title=self._qr_code_info.device_name or f"Sesame {friendly_model}",
                            data={
                                "mac_address": self._mac_address,
                                CONF_SECRET_KEY: self._qr_code_info.secret_key.hex(),
                                CONF_MODEL: model_name,
                                CONF_DEVICE_UUID: str(self._qr_code_info.device_uuid),
                            },
                        )
                    else:
                        return await self.async_step_select_address()

        return self.async_show_form(
            step_id="import_qr",
            data_schema=vol.Schema(
                {
                    vol.Optional("qr_code_image"): selector.FileSelector(
                        selector.FileSelectorConfig(accept="image/*")
                    ),
                    vol.Optional(CONF_QR_URL): str,
                }
            ),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual config entry validation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mac_address = user_input.get("mac_address")
            secret_key = user_input.get(CONF_SECRET_KEY)
            model_name = user_input.get(CONF_MODEL)

            try:
                bytes.fromhex(secret_key)
                if len(secret_key) != 32:
                    raise ValueError
            except ValueError:
                errors[CONF_SECRET_KEY] = "invalid_secret_key"

            if not errors:
                self._mac_address = mac_address
                formatted_mac = format_mac(mac_address)
                await self.async_set_unique_id(formatted_mac)
                self._abort_if_unique_id_configured()
                friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
                return self.async_create_entry(
                    title=f"Sesame {friendly_model} ({mac_address[-5:]})",
                    data={
                        "mac_address": mac_address,
                        CONF_SECRET_KEY: secret_key,
                        CONF_MODEL: model_name,
                    },
                )

        models_list = [model.name for model in ProductModels]
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required("mac_address"): str,
                    vol.Required(CONF_SECRET_KEY): str,
                    vol.Required(CONF_MODEL, default=ProductModels.SESAME5.name): vol.In(models_list),
                }
            ),
            errors=errors,
        )

    async def async_step_select_address(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step to select the BLE MAC address if QR code UUID cannot be resolved automatically."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._mac_address = user_input["mac_address"]
            formatted_mac = format_mac(self._mac_address)
            await self.async_set_unique_id(formatted_mac)
            self._abort_if_unique_id_configured()

            model_name = ProductModels(self._qr_code_info.model_id).name
            friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
            return self.async_create_entry(
                title=self._qr_code_info.device_name or f"Sesame {friendly_model}",
                data={
                    "mac_address": self._mac_address,
                    CONF_SECRET_KEY: self._qr_code_info.secret_key.hex(),
                    CONF_MODEL: model_name,
                    CONF_DEVICE_UUID: str(self._qr_code_info.device_uuid),
                },
            )

        discovered_options = {}
        for service_info in async_discovered_service_info(self.hass):
            if COMPANY_ID in service_info.advertisement.manufacturer_data:
                mfg_data = service_info.advertisement.manufacturer_data[COMPANY_ID]
                try:
                    sesame_adv_data = SesameAdData.decode(mfg_data)
                    model_name = ProductModels(sesame_adv_data.model_id).name
                    friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
                    discovered_options[service_info.address] = (
                        f"Sesame {friendly_model} ({service_info.name or service_info.address})"
                    )
                except Exception:
                    pass

        if not discovered_options:
            errors["base"] = "no_devices_found"
            return self.async_show_form(
                step_id="select_address",
                data_schema=vol.Schema({vol.Required("mac_address"): str}),
                errors=errors,
            )

        return self.async_show_form(
            step_id="select_address",
            data_schema=vol.Schema(
                {vol.Required("mac_address"): vol.In(discovered_options)}
            ),
            errors=errors,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle bluetooth discovery."""
        mac_address = discovery_info.address
        formatted_mac = format_mac(mac_address)
        await self.async_set_unique_id(formatted_mac)
        self._abort_if_unique_id_configured()

        mfg_data = discovery_info.advertisement.manufacturer_data.get(COMPANY_ID)
        if not mfg_data:
            return self.async_abort(reason="not_sesame")

        try:
            sesame_adv_data = SesameAdData.decode(mfg_data)
            model_name = ProductModels(sesame_adv_data.model_id).name
        except Exception:
            return self.async_abort(reason="invalid_mfg_data")

        friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
        self.context["title_placeholders"] = {
            "name": f"Sesame {friendly_model} ({mac_address[-5:]})"
        }
        self._mac_address = mac_address
        self._discovery_info = discovery_info
        self._sesame_adv_data = sesame_adv_data

        if not sesame_adv_data.is_registered:
            return await self.async_step_bluetooth_register_confirm()

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery and request secret key for already registered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            secret_key = user_input[CONF_SECRET_KEY]
            try:
                bytes.fromhex(secret_key)
                if len(secret_key) != 32:
                    raise ValueError
            except ValueError:
                errors[CONF_SECRET_KEY] = "invalid_secret_key"

            if not errors:
                model_name = ProductModels.SESAME5.name
                for service_info in async_discovered_service_info(self.hass):
                    if service_info.address == self._mac_address:
                        mfg_data = service_info.advertisement.manufacturer_data.get(COMPANY_ID)
                        if mfg_data:
                            try:
                                sesame_adv_data = SesameAdData.decode(mfg_data)
                                model_name = ProductModels(sesame_adv_data.model_id).name
                                break
                            except Exception:
                                pass

                friendly_model = FRIENDLY_MODELS.get(model_name, model_name)
                return self.async_create_entry(
                    title=f"Sesame {friendly_model} ({self._mac_address[-5:]})",
                    data={
                        "mac_address": self._mac_address,
                        CONF_SECRET_KEY: secret_key,
                        CONF_MODEL: model_name,
                    },
                )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_SECRET_KEY): str}),
            errors=errors,
        )

    async def async_step_bluetooth_register_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm BLE registration for discovered unregistered Sesame device."""
        errors: dict[str, str] = {}

        if not self._discovery_info or not self._sesame_adv_data:
            return await self.async_step_user()

        model_name = ProductModels(self._sesame_adv_data.model_id).name
        friendly_model = FRIENDLY_MODELS.get(model_name, model_name)

        if user_input is not None:
            device = SesameDevice(self._discovery_info.device, self._sesame_adv_data)
            try:
                await device.connect()
                secret_key_hex = await device.register()
                await device.disconnect()

                formatted_mac = format_mac(self._mac_address)
                await self.async_set_unique_id(formatted_mac)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Sesame {friendly_model} ({self._mac_address[-5:]})",
                    data={
                        "mac_address": self._mac_address,
                        CONF_SECRET_KEY: secret_key_hex,
                        CONF_MODEL: model_name,
                        CONF_DEVICE_UUID: str(self._sesame_adv_data.device_uuid),
                    },
                )
            except Exception as e:
                logger.exception("BLE registration failed during auto-discovery")
                errors["base"] = "registration_failed"
                try:
                    await device.disconnect()
                except Exception:
                    pass

        return self.async_show_form(
            step_id="bluetooth_register_confirm",
            description_placeholders={"model": friendly_model, "mac": self._mac_address},
            errors=errors,
        )
