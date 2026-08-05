import os
import sys
from unittest.mock import MagicMock
from uuid import UUID

# Set up real class mocks for base classes to prevent inheritance issues with MagicMock
class MockConfigFlow:
    def __init__(self, *args, **kwargs):
        self.hass = None
        self.context = {}
    def __init_subclass__(cls, **kwargs):
        pass
    async def async_create_entry(self, *args, **kwargs):
        pass
    async def async_show_form(self, *args, **kwargs):
        pass
    async def async_set_unique_id(self, *args, **kwargs):
        pass
    def _abort_if_unique_id_configured(self, *args, **kwargs):
        pass
    def async_abort(self, *args, **kwargs):
        pass
    def async_show_menu(self, *args, **kwargs):
        pass

class MockEntity:
    def __init__(self):
        self.hass = None
    @property
    def name(self):
        return getattr(self, "_attr_name", None)
    @property
    def unique_id(self):
        return getattr(self, "_attr_unique_id", None)
    def async_write_ha_state(self):
        pass

class MockLockEntity(MockEntity):
    pass

class MockSensorEntity(MockEntity):
    pass

class MockButtonEntity(MockEntity):
    pass

# Helper to build mock modules and link them in sys.modules
def mock_module(name, attrs=None):
    import types
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    sys.modules[name] = mod
    return mod

# Build the module hierarchy from leaf to root
config_entries = mock_module("homeassistant.config_entries", {
    "ConfigFlow": MockConfigFlow,
    "ConfigEntry": MagicMock,
})
const = mock_module("homeassistant.const", {
    "Platform": MagicMock(),
    "PERCENTAGE": "%",
    "EntityCategory": MagicMock(),
})
core = mock_module("homeassistant.core", {
    "HomeAssistant": MagicMock,
    "ServiceCall": MagicMock,
    "callback": lambda x: x,
})
exceptions = mock_module("homeassistant.exceptions", {
    "ConfigEntryNotReady": Exception,
    "HomeAssistantError": Exception,
})
device_registry = mock_module("homeassistant.helpers.device_registry", {
    "format_mac": lambda x: x,
    "CONNECTION_BLUETOOTH": "bluetooth",
})
entity = mock_module("homeassistant.helpers.entity", {
    "DeviceInfo": dict,
})
entity_platform = mock_module("homeassistant.helpers.entity_platform", {
    "AddEntitiesCallback": MagicMock,
})
data_entry_flow = mock_module("homeassistant.data_entry_flow", {
    "FlowResult": MagicMock,
})

class MockFileSelector:
    def __init__(self, config=None):
        self.config = config
    def __call__(self, val):
        return val

class MockFileSelectorConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

selector = mock_module("homeassistant.helpers.selector", {
    "FileSelector": MockFileSelector,
    "FileSelectorConfig": MockFileSelectorConfig,
})

from contextlib import contextmanager

@contextmanager
def mock_process_uploaded_file(hass, file_id):
    yield MagicMock()

file_upload = mock_module("homeassistant.components.file_upload", {
    "process_uploaded_file": mock_process_uploaded_file,
})

class MockStore:
    def __init__(self, hass, version, key, **kwargs):
        self.hass = hass
        self.version = version
        self.key = key
        # We can store the data inside the config entry or hass object to simulate persistence
        self._data = {}

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data

storage = mock_module("homeassistant.helpers.storage", {
    "Store": MockStore,
})

helpers = mock_module("homeassistant.helpers", {
    "device_registry": device_registry,
    "entity": entity,
    "entity_platform": entity_platform,
    "selector": selector,
    "storage": storage,
})

class MockNumberEntity(MockEntity):
    pass

class MockSelectEntity(MockEntity):
    pass

class MockBinarySensorEntity(MockEntity):
    pass

lock = mock_module("homeassistant.components.lock", {
    "LockEntity": MockLockEntity,
})
sensor = mock_module("homeassistant.components.sensor", {
    "SensorEntity": MockSensorEntity,
    "SensorDeviceClass": MagicMock(),
    "SensorStateClass": MagicMock(),
})
binary_sensor = mock_module("homeassistant.components.binary_sensor", {
    "BinarySensorEntity": MockBinarySensorEntity,
    "BinarySensorDeviceClass": MagicMock(),
})
button = mock_module("homeassistant.components.button", {
    "ButtonEntity": MockButtonEntity,
})
number = mock_module("homeassistant.components.number", {
    "NumberEntity": MockNumberEntity,
})
select = mock_module("homeassistant.components.select", {
    "SelectEntity": MockSelectEntity,
})

bluetooth_mock = mock_module("homeassistant.components.bluetooth", {
    "async_discovered_service_info": MagicMock(),
    "async_ble_device_from_address": MagicMock(),
    "async_get_advertisement_data": MagicMock(),
    "BluetoothServiceInfoBleak": MagicMock,
    "async_last_service_info": MagicMock(),
})
class MockHomeAssistantView:
    def __init__(self, *args, **kwargs):
        pass

    def json(self, data, status=200, status_code=None):
        from aiohttp import web
        return web.json_response(data, status=status_code if status_code is not None else status)

http = mock_module("homeassistant.components.http", {
    "HomeAssistantView": MockHomeAssistantView,
})

class MockUpdateEntity(MockEntity):
    pass

class MockUpdateEntityFeature:
    INSTALL = 1
    PROGRESS = 4

update = mock_module("homeassistant.components.update", {
    "UpdateEntity": MockUpdateEntity,
    "UpdateDeviceClass": MagicMock(),
    "UpdateEntityFeature": MockUpdateEntityFeature,
})

components = mock_module("homeassistant.components", {
    "lock": lock,
    "sensor": sensor,
    "binary_sensor": binary_sensor,
    "button": button,
    "number": number,
    "select": select,
    "update": update,
    "bluetooth": bluetooth_mock,
    "file_upload": file_upload,
    "http": http,
})


homeassistant = mock_module("homeassistant", {
    "config_entries": config_entries,
    "const": const,
    "core": core,
    "exceptions": exceptions,
    "helpers": helpers,
    "components": components,
    "data_entry_flow": data_entry_flow,
})

# Add custom_components directory to the path so we can import sesame_ble
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../custom_components")))

TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"
TEST_UUID = UUID("01234567-89ab-cdef-0123-456789abcdef")
TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef"
TEST_SECRET_BYTES = bytes.fromhex(TEST_SECRET_HEX)
