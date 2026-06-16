# Candy House Sesame BLE Home Assistant Integration

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Stability: Stable](https://img.shields.io/badge/Stability-Stable-green.svg)](#)

A high-performance, fully local Home Assistant custom integration for controlling and managing **Candy House Sesame 5 / 5 Pro** locks and **Sesame Touch / Touch Pro** keypads directly via Bluetooth Low Energy (BLE) based on the [official Candy House API documentation](https://github.com/CANDY-HOUSE/API_document).

> [!NOTE]
> This integration operates **100% locally** over Bluetooth. It does not require a Candy House Wi-Fi Hub, active internet connection, or cloud API credentials.

---

## Features

- 🔒 **Local Lock & Unlock**: Quick, responsive lock operations over BLE.
- 🔋 **Device Sensors**: Diagnostic sensors reporting real-time battery percentages for locks and keypads.
- 🔢 **Sesame Touch Sensor Suite**: Exposes metrics for registered credentials (fingerprint count, card count, and passcode count).
- 🏷️ **Passcode Database Sync**: Dynamically syncs and lists all registered passcodes (names and unique identifiers) inside the passcode sensor's extra attributes.
- ⚙️ **Keypad Passcode Management**: Direct services to **Add**, **Delete**, and **Update/Rename** keypad PIN codes directly from Home Assistant.
- 🔌 **Stable Connection Backend**: Uses Home Assistant's native BLE framework (`BLEDevice`) and connection retry connector (`bleak-retry-connector`) to work flawlessly with Bluetooth USB dongles and Bluetooth Proxies (ESPHome).
- 📱 **QR Code Setup**: Automatically parses `ssm://` setup URLs extracted from the official Candy House mobile app's QR codes to auto-populate UUIDs and Secret Keys.

---

## Supported Hardware

- 🔑 **Candy House Sesame 5**
- 🔑 **Candy House Sesame 5 Pro**
- ⌨️ **Candy House Sesame Touch**
- ⌨️ **Candy House Sesame Touch Pro**

---

## Installation

### Manual Installation
1. Download the latest source code or clone the repository.
2. Copy the `custom_components/sesame_ble` directory into your Home Assistant's `config/custom_components/` directory.
3. Your folder structure should look like:
   ```text
   config/
   └── custom_components/
       └── sesame_ble/
           ├── __init__.py
           ├── config_flow.py
           ├── const.py
           ├── manifest.json
           ├── services.yaml
           └── ...
   ```
4. **Restart** Home Assistant.

---

## Configuration

1. In the Home Assistant UI, navigate to **Settings** -> **Devices & Services**.
2. Click **Add Integration** in the bottom-right.
3. Search for **Candy House Sesame BLE**.
4. Choose your configuration method:
   - **QR Code (Recommended)**: Scan or copy the QR code URL from your Sesame app (format: `ssm://UI?uuid=...&key=...&m=...`). The integration will parse the MAC address, UUID, and Encryption Secret Key automatically.
   - **Manual Configuration**: Manually enter the Bluetooth MAC Address, Device UUID, and Secret Key.

---

## Passcode Management Services

The integration exposes services specifically for managing local keypad passcodes on **Sesame Touch** / **Touch Pro** keypads.

### 1. `sesame_ble.add_passcode`
Adds a new passcode to the Sesame Touch keypad.

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The Sesame Touch device target. | *Select device* |
| `passcode` | String | Yes | Passcode PIN digits (between 4 and 16 digits, 0-9). | `"123456"` |
| `name` | String | Yes | Name / Nickname to identify the passcode. | `"John Doe"` |

---

### 2. `sesame_ble.delete_passcode`
Deletes an existing passcode from the Sesame Touch keypad.

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The Sesame Touch device target. | *Select device* |
| `passcode_or_id` | String | Yes | The passcode PIN (e.g. `"123456"`) or its unique Hex ID (e.g. `"010203040506"`). | `"010203040506"` |

> [!TIP]
> You can retrieve the unique Hex ID of any passcode from the `Registered Passcodes` sensor's attributes. Using the Hex ID is recommended to avoid exposing raw PINs in logs or scripts.

---

### 3. `sesame_ble.update_passcode`
Updates/renames the nickname of an existing passcode.

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The Sesame Touch device target. | *Select device* |
| `passcode_or_id` | String | Yes | The passcode PIN or its unique Hex ID to rename. | `"010203040506"` |
| `name` | String | Yes | New name / nickname (maximum 20 characters). | `"John (Work PIN)"` |

---

## License

This project is licensed under the **GNU General Public License v3.0**. See the [LICENSE](LICENSE) file for the full text.
