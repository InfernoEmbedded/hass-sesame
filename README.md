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

- 🔑 **Candy House Sesame 5 / 5 Pro / 5 USA**
- 🔑 **Candy House Sesame 6 / 6 Pro**
- ⌨️ **Candy House Sesame Touch / Touch Pro**
- ⌨️ **Candy House Sesame Touch 2 / Touch 2 Pro**
- 👤 **Candy House Sesame Face / Face Pro / Face AI**
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

### Optional: Cloud Credentials for Firmware Updates (Options Flow)
To enable automatic cloud checks and one-click over-the-air (OTA) firmware updates via Home Assistant's `update` entity:
1. Go to **Settings** -> **Devices & Services** -> **Candy House Sesame BLE**.
2. Click **Configure** on any Sesame device entry.
3. Enter your **API Key** and **Cognito Identity Pool ID** (see [Firmware Updates & Cloud Credentials](#firmware-updates--cloud-credentials) below for extraction steps).

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

### 🔄 Firmware Update Entity (All Locks & Keypads)

| Entity ID | Platform | Category | Description / Features |
| :--- | :--- | :--- | :--- |
| `update.<name>_firmware_update` | `update` | Config | Monitors current firmware version against the latest Candy House release or local offline package. Enables triggering full over-the-air (OTA) Nordic DFU flashing directly from Home Assistant with live percentage progress tracking. |

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

## Firmware Updates & Cloud Credentials

The integration includes full support for over-the-air (OTA) Nordic DFU firmware flashing for all supported locks and keypads directly from Home Assistant or via the standalone `download_firmware.py` tool.

### What Works vs. What Won't Work Without Credentials

Candy House locks and keypads use direct BLE communication. **Cloud credentials are never required for normal, local operation.**

| Feature | Without Credentials | With Credentials |
| :--- | :---: | :---: |
| 🔒 **Local BLE Lock & Unlock** | ✅ **100% Local** | ✅ **100% Local** |
| 🔋 **Battery, Motor Angles & Telemetry** | ✅ **100% Local** | ✅ **100% Local** |
| ⌨️ **Keypad Passcodes, OTPs & Schedules** | ✅ **100% Local** | ✅ **100% Local** |
| 👤 **Face, Palm, Fingerprint & Card Access** | ✅ **100% Local** | ✅ **100% Local** |
| 📜 **Local Operation History Logs** | ✅ **100% Local** | ✅ **100% Local** |
| 🔗 **Keypad-to-Lock Pairing** | ✅ **100% Local** | ✅ **100% Local** |
| 📁 **Offline Firmware Flashing** (`/config/sesame_firmware/`) | ✅ **100% Local** | ✅ **100% Local** |
| ☁️ **Automatic Cloud Firmware Version Checks** | ❌ Skipped gracefully | ✅ Automatic via Candy House AWS API |
| ⬇️ **One-Click In-HA Cloud Firmware Download** | ❌ Manual `.zip` required | ✅ Automatic download & DFU flash |

> [!NOTE]
> **Your Sesame devices will work completely fine without cloud credentials.**
> All day-to-day operations communicate strictly between your Home Assistant Bluetooth adapter and the physical hardware over BLE.
>
> An **API Key** and **AWS Cognito Identity Pool ID** are **only** needed if you want Home Assistant to automatically poll Candy House's AWS API for newer firmware versions and download the DFU update archives from their cloud. If no credentials are configured and no local firmware file is provided, the `update` entity simply reports that cloud checks are skipped—it will **not** raise errors, fail to load, or degrade device control.

---

### How to Extract the API Credentials

Candy House's official Android mobile app contains static client credentials used to query their AWS API Gateway and Cognito services for firmware release manifests. You can extract these credentials in seconds using the offline tool provided in this repository: `tools/extract_apk_credentials.py`.

The extractor runs **100% locally and offline** without making any external network requests. It parses Dalvik Executable (DEX) bytecode using Python's standard library to retrieve the API key and unauthenticated Cognito Pool ID:

#### Method 1: From a Downloaded Sesame APK File
1. Download an official Sesame APK (from your phone or via an APK mirror such as APKPure or APKMirror).
2. Run the extractor:
   ```bash
   python3 tools/extract_apk_credentials.py path/to/Sesame.apk
   ```

#### Method 2: Extract Directly from Connected Android Phone via ADB
If you have an Android device with the Sesame app installed and USB Debugging enabled:
```bash
python3 tools/extract_apk_credentials.py --adb
```
*The script automatically detects connected devices via `adb`, locates the installed Sesame package (`co.candyhouse.sesame2`), pulls the APK to a temporary directory, extracts the credentials, and cleans up.*

#### Method 3: Output Directly to a Home Assistant Credentials File
```bash
python3 tools/extract_apk_credentials.py path/to/Sesame.apk --out /config/sesame_credentials.json
```

#### Additional Extractor Flags
- `--out <path>` / `-o <path>`: Save extracted credentials as JSON to the specified path.
- `--env`: Format output as shell `export` statements (`export SESAME_API_KEY=...`).
- `--json`: Output raw JSON to standard output.

---

### Where to Put the Credentials

The integration supports four configuration methods with automatic fallback priority:

#### 1. Home Assistant UI (Options Flow - Recommended)
This is the easiest method and applies immediately without restarting Home Assistant:
1. Navigate to **Settings** -> **Devices & Services** -> **Candy House Sesame BLE**.
2. Click **Configure** on any Sesame device entry.
3. Paste the **API Key** and **Cognito Pool ID** into the form fields.
4. Click **Submit**.

#### 2. JSON Credentials File (`sesame_credentials.json`)
Create a JSON file named `sesame_credentials.json` in your Home Assistant configuration directory (`/config/sesame_credentials.json`):
```json
{
  "api_key": "YOUR_EXTRACTED_API_KEY",
  "cognito_identity_pool_id": "ap-northeast-1:YOUR_EXTRACTED_COGNITO_POOL_ID"
}
```
*(Restrict permissions on the file: `chmod 600 /config/sesame_credentials.json`).*

You can also specify an alternate file path by setting the `SESAME_CREDENTIALS_FILE` environment variable.

#### 3. Environment Variables
If running Home Assistant Container, Supervised, or Core, set the environment variables:
```bash
export SESAME_API_KEY="YOUR_EXTRACTED_API_KEY"
export SESAME_COGNITO_POOL_ID="ap-northeast-1:YOUR_EXTRACTED_COGNITO_POOL_ID"
```

#### 4. Zero-Cloud Local Firmware Directory (No Keys Needed)
If you prefer not to configure cloud credentials, you can update firmware completely offline. Place downloaded Nordic DFU `.zip` packages in:
- `/config/sesame_firmware/` (or `/share/sesame_firmware/`)

Name the zip archive matching the device product type or model name (e.g. `prod_22.zip` or `sesame_face_pro_ai.zip`). The integration's `update` entity will detect local firmware archives automatically and offer them for installation over BLE!

---

### Firmware Downloader CLI Tool

The repository also includes `download_firmware.py`, a standalone CLI tool that queries Candy House's AWS cloud to download and unpack the latest Nordic DFU firmware archives for all 31 supported hardware models into a local `firmware/` folder:

```bash
# Download firmware for all 31 supported models using credentials file or env vars
python download_firmware.py

# Specify credentials directly via CLI flags
python download_firmware.py --api-key <KEY> --pool-id <POOL_ID>

# Download firmware for specific models (e.g. Sesame 6 Pro and Touch 2 Pro)
python download_firmware.py --models sesame6_pro sesame_touch_2_pro

# Check available versions without downloading archives (dry run)
python download_firmware.py --dry-run

# List all 31 supported hardware models and productType IDs
python download_firmware.py --list-models
```

Each downloaded model is staged in `firmware/<model_name>/` containing:
- `manifest.json`: Nordic DFU packet manifest and hardware version parameters.
- `firmware.bin`: ARM Cortex-M4 binary executed by both physical hardware and the simulation emulator.
- `firmware.dat`: Signed init packet for Nordic DFU verification.
- `metadata.json`: Release timestamps and file checksums.

> [!NOTE]
> The `firmware/` directory is gitignored by default so binary artifacts are never committed to version control. Downloaded binaries are directly consumed by the simulation environment (`sesame_sim`) to run authentic Candy House firmware in memory.

---

## Sesame Hardware Simulation Environment

You can simulate Sesame devices without any physical hardware using the integrated simulation framework (`sesame_sim`). It runs the authentic Candy House firmware binaries inside an ARM Cortex-M4 **Unicorn Engine** emulator with an in-memory BLE bridge:

### 1. Launch the Interactive Web GUI
```bash
python -m sesame_sim
```
Open **http://127.0.0.1:8088** in your browser to interact with the simulated hardware:

**Interactive Features:**
- 🔄 **Real-Time Rotary Motor Dial**: Visual SVG thumbturn showing real motor position versus target angle, smooth CSS rotation animations, and manual drag-to-turn controls.
- 🔢 **Backlit 3x4 Keypad**: Functional numeric matrix with audio feedback, backspace (`*`), and enter (`#`) validating PINs against the emulated database.
- 🔊 **Synthesized Audio & Visual Buzzer**: Realistic piezo buzzer tones and mechanical click sounds synthesized via the Web Audio API.
- 🚦 **Tri-Color Status LEDs**: Real-time Red/Blue/Green status indicators replicating physical hardware behavior.
- 👆 **Biometric & NFC Simulation**: One-click fingerprint touch and NFC/RFID card tap emulation.
- 🔗 **Cross-Device Interactivity**: Entering a valid PIN, scanning an enrolled card, or touching a matched fingerprint automatically drives the linked Sesame 6 Pro lock motor to unlock.

---

## Testing Against the Hardware Simulator

The simulation environment allows rapid end-to-end testing without Bluetooth dongles, mobile apps, or physical hardware.

### 1. Automated Test Suite (Pytest)
Run rigorous automated unit and integration tests directly against the emulated firmware:

```bash
# Run all hardware simulation tests (33 tests)
pytest tests/test_simulated_lock_rigorous.py tests/test_simulated_keypad_rigorous.py tests/test_simulation_emulator.py tests/test_virtual_ble_bridge.py -v

# Test Sesame 6 Pro lock operations (handshake, session key validation, motor animation, thumbturn, history log sync):
pytest tests/test_simulated_lock_rigorous.py -v

# Test Touch 2 Pro keypad operations (passcode CRUD, fingerprint/card auth, linked lock motor actuation):
pytest tests/test_simulated_keypad_rigorous.py -v

# Test low-level ARM Cortex-M4 Unicorn emulation and OnMicro ROM HLE dispatch:
pytest tests/test_simulation_emulator.py -v

# Test in-memory Bleak GATT client/server loopback transport:
pytest tests/test_virtual_ble_bridge.py -v
```

### 2. Standalone CLI Testing (`sesame_cli.py --simulator`)
Use the `--simulator` flag to execute commands against simulated hardware running authentic firmware in memory:

```bash
# Query simulated Sesame 6 Pro status
python sesame_cli.py --simulator --address FD:81:AA:BB:CC:21 --status

# Unlock simulated Sesame 6 Pro (animates motor from 0° to 90°)
python sesame_cli.py --simulator --address FD:81:AA:BB:CC:21 --unlock

# Lock simulated Sesame 6 Pro (animates motor back to 0°)
python sesame_cli.py --simulator --address FD:81:AA:BB:CC:21 --lock

# Query simulated Touch 2 Pro keypad status
python sesame_cli.py --simulator --address FD:81:AA:BB:CC:26 --status

# Retrieve and sync all enrolled passcodes on simulated keypad
python sesame_cli.py --simulator --address FD:81:AA:BB:CC:26 --get-passcodes

# Add a new passcode to the simulated keypad
python sesame_cli.py --simulator --address FD:81:AA:BB:CC:26 --add-passcode "123456" "Guest PIN"

# Delete a passcode from the simulated keypad
python sesame_cli.py --simulator --address FD:81:AA:BB:CC:26 --delete-passcode "123456"
```

---

## Testing Against Real Physical Hardware Over BLE

To test against physical Sesame locks and keypads using your host machine's Bluetooth adapter:

### 1. Prerequisites
- **Bluetooth Adapter**: BLE 4.0 or higher adapter (internal or USB dongle).
- **Linux Permissions**: Ensure the `bluetooth` daemon is running (`systemctl status bluetooth`). Ensure your user has permissions to access the Bluetooth adapter (member of `bluetooth` group or run with appropriate privileges).
- **Wake Device**: Ensure batteries are installed in your physical Sesame lock or keypad and that it is within range.

### 2. Discover Nearby Hardware
Scan for physical Sesame devices advertising over BLE:

```bash
python sesame_cli.py --scan
```
*Output displays device names, BLE MAC addresses, model identifiers, and registration status.*

### 3. Query Real Hardware Status
Connect, perform session key negotiation, and read live telemetry (battery voltage, motor angle, credential counts):

```bash
# Using 32-character Hex Secret Key:
python sesame_cli.py --address "DE:AD:BE:EF:01:02" --secret "00112233445566778899aabbccddeeff" --status

# Or using the official Sesame App QR Code URL:
python sesame_cli.py --address "DE:AD:BE:EF:01:02" --qr-url "ssm://UI?uuid=...&key=...&m=..." --status
```

### 4. Test Lock & Unlock on Physical Lock
Control the physical motor:

```bash
# Unlock physical lock
python sesame_cli.py --address "DE:AD:BE:EF:01:02" --secret "your_32char_hex_secret" --unlock

# Lock physical lock
python sesame_cli.py --address "DE:AD:BE:EF:01:02" --secret "your_32char_hex_secret" --lock
```

### 5. Test Passcode Management on Physical Keypad
Sync, add, or delete PINs on physical Sesame Touch / Touch Pro / Touch 2 keypads:

```bash
# Sync and list all passcodes stored on the physical keypad
python sesame_cli.py --address "DE:AD:BE:EF:03:04" --secret "your_32char_hex_secret" --get-passcodes

# Add a new test passcode to the physical keypad
python sesame_cli.py --address "DE:AD:BE:EF:03:04" --secret "your_32char_hex_secret" --add-passcode "987654" "TestPIN"

# Delete the passcode by PIN or Hex ID
python sesame_cli.py --address "DE:AD:BE:EF:03:04" --secret "your_32char_hex_secret" --delete-passcode "987654"
```

### 6. Register a Factory-Reset Device
To register a factory-fresh or reset device and retrieve the generated secret key:

```bash
python sesame_cli.py --address "DE:AD:BE:EF:01:02" --register
```

---

## Running the Automated Test Suite

The repository contains an extensive automated test suite covering protocol compatibility, cryptography, Home Assistant config flows, entity platforms, hardware simulation, and Nordic DFU firmware downloader logic:

```bash
# Run the complete test suite (132 tests)
pytest -v

# Run hardware simulation tests
pytest tests/test_simulated_lock_rigorous.py tests/test_simulated_keypad_rigorous.py tests/test_simulation_emulator.py tests/test_virtual_ble_bridge.py -v

# Run Home Assistant integration and config flow tests
pytest tests/test_hass_integration.py tests/test_config_flow.py -v

# Run protocol and crypto compatibility tests
pytest tests/test_protocol_compatibility.py tests/test_crypto_compatibility.py -v

# Run firmware downloader tests
pytest tests/test_download_firmware.py -v
```

---

## License

This project is licensed under the **GNU General Public License v3.0**. See the [LICENSE](LICENSE) file for the full text.

