import pytest
from unittest.mock import MagicMock, patch, Mock, ANY, AsyncMock
import struct
from uuid import UUID

from homeassistant.helpers.device_registry import format_mac
from sesame_ble.config_flow import SesameBLEConfigFlow
from sesame_ble.const import CONF_MODEL, CONF_QR_URL, CONF_SECRET_KEY, CONF_DEVICE_UUID, DOMAIN
from sesame_ble.sesame_client import COMPANY_ID, ProductModels, SesameQRCode, SesameAdData

TEST_UUID = UUID("01234567-89ab-cdef-0123-456789abcdef")


@pytest.mark.asyncio
async def test_flow_manual_success() -> None:
    """Tests that manual setup flow with valid inputs successfully creates a config entry."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()

    # Mock inherited config flow methods
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    user_input = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME5",
    }

    result = await flow.async_step_manual(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("AA:BB:CC:DD:EE:FF"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="Sesame 5 (EE:FF)",
        data={
            "mac_address": "AA:BB:CC:DD:EE:FF",
            CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
            CONF_MODEL: "SESAME5",
        },
    )


@pytest.mark.asyncio
async def test_flow_manual_invalid_key() -> None:
    """Tests that manual setup flow displays errors when the secret key is invalid."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_show_form = MagicMock(return_value="form_displayed")

    # 1. Invalid key (not hex)
    user_input = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        CONF_SECRET_KEY: "invalidhexcharacters",
        CONF_MODEL: "SESAME5",
    }
    result = await flow.async_step_manual(user_input=user_input)
    assert result == "form_displayed"
    flow.async_show_form.assert_called_with(
        step_id="manual",
        data_schema=ANY,
        errors={CONF_SECRET_KEY: "invalid_secret_key"},
    )

    # 2. Invalid key (wrong length - not 16 bytes/32 chars)
    user_input[CONF_SECRET_KEY] = "0123456789abcdef"
    result = await flow.async_step_manual(user_input=user_input)
    assert result == "form_displayed"
    flow.async_show_form.assert_called_with(
        step_id="manual",
        data_schema=ANY,
        errors={CONF_SECRET_KEY: "invalid_secret_key"},
    )


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
async def test_flow_qr_code_with_discovered_mac(mock_discovered) -> None:
    """Tests that a valid QR URL flow auto-resolves the BLE MAC address if discovered."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    # Generate a valid QR URL for model 5 (SESAME5)
    secret_key = bytes(range(16))
    qr = SesameQRCode(
        device_name="My Lock",
        key_level=0,  # OWNER
        model_id=5,   # SESAME5
        device_uuid=TEST_UUID,
        secret_key=secret_key,
    )
    qr_url = qr.to_url()

    # Mock bluetooth discovery return
    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_service_info = MagicMock()
    mock_service_info.address = "AA:BB:CC:DD:EE:FF"
    mock_service_info.advertisement.manufacturer_data = {COMPANY_ID: mfg_data}
    mock_discovered.return_value = [mock_service_info]

    user_input = {CONF_QR_URL: qr_url}
    result = await flow.async_step_import_qr(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("AA:BB:CC:DD:EE:FF"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="My Lock",
        data={
            "mac_address": "AA:BB:CC:DD:EE:FF",
            CONF_SECRET_KEY: secret_key.hex(),
            CONF_MODEL: "SESAME5",
            CONF_DEVICE_UUID: str(TEST_UUID),
        },
    )


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
async def test_flow_qr_code_fallback_select_address(mock_discovered) -> None:
    """Tests that a valid QR URL flow falls back to selecting address if discovery doesn't find it."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_step_select_address = MagicMock(return_value="select_address_step")

    # Generate a valid QR URL
    secret_key = bytes(range(16))
    qr = SesameQRCode(
        device_name="My Lock",
        key_level=0,
        model_id=5,
        device_uuid=TEST_UUID,
        secret_key=secret_key,
    )
    qr_url = qr.to_url()

    # Mock bluetooth discovery to return unrelated device
    mock_discovered.return_value = []
    flow.async_step_select_address = AsyncMock(return_value="select_address_step")

    user_input = {CONF_QR_URL: qr_url}
    result = await flow.async_step_import_qr(user_input=user_input)

    assert result == "select_address_step"
    assert flow._qr_code_info == qr


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
async def test_flow_step_select_address_success(mock_discovered) -> None:
    """Tests that selecting discovered address successfully creates the config entry."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    # Prepopulate the QR code info from a previous step
    secret_key = bytes(range(16))
    qr = SesameQRCode(
        device_name="My Lock",
        key_level=0,
        model_id=5,
        device_uuid=TEST_UUID,
        secret_key=secret_key,
    )
    flow._qr_code_info = qr

    # Mock user input selecting address
    user_input = {"mac_address": "AA:BB:CC:DD:EE:FF"}
    result = await flow.async_step_select_address(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("AA:BB:CC:DD:EE:FF"))
    flow.async_create_entry.assert_called_once_with(
        title="My Lock",
        data={
            "mac_address": "AA:BB:CC:DD:EE:FF",
            CONF_SECRET_KEY: secret_key.hex(),
            CONF_MODEL: "SESAME5",
            CONF_DEVICE_UUID: str(TEST_UUID),
        },
    )


@pytest.mark.asyncio
async def test_flow_qr_code_invalid_url() -> None:
    """Tests that an invalid QR URL displays an error form."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_show_form = MagicMock(return_value="form_displayed")

    user_input = {CONF_QR_URL: "invalid_url_without_ssm_prefix"}
    result = await flow.async_step_import_qr(user_input=user_input)

    assert result == "form_displayed"
    flow.async_show_form.assert_called_once_with(
        step_id="import_qr",
        data_schema=ANY,
        errors={"base": "invalid_qr_code"},
    )


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
@patch("PIL.Image.open")
@patch("pyzbar.pyzbar.decode")
async def test_flow_qr_image_success(mock_decode, mock_image_open, mock_discovered) -> None:
    """Tests that a valid QR image upload flow successfully creates a config entry."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()

    # Mock async_add_executor_job to run the function synchronously
    async def mock_async_add_executor_job(func, *args, **kwargs):
        return func(*args, **kwargs)
    flow.hass.async_add_executor_job = mock_async_add_executor_job

    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    # Generate a valid QR URL for model 5 (SESAME5)
    secret_key = bytes(range(16))
    qr = SesameQRCode(
        device_name="My Lock",
        key_level=0,  # OWNER
        model_id=5,   # SESAME5
        device_uuid=TEST_UUID,
        secret_key=secret_key,
    )
    qr_url = qr.to_url()

    # Mock pyzbar decoding return
    mock_obj = MagicMock()
    mock_obj.data = qr_url.encode("utf-8")
    mock_decode.return_value = [mock_obj]

    # Mock bluetooth discovery return
    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes)
    mock_service_info = MagicMock()
    mock_service_info.address = "AA:BB:CC:DD:EE:FF"
    mock_service_info.advertisement.manufacturer_data = {COMPANY_ID: mfg_data}
    mock_discovered.return_value = [mock_service_info]

    user_input = {"qr_code_image": "some_file_id"}
    result = await flow.async_step_import_qr(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("AA:BB:CC:DD:EE:FF"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="My Lock",
        data={
            "mac_address": "AA:BB:CC:DD:EE:FF",
            CONF_SECRET_KEY: secret_key.hex(),
            CONF_MODEL: "SESAME5",
            CONF_DEVICE_UUID: str(TEST_UUID),
        },
    )


@pytest.mark.asyncio
@patch("PIL.Image.open")
@patch("pyzbar.pyzbar.decode")
async def test_flow_qr_image_no_qr(mock_decode, mock_image_open) -> None:
    """Tests that an image upload containing no QR code displays an error form."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()

    async def mock_async_add_executor_job(func, *args, **kwargs):
        return func(*args, **kwargs)
    flow.hass.async_add_executor_job = mock_async_add_executor_job

    flow.async_show_form = MagicMock(return_value="form_displayed")

    # Mock pyzbar to return no detected objects
    mock_decode.return_value = []

    user_input = {"qr_code_image": "some_file_id"}
    result = await flow.async_step_import_qr(user_input=user_input)

    assert result == "form_displayed"
    flow.async_show_form.assert_called_once_with(
        step_id="import_qr",
        data_schema=ANY,
        errors={"base": "invalid_qr_code"},
    )


@pytest.mark.asyncio
@patch("PIL.Image.open")
@patch("pyzbar.pyzbar.decode")
async def test_flow_qr_image_invalid_url(mock_decode, mock_image_open) -> None:
    """Tests that an image upload containing a non-ssm QR code URL displays an error form."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()

    async def mock_async_add_executor_job(func, *args, **kwargs):
        return func(*args, **kwargs)
    flow.hass.async_add_executor_job = mock_async_add_executor_job

    flow.async_show_form = MagicMock(return_value="form_displayed")

    # Mock pyzbar to return a QR code that does not start with ssm://
    mock_obj = MagicMock()
    mock_obj.data = b"https://example.com/not-ssm"
    mock_decode.return_value = [mock_obj]

    user_input = {"qr_code_image": "some_file_id"}
    result = await flow.async_step_import_qr(user_input=user_input)

    assert result == "form_displayed"
    flow.async_show_form.assert_called_once_with(
        step_id="import_qr",
        data_schema=ANY,
        errors={"base": "invalid_qr_code"},
    )


@pytest.mark.asyncio
@patch("homeassistant.components.file_upload.process_uploaded_file")
async def test_flow_qr_image_exception(mock_process) -> None:
    """Tests that an exception during the file processing phase displays an error form."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()

    async def mock_async_add_executor_job(func, *args, **kwargs):
        return func(*args, **kwargs)
    flow.hass.async_add_executor_job = mock_async_add_executor_job

    flow.async_show_form = MagicMock(return_value="form_displayed")

    # Mock the context manager to raise an exception when accessed
    mock_process.side_effect = Exception("File could not be opened")

    user_input = {"qr_code_image": "some_file_id"}
    result = await flow.async_step_import_qr(user_input=user_input)

    assert result == "form_displayed"
    flow.async_show_form.assert_called_once_with(
        step_id="import_qr",
        data_schema=ANY,
        errors={"base": "invalid_qr_code"},
    )


@pytest.mark.asyncio
async def test_flow_menu_user_step() -> None:
    """Tests that the initial user step presents a menu choice."""
    flow = SesameBLEConfigFlow()
    flow.async_show_menu = MagicMock(return_value="menu_displayed")
    
    result = await flow.async_step_user()
    assert result == "menu_displayed"
    flow.async_show_menu.assert_called_once_with(
        step_id="user",
        menu_options=["discover_unregistered", "import_qr", "manual"]
    )


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
async def test_discover_unregistered_no_devices(mock_discovered) -> None:
    """Tests step_discover_unregistered when no unregistered Sesame devices are found."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_show_form = MagicMock(return_value="form_displayed")

    # Mock discover to return nothing
    mock_discovered.return_value = []

    result = await flow.async_step_discover_unregistered()
    assert result == "form_displayed"
    flow.async_show_form.assert_called_once_with(
        step_id="discover_unregistered",
        data_schema=ANY,
        errors={"base": "no_unregistered_devices"}
    )


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
async def test_discover_unregistered_success(mock_discovered) -> None:
    """Tests successful BLE enrollment of discovered unregistered device."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    # Unregistered Sesame 5 advertisement data
    mfg_data = struct.pack("<HB16s", 5, 0, TEST_UUID.bytes) # 0 = is_registered is False
    mock_service_info = MagicMock()
    mock_service_info.address = "AA:BB:CC:DD:EE:FF"
    mock_service_info.name = "My New Sesame"
    mock_service_info.device = MagicMock()
    mock_service_info.advertisement.manufacturer_data = {COMPANY_ID: mfg_data}
    mock_discovered.return_value = [mock_service_info]

    # Mock the SesameDevice register method in config_flow
    with patch("sesame_ble.config_flow.SesameDevice") as mock_device_class:
        mock_device = MagicMock()
        mock_device.connect = AsyncMock()
        mock_device.register = AsyncMock(return_value="0123456789abcdef0123456789abcdef")
        mock_device.disconnect = AsyncMock()
        mock_device_class.return_value = mock_device

        # Form submission
        user_input = {"mac_address": "AA:BB:CC:DD:EE:FF"}
        result = await flow.async_step_discover_unregistered(user_input=user_input)

        assert result == "entry_created"
        mock_device.connect.assert_called_once()
        mock_device.register.assert_called_once()
        mock_device.disconnect.assert_called_once()
        flow.async_create_entry.assert_called_once_with(
            title="Sesame 5 (EE:FF)",
            data={
                "mac_address": "AA:BB:CC:DD:EE:FF",
                CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
                CONF_MODEL: "SESAME5",
                CONF_DEVICE_UUID: str(TEST_UUID),
            }
        )


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
async def test_bluetooth_step_routing_registered(mock_discovered) -> None:
    """Tests routing in async_step_bluetooth when device is already registered."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_step_bluetooth_confirm = AsyncMock(return_value="confirm_step")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    mfg_data = struct.pack("<HB16s", 5, 1, TEST_UUID.bytes) # is_registered = True
    discovery_info = MagicMock()
    discovery_info.address = "AA:BB:CC:DD:EE:FF"
    discovery_info.advertisement.manufacturer_data = {COMPANY_ID: mfg_data}

    result = await flow.async_step_bluetooth(discovery_info)
    assert result == "confirm_step"
    assert flow._mac_address == "AA:BB:CC:DD:EE:FF"
    flow.async_step_bluetooth_confirm.assert_called_once()


@pytest.mark.asyncio
@patch("sesame_ble.config_flow.async_discovered_service_info")
async def test_bluetooth_step_routing_unregistered(mock_discovered) -> None:
    """Tests routing in async_step_bluetooth when device is unregistered (starts BLE registration flow)."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_step_bluetooth_register_confirm = AsyncMock(return_value="register_confirm_step")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    mfg_data = struct.pack("<HB16s", 5, 0, TEST_UUID.bytes) # is_registered = False
    discovery_info = MagicMock()
    discovery_info.address = "AA:BB:CC:DD:EE:FF"
    discovery_info.advertisement.manufacturer_data = {COMPANY_ID: mfg_data}

    result = await flow.async_step_bluetooth(discovery_info)
    assert result == "register_confirm_step"
    assert flow._mac_address == "AA:BB:CC:DD:EE:FF"
    flow.async_step_bluetooth_register_confirm.assert_called_once()


@pytest.mark.asyncio
async def test_bluetooth_register_confirm_success() -> None:
    """Tests successful auto-discovery registration confirmation."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    # Prepopulate discovery variables
    flow._mac_address = "AA:BB:CC:DD:EE:FF"
    mfg_data = struct.pack("<HB16s", 5, 0, TEST_UUID.bytes)
    flow._sesame_adv_data = SesameAdData.decode(mfg_data)
    flow._discovery_info = MagicMock()
    flow._discovery_info.device = MagicMock()

    with patch("sesame_ble.config_flow.SesameDevice") as mock_device_class:
        mock_device = MagicMock()
        mock_device.connect = AsyncMock()
        mock_device.register = AsyncMock(return_value="0123456789abcdef0123456789abcdef")
        mock_device.disconnect = AsyncMock()
        mock_device_class.return_value = mock_device

        result = await flow.async_step_bluetooth_register_confirm(user_input={})

        assert result == "entry_created"
        flow.async_create_entry.assert_called_once_with(
            title="Sesame 5 (EE:FF)",
            data={
                "mac_address": "AA:BB:CC:DD:EE:FF",
                CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
                CONF_MODEL: "SESAME5",
                CONF_DEVICE_UUID: str(TEST_UUID),
            }
        )


@pytest.mark.asyncio
async def test_flow_sesame6_manual_success() -> None:
    """Tests manual setup flow for Sesame 6."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    user_input = {
        "mac_address": "11:22:33:44:55:66",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME6",
    }

    result = await flow.async_step_manual(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("11:22:33:44:55:66"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="Sesame 6 (55:66)",
        data={
            "mac_address": "11:22:33:44:55:66",
            CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
            CONF_MODEL: "SESAME6",
        },
    )


@pytest.mark.asyncio
async def test_flow_sesame6_pro_manual_success() -> None:
    """Tests manual setup flow for Sesame 6 Pro."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    user_input = {
        "mac_address": "AA:BB:CC:66:77:88",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME6_PRO",
    }

    result = await flow.async_step_manual(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("AA:BB:CC:66:77:88"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="Sesame 6 Pro (77:88)",
        data={
            "mac_address": "AA:BB:CC:66:77:88",
            CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
            CONF_MODEL: "SESAME6_PRO",
        },
    )


@pytest.mark.asyncio
async def test_flow_sesame_touch_2_manual_success() -> None:
    """Tests manual setup flow for Sesame Touch 2."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    user_input = {
        "mac_address": "11:22:33:44:99:88",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_TOUCH_2",
    }

    result = await flow.async_step_manual(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("11:22:33:44:99:88"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="Sesame Touch 2 (99:88)",
        data={
            "mac_address": "11:22:33:44:99:88",
            CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
            CONF_MODEL: "SESAME_TOUCH_2",
        },
    )


@pytest.mark.asyncio
async def test_flow_sesame_face_manual_success() -> None:
    """Tests manual setup flow for Sesame Face 1."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    user_input = {
        "mac_address": "11:22:33:44:FA:CE",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_FACE",
    }

    result = await flow.async_step_manual(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("11:22:33:44:FA:CE"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="Sesame Face 1 (FA:CE)",
        data={
            "mac_address": "11:22:33:44:FA:CE",
            CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
            CONF_MODEL: "SESAME_FACE",
        },
    )


@pytest.mark.asyncio
async def test_flow_sesame_face_ai_manual_success() -> None:
    """Tests manual setup flow for Sesame Face 1 AI."""
    flow = SesameBLEConfigFlow()
    flow.hass = MagicMock()
    flow.async_create_entry = MagicMock(return_value="entry_created")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()

    user_input = {
        "mac_address": "11:22:33:44:A1:A1",
        CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
        CONF_MODEL: "SESAME_FACE_AI",
    }

    result = await flow.async_step_manual(user_input=user_input)

    assert result == "entry_created"
    flow.async_set_unique_id.assert_called_once_with(format_mac("11:22:33:44:A1:A1"))
    flow._abort_if_unique_id_configured.assert_called_once()
    flow.async_create_entry.assert_called_once_with(
        title="Sesame Face 1 AI (A1:A1)",
        data={
            "mac_address": "11:22:33:44:A1:A1",
            CONF_SECRET_KEY: "0123456789abcdef0123456789abcdef",
            CONF_MODEL: "SESAME_FACE_AI",
        },
    )





def test_translations_completeness() -> None:
    """Verifies strings.json and all translation files exist, are valid JSON, and have matching keys."""
    import json
    import os
    base_dir = os.path.dirname(__file__)
    strings_path = os.path.normpath(os.path.join(base_dir, "../custom_components/sesame_ble/strings.json"))

    # 1. Verify strings.json exists and is valid
    assert os.path.exists(strings_path), "strings.json is missing!"
    with open(strings_path, "r", encoding="utf-8") as f:
        strings_data = json.load(f)

    # Check core keys exist in strings.json
    assert "config" in strings_data
    config_data = strings_data["config"]
    assert "step" in config_data
    assert "error" in config_data
    assert "abort" in config_data

    # Check specific errors returned in flow exist in strings.json
    expected_errors = ["invalid_qr_code", "invalid_secret_key", "no_devices_found", "no_unregistered_devices", "device_not_found", "registration_failed"]
    for err in expected_errors:
        assert err in config_data["error"], f"Error '{err}' is not defined in strings.json!"

    expected_aborts = ["already_configured", "not_sesame", "invalid_mfg_data"]
    for abrt in expected_aborts:
        assert abrt in config_data["abort"], f"Abort reason '{abrt}' is not defined in strings.json!"

    # 2. Check all other translation files match strings.json structure
    translations_dir = os.path.normpath(os.path.join(base_dir, "../custom_components/sesame_ble/translations"))
    assert os.path.exists(translations_dir), "translations directory is missing!"

    translation_files = ["en.json", "ja.json", "zh-Hant.json", "de.json", "fr.json", "es.json"]
    for filename in translation_files:
        file_path = os.path.join(translations_dir, filename)
        assert os.path.exists(file_path), f"Translation file {filename} is missing!"

        with open(file_path, "r", encoding="utf-8") as f:
            trans_data = json.load(f)

        # Assert structure matches strings.json exactly
        assert "config" in trans_data, f"{filename} is missing 'config' key!"
        t_config = trans_data["config"]

        # Check flow_title
        assert t_config.get("flow_title") == config_data.get("flow_title"), f"{filename} flow_title mismatch!"

        # Check steps keys
        for step_name, step_info in config_data["step"].items():
            assert step_name in t_config.get("step", {}), f"{filename} is missing step '{step_name}'!"
            # Check fields
            for field in step_info.get("data", {}):
                assert field in t_config["step"][step_name].get("data", {}), f"{filename} is missing field '{field}' in step '{step_name}'!"

        # Check errors
        for err in config_data["error"]:
            assert err in t_config.get("error", {}), f"{filename} is missing error key '{err}'!"

        # Check aborts
        for abrt in config_data["abort"]:
            assert abrt in t_config.get("abort", {}), f"{filename} is missing abort key '{abrt}'!"

