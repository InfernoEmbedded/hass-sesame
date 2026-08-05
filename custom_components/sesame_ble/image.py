"""Image platform for Candy House Sesame BLE integration."""

import io
import logging
from datetime import datetime, timezone

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback


from .const import DOMAIN
from .__init__ import SesameDeviceWrapper

logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up image entities for Candy House Sesame BLE device."""
    wrapper: SesameDeviceWrapper = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SesameQRCodeImageEntity(hass, wrapper)])


class SesameQRCodeImageEntity(ImageEntity):
    """Image entity that renders the device setup QR Code."""

    def __init__(self, hass: HomeAssistant, wrapper: SesameDeviceWrapper) -> None:
        """Initialize the QR code image entity."""
        super().__init__(hass)
        self.wrapper = wrapper
        self._attr_name = "Setup QR Code Image"
        self._attr_unique_id = f"{wrapper.entry.unique_id}_setup_qr_code_image"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:qrcode"
        self._attr_content_type = "image/png"
        self._attr_image_last_updated = datetime.now(timezone.utc)
        self._cached_bytes: bytes | None = None


    @property
    def available(self) -> bool:
        """Image entity is always available."""
        return True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info matching the main Sesame device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.wrapper.entry.unique_id)},
            name=f"Sesame {self.wrapper.model_name}",
            manufacturer="CANDY HOUSE",
            model=self.wrapper.model_name,
        )


    async def async_image(self) -> bytes | None:
        """Return image bytes of the QR Code."""
        if self._cached_bytes:
            return self._cached_bytes

        qr_info = self.wrapper.get_qr_code_data()
        if not qr_info or not qr_info.get("qr_url"):
            return None

        qr_url = qr_info["qr_url"]
        try:
            import qrcode
            qr = qrcode.QRCode(version=1, box_size=8, border=2)
            qr.add_data(qr_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self._cached_bytes = buf.getvalue()
            return self._cached_bytes
        except Exception as e:
            logger.warning("Error generating local QR code PNG bytes for %s: %s", self.wrapper.entry.title, e)

        return None
