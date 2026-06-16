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

logger = logging.getLogger(__name__)


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
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._qr_code_info: SesameQRCode | None = None
        self._mac_address: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            qr_code_image = user_input.get("qr_code_image")
            qr_url = user_input.get(CONF_QR_URL)
            mac_address = user_input.get("mac_address")
            secret_key = user_input.get(CONF_SECRET_KEY)
            model_name = user_input.get(CONF_MODEL)

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
                    # Attempt to resolve the BLE MAC address using the UUID from the QR code
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
                        return self.async_create_entry(
                            title=self._qr_code_info.device_name or f"Sesame {model_name}",
                            data={
                                "mac_address": self._mac_address,
                                CONF_SECRET_KEY: self._qr_code_info.secret_key.hex(),
                                CONF_MODEL: model_name,
                                CONF_DEVICE_UUID: str(self._qr_code_info.device_uuid),
                            },
                        )
                    else:
                        # Cannot resolve automatically, proceed to manual selection with parsed details
                        return await self.async_step_select_address()

            elif mac_address and secret_key and model_name:
                # Manual entry validation
                try:
                    bytes.fromhex(secret_key)
                    if len(secret_key) != 32:
                        raise ValueError("Secret key must be 16 bytes (32 hex characters)")
                except ValueError:
                    errors[CONF_SECRET_KEY] = "invalid_secret_key"

                if not errors:
                    self._mac_address = mac_address
                    formatted_mac = format_mac(mac_address)
                    await self.async_set_unique_id(formatted_mac)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Sesame {model_name} ({mac_address[-5:]})",
                        data={
                            "mac_address": mac_address,
                            CONF_SECRET_KEY: secret_key,
                            CONF_MODEL: model_name,
                        },
                    )

        # Discover nearby devices to display in manual address step or for user help
        models_list = [model.name for model in ProductModels]

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional("qr_code_image"): selector.FileSelector(
                        selector.FileSelectorConfig(accept="image/*")
                    ),
                    vol.Optional(CONF_QR_URL): str,
                    vol.Optional("mac_address"): str,
                    vol.Optional(CONF_SECRET_KEY): str,
                    vol.Optional(CONF_MODEL, default=ProductModels.SESAME5.name): vol.In(models_list),
                }
            ),
            errors=errors,
            description_placeholders={
                "qr_code_help": "Or paste the shared ssm:// URL from the official app's QR code share option."
            }
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
            return self.async_create_entry(
                title=self._qr_code_info.device_name or f"Sesame {model_name}",
                data={
                    "mac_address": self._mac_address,
                    CONF_SECRET_KEY: self._qr_code_info.secret_key.hex(),
                    CONF_MODEL: model_name,
                    CONF_DEVICE_UUID: str(self._qr_code_info.device_uuid),
                },
            )

        # Build list of discovered Sesame BLE devices for selection
        discovered_options = {}
        for service_info in async_discovered_service_info(self.hass):
            if COMPANY_ID in service_info.advertisement.manufacturer_data:
                mfg_data = service_info.advertisement.manufacturer_data[COMPANY_ID]
                try:
                    sesame_adv_data = SesameAdData.decode(mfg_data)
                    model_name = ProductModels(sesame_adv_data.model_id).name
                    discovered_options[service_info.address] = (
                        f"{model_name} ({service_info.name or service_info.address})"
                    )
                except Exception:
                    pass

        if not discovered_options:
            errors["base"] = "no_devices_found"
            # Fallback to text input
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

        self.context["title_placeholders"] = {
            "name": f"Sesame {model_name} ({mac_address[-5:]})"
        }
        self._mac_address = mac_address

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery and request secret key."""
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
                # Find matching model
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

                return self.async_create_entry(
                    title=f"Sesame {model_name} ({self._mac_address[-5:]})",
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
