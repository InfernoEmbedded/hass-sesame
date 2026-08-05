# Candy House Sesame BLE Home Assistant Integration

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Stability: Stable](https://img.shields.io/badge/Stability-Stable-green.svg)](#)

A high-performance, fully local Home Assistant custom integration for controlling and managing **Candy House Sesame 5 / 5 Pro** locks and **Sesame Touch / Touch Pro** keypads directly via Bluetooth Low Energy (BLE) based on the [official Candy House API documentation](https://github.com/CANDY-HOUSE/API_document).

> [!NOTE]
> This integration operates **100% locally** over Bluetooth. It does not require a Candy House Wi-Fi Hub, active internet connection, or cloud API credentials.
>
> All credential nicknames, configurations, and schedules are stored in a **local Home Assistant database**. To protect your privacy and maintain local offline operations, names and schedules created in Home Assistant **will not sync to or appear in the official Candy House mobile app or cloud account**.

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

## Installation & Components

Installing the integration installs both components: the **Sesame BLE Lock Driver** (to communicate with locks) and the **Sesame Touch Keypad Manager** (to manage keypads and credentials).

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
           ├── views.py
           ├── static/
           │   ├── index.html
           │   └── panel.js
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
   - **Direct BLE Enrollment via Reset Button (100% App-Free)**: Put your Sesame lock or keypad into its unregistered pairing state by holding down its physical reset button (located under the battery cover or back plate) until it flashes/beeps. Select **Discover Unregistered Devices** in Home Assistant; the integration will automatically detect the reset device, perform the secure BLE registration handshake, retrieve its secret key, and add it directly.
   - **Manual Configuration**: Manually enter the Bluetooth MAC Address, Device UUID, and Secret Key.

---

## Keypad Manager Sidebar Panel

The integration automatically registers a custom sidebar dashboard called **Keypad Manager** (URL path `/keypads`, icon `mdi:dialpad`). This panel provides a rich, responsive user interface to perform local management tasks without configuration yaml or complex service calls:

- 📋 **Credential Summary**: See at a glance the number of registered passcodes, cards, and fingerprints.
- 🕒 **PIN Code Scheduler**: Add, delete, and rename keypad PINs, or define active schedules.
- 💳 **NFC Card & Fingerprint Registration Wizard**: Guide step-by-step additions of new physical cards or fingerprints by activating the keypad's registration mode on the device and naming the tapped credentials.
- 🔗 **Lock Pairing Manager**: Manage mapping links between Sesame Touch keypads and Sesame Locks.
- 📜 **Lock Operation History Logs**: View and search local operation records (detailing NFC cards used, fingerprints scanned, PINs entered, Auto-Lock triggers, or manual/Home Assistant actions).

### Passcode Types & Scheduling

When adding or updating a passcode via the **Keypad Manager** dashboard, you can define three types of scheduling and access restrictions:

1. **Temporary Passcodes (Date/Time Windowed)**:
   - Define a specific **Start Date & Time** and **End Date & Time** using the calendar pickers.
   - The passcode will only be active on the keypad within this specific date/time window. Perfect for guest rentals, delivery workers, or short-term visitors.

2. **One-Time Passcodes (OTP / Disposable)**:
   - Toggle the **One-Time Passcode (OTP)** switch to `On`.
   - The passcode allows exactly **one successful unlock event** on the paired lock. Once the unlock history record is retrieved by Home Assistant, the integration immediately and automatically deletes the passcode from both the Home Assistant database and the physical keypad.

3. **Repeating/Scheduled Passcodes (Weekly Day & Time Constraints)**:
   - Use the **Day Constraints** checkboxes (Monday through Sunday) and specify a **Daily Start Time** and **Daily End Time** (e.g., `09:00` to `17:00`).
   - The passcode will only function on the specified days of the week during the specified hours. Excellent for cleaners, dog walkers, or office staff working recurring shifts.

---

## Entity & Platform Reference

Depending on the hardware configured, the integration creates the following Home Assistant entities:

### 🔒 Sesame Lock Entities (Sesame 5 / 5 Pro)

| Entity ID | Platform | Category | Description / Features |
| :--- | :--- | :--- | :--- |
| `lock.<name>` | `lock` | - | Standard lock control entity. Supported services: `lock.lock`, `lock.unlock`. Exposes current angle, lock position, and unlock position in state attributes. |
| `sensor.<name>_battery` | `sensor` | Diagnostic | Reports real-time battery percentage. |
| `number.<name>_auto_lock_delay` | `number` | Config | Configures the lock's auto-lock delay in seconds (0 to 3600 seconds). |
| `button.<name>_set_locked_position` | `button` | Config | Calibrates the turn sensor by setting the current physical position as the **locked** angle. |
| `button.<name>_set_unlocked_position` | `button` | Config | Calibrates the turn sensor by setting the current physical position as the **unlocked** angle. |
| `sensor.<name>_locked_position` | `sensor` | Diagnostic | Reports the calibrated locked turn angle in degrees. |
| `sensor.<name>_unlocked_position` | `sensor` | Diagnostic | Reports the calibrated unlocked turn angle in degrees. |
| `sensor.<name>_current_angle` | `sensor` | Diagnostic | Reports the lock's current physical rotation angle in degrees. |

### ⌨️ Sesame Touch Keypad Entities (Touch / Touch Pro)

| Entity ID | Platform | Category | Description / Features |
| :--- | :--- | :--- | :--- |
| `sensor.<name>_battery` | `sensor` | Diagnostic | Reports real-time battery percentage. |
| `sensor.<name>_registered_cards` | `sensor` | Diagnostic | Reports the number of registered NFC/IC cards. |
| `sensor.<name>_registered_fingerprints` | `sensor` | Diagnostic | Reports the number of registered fingerprints. |
| `sensor.<name>_registered_passcodes` | `sensor` | Diagnostic | Reports the number of registered passcodes. Exposes the list of passcodes (names and hex IDs) in its `passcodes` state attribute. |
| `sensor.<name>_paired_locks` | `sensor` | Diagnostic | Reports the count of paired locks. Exposes detailed paired lock info (UUIDs, statuses) in its `paired_locks` state attribute. |
| `select.<name>_pair_lock` | `select` | Config | Dropdown list of HA-configured Sesame locks not yet paired to this keypad. Selecting a lock pairs it. |
| `select.<name>_unpair_lock` | `select` | Config | Dropdown list of locks paired to this keypad. Selecting a lock unpairs it. |

---

## Service Reference

The integration exposes the following local services under the `sesame_ble` domain:

### 1. `sesame_ble.add_passcode`
Adds a new passcode PIN to the Sesame Touch keypad (supporting optional timing constraints and one-time configuration).

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The target Sesame Touch keypad device. | *Select device* |
| `passcode` | String | Yes | Passcode PIN digits (4-16 numeric characters, 0-9). | `"123456"` |
| `name` | String | Yes | Name / Nickname to identify the passcode (max 20 characters). | `"John Doe"` |
| `start` | String | No | Date and time when the passcode becomes active (`YYYY-MM-DD HH:MM`). | `"2026-06-20 08:00"` |
| `end` | String | No | Date and time when the passcode expires and is auto-deleted (`YYYY-MM-DD HH:MM`). | `"2026-06-20 18:00"` |
| `days` | List | No | Days of week the passcode is active. Monday is `0`, Sunday is `6`. | `[0, 1, 2, 3, 4]` |
| `time_start` | String | No | Daily start time constraint (`HH:MM`). | `"09:00"` |
| `time_end` | String | No | Daily end time constraint (`HH:MM`). | `"17:00"` |
| `one_time` | Boolean | No | If `true`, the passcode is deleted automatically after a single successful unlock. | `true` |
| `person_id` | Entity | No | Home Assistant person entity ID to link unlock history. | `person.john_doe` |

---

### 2. `sesame_ble.delete_passcode`
Deletes an existing passcode from the Sesame Touch keypad.

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The target Sesame Touch device entity. | *Select device* |
| `passcode_or_id` | String | Yes | The passcode PIN (e.g. `"123456"`) or its unique Hex ID (e.g. `"0a0b0c..."`). | `"0a0b0c0d"` |

> [!TIP]
> You can retrieve the unique Hex ID of any passcode from the `Registered Passcodes` sensor's attributes. Using the Hex ID is recommended to avoid exposing raw PINs in automation logs.

---

### 3. `sesame_ble.update_passcode`
Renames/updates the nickname of an existing passcode.

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The target Sesame Touch device entity. | *Select device* |
| `passcode_or_id` | String | Yes | The passcode PIN or its unique Hex ID to rename. | `"0a0b0c0d"` |
| `name` | String | Yes | New name / nickname for the passcode (max 20 characters). | `"John (Work PIN)"` |

---

### 4. `sesame_ble.pair_lock`
Pairs a configured Sesame Lock device directly to the Sesame Touch keypad.

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The target Sesame Touch keypad device. | *Select device* |
| `lock_device_id` | Device | Yes | The Sesame Lock device configured in Home Assistant to pair. | *Select device* |

---

### 5. `sesame_ble.unpair_lock`
Unpairs a physical lock from the Sesame Touch keypad.

| Field | Type | Required | Description | Example |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | Device | Yes | The target Sesame Touch keypad device. | *Select device* |
| `lock_uuid` | String | Yes | The raw UUID of the lock to unpair. | `"01234567-89ab-cdef-0123-456789abcdef"` |

---

## Programmatic Passcodes & Advanced Schedules

Because scheduled, temporary, and one-time passcodes are supported directly in the `sesame_ble.add_passcode` service call, you can generate and manage them programmatically using standard Home Assistant automations and scripts.

### 🤖 Example Automations & Scripts

#### 1. Generating a One-Time Passcode (OTP / Disposable PIN)
This automation generates a one-time passcode for a guest when a specific trigger occurs. The passcode is automatically cleaned up after its first successful unlock:

```yaml
alias: "Generate Guest OTP"
trigger:
  - platform: state
    entity_id: input_button.generate_otp
action:
  - service: sesame_ble.add_passcode
    data:
      device_id: "your_keypad_device_id"
      name: "Temporary Guest PIN"
      passcode: "749204"
      one_time: true
```

#### 2. Creating a Temporary/Scheduled Passcode (Weekly Windows)
This automation configures a weekday-only passcode that automatically registers on the keypad during working hours (9 AM - 5 PM Monday-Friday) and is revoked/removed outside those hours:

```yaml
alias: "Create Cleaner Weekday Passcode"
trigger:
  - platform: homeassistant
    event: start
action:
  - service: sesame_ble.add_passcode
    data:
      device_id: "your_keypad_device_id"
      name: "Cleaner Schedule"
      passcode: "553311"
      days:
        - 0 # Monday
        - 1 # Tuesday
        - 2 # Wednesday
        - 3 # Thursday
        - 4 # Friday
      time_start: "09:00"
      time_end: "17:00"
```

---

## License

This project is licensed under the **GNU General Public License v3.0**. See the [LICENSE](LICENSE) file for the full text.
