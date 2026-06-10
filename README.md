# iM.Master Robot Control - BLE Protocol Reverse Engineering

Reverse engineering of the BLE control protocol used by the **iM.Master No. 8052** building block robot kit.

## The Robot

- **Product**: iM.Master No. 8052
- **Pieces**: 552 PCS
- **Configurations**: 4-in-1 (Tank / Robot / Rubik's Cube / Snow Plow)
- **Manufacturer**: Shantou Leshow Technology Co., Ltd.
- **Battery**: 7.4V 300mAh lithium
- **Product page**: https://immastertech.com/r-c-robots-9/

## The App

- **Name**: iM.Master
- **Package**: `com.tyb.smartcontrol`
- **Version**: 1.0.33
- **Source**: APKPure (XAPK format)
- **Play Store**: https://play.google.com/store/apps/details?id=com.tyb.smartcontrol

### Control Modes

The app supports multiple control modes:
- Conventional button remote control
- Joystick control (multiple layouts)
- Gravity sensing
- Line drawing path control
- Action programming (Blockly-style visual programming)
- Single-hand mode

## BLE Protocol

### Key Discovery: Advertising-Based Communication

This robot does **NOT** use standard BLE GATT connections. Instead, it communicates entirely via **BLE Advertising manufacturer-specific data**. The phone acts as both a BLE advertiser (sending commands) and scanner (receiving responses).

This is unusual - most BLE devices use GATT connections. The advertising approach means:
- No pairing required
- No persistent connection
- Commands are broadcast, not point-to-point
- Multiple devices can potentially receive the same command
- Range may be more limited than GATT connections

### Manufacturer Company ID

```
Company ID: 61951 (0xF1FF)
```

This is **not** a registered Bluetooth SIG company identifier. It appears to be a custom/fake ID.

### Pairing / Discovery

1. Phone generates a random 3-byte **phone ID** (stored in SharedPreferences as `DevId`)
2. Phone generates a **password** (`apwd`) from 3 random values (1-84 each), though in practice this is set to `0` with `devApwd = "00 00 00"`
3. Phone advertises a **search packet** with marker byte `E1`
4. Robot responds by advertising with marker byte `D2` and its own 3-byte **device ID**
5. Phone scans for manufacturer data with company ID `61951`, checks for 14-byte response with `D2` at position 12
6. Device type byte (position 13) must match expected type (`98` or `99`)

### Packet Format

Commands are sent as the manufacturer-specific data payload in BLE advertisements. The packet is **14 bytes**:

```
Byte  0: Phone ID byte 1
Byte  1: Phone ID byte 2  
Byte  2: Phone ID byte 3
Byte  3: Device ID byte 1
Byte  4: Device ID byte 2
Byte  5: Device ID byte 3
Byte  6: 0x00 (reserved)
Byte  7: 0x00 (reserved)
Byte  8: Password (hex encoded, typically 0x00)
Byte  9: Command data (encoded motor/direction value)
Byte 10: Light control string
Byte 11: 0x00 (reserved)
Byte 12: Command data (duplicate of byte 9)
Byte 13: CRC checksum
```

Followed by fixed trailer: `C3` and then the device type byte.

Actually the full format string from the code is:
```
[phoneID] [devID] 00 00 [pwd] [cmd] [light] 00 [cmd] [crc] C3 [devType]
```

Where `[phoneID]` is 1 byte (3rd byte of the 3-byte phone ID) and `[devID]` is 3 bytes.

### BLE Advertiser Settings

```
Advertise Mode: LOW_LATENCY (2)
TX Power Level: HIGH (3)  
Connectable: true
Timeout: 1000ms
```

The app maintains up to **10 concurrent advertisers** and refreshes every ~50ms.

### Command Encoding

Commands are encoded as 8-bit binary strings that get converted to a hex byte:

```
Format: [speed_2bits][00][motor_4bits]

Speed bits:
  01 = Speed 1 (slow)
  10 = Speed 2 (medium)
  11 = Speed 3 (fast)
```

### Motor Control Bits (4-bit command)

The 4 bits represent motor directions for a 2-motor (left/right) differential drive:

```
Bits: [Left_Forward] [Left_Backward] [Right_Forward] [Right_Backward]

Movement commands (default, no direction swap):
  1010 = Forward      (both motors forward)
  0101 = Backward     (both motors backward)
  1000 = Turn Left    (left motor forward only)
  0010 = Turn Right   (right motor forward only)
  0110 = Spin Left    (left back, right forward)
  1001 = Spin Right   (left forward, right back)
  0000 = Stop

Combined directions:
  0001 = Reverse Right (right motor backward only)
  0100 = Reverse Left  (left motor backward only)
```

Note: The app supports swapping motor directions via `isChangeL` and `isChangeR` flags, which mirror the bit patterns.

### CRC Calculation

The CRC is calculated by counting the number of `1` bits in the binary command string:

```java
int crc = count_of_1_bits_in_binary_command_string;
if (crc < password) {
    crc += 256;
}
// Then: crc_byte = hex(crc - password)
```

Since password is typically 0, the CRC is simply the popcount of the binary command string, expressed as a hex byte.

### Password Encoding

Values are "encrypted" by subtracting the password:

```java
encoded = value - password;
if (value < password) {
    encoded = (value + 256) - password;
}
```

Since password (`apwd`) is hardcoded to 0, values are effectively unencrypted.

### Device Types

```
Type "98": Standard model (3-channel control - uses 6-bit command format)
Type "99": Extended model (4WD drift variant, index 1 of type 98)
```

The device type affects motor binding and direction mapping.

### Light Control

```
Byte 10 format: "0X" where X = color_setting + 1

Color settings stored per direction:
  - az: color for left turn
  - af: color for right turn  
  - a: color for straight/stop
```

## Advertising Flow

```
Phone                          Robot
  |                              |
  |--- Advertise (search) ----->|  (marker E1, devType)
  |                              |
  |<--- Advertise (response) ---|  (marker D2, devID, devType)
  |                              |
  |    [Phone records devID]     |
  |                              |
  |--- Advertise (commands) --->|  (motor data, every ~50ms)
  |                              |
```

## Linux Implementation

### Why BlueZ Management Socket?

The standard approach to BLE advertising on Linux is via BlueZ's D-Bus `LEAdvertisement1` interface. However, this **does not work** with the TP-Link UB500 adapter (RTL8761B chipset) — BlueZ returns "Invalid Parameters (0x0d)" from the HCI layer when trying to register advertisements via D-Bus.

Three approaches were evaluated:

| Approach | Result |
|----------|--------|
| BlueZ D-Bus `LEAdvertisement1` | ❌ Fails with "Invalid Parameters" on RTL8761B chipset |
| Raw HCI socket (`BTPROTO_HCI`) | ❌ Requires `HCI_CHANNEL_USER` (exclusive access, disconnects BlueZ) |
| BlueZ Management socket (`HCI_CHANNEL_CONTROL`) | ✅ Works — can add/remove advertisements while BlueZ remains active |

The management socket (`mgmt`) talks directly to the kernel's Bluetooth management layer, bypassing bluetoothd's parameter validation that the RTL8761B rejects.

### Keepalive Requirement

The robot treats BLE advertisements as a heartbeat. If it stops receiving packets, it disconnects (LED flashes). The Android app re-advertises every **50ms** — even when idle it sends a "stop" command to maintain the connection. Our implementation uses an async background loop that continuously re-registers the advertisement at the same interval.

### Setup

#### 1. Install dependencies

```bash
cd /home/mark/git/robotcontrol
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 2. Grant BLE capabilities to Python

Raw HCI/mgmt sockets require `CAP_NET_RAW` and `CAP_NET_ADMIN`:

```bash
sudo setcap 'cap_net_raw,cap_net_admin+eip' $(readlink -f venv/bin/python3)
```

#### 3. Install D-Bus policy (optional, for future BlueZ D-Bus use)

```bash
sudo cp dbus/org.bluez.robot.conf /etc/dbus-1/system.d/
sudo systemctl reload dbus
```

### Usage

```bash
source venv/bin/activate

# Interactive mode
python3 robot_cli.py -t 99

# One-shot commands
python3 robot_cli.py forward -t 99 -d 2 -s 3    # forward, 2 seconds, speed 3
python3 robot_cli.py spin_left -t 99 -d 1 -s 2  # spin left, 1 second, speed 2
```

### Hardware

- **Bluetooth adapter**: TP-Link UB500 (USB, VID:PID 2357:0604, RTL8761B chipset)
- **BLE features**: Supports LE advertising (4 instances), central and peripheral roles
- **Known limitation**: BlueZ D-Bus advertising API fails on this chipset; mgmt socket works

## Existing Reverse Engineering

No existing reverse engineering efforts for this specific robot/app were found online. This appears to be the first public documentation of this protocol.

## Source Files

Key decompiled classes (in `apk/decompiled/sources/com/tyb/smartcontrol/`):

| File | Purpose |
|------|---------|
| `ble/BleHandler.java` | Low-level BLE advertising start/stop |
| `ble/BluetoothServiceHandler.java` | BLE scanning, device discovery |
| `ble/BleToolHandler.java` | Command queue and advertising management |
| `ble/BluetoothUtils.java` | Hex conversion utilities |
| `handler/HexHandler.java` | Byte/hex conversion |
| `model/DevInfo.java` | Device config, motor bindings, direction mapping |
| `BaseActivity.java` | Common control logic, command construction |
| `tool/Tools.java` | Device ID generation, preferences |

## Next Steps

- [x] Build a Python/Linux BLE controller
- [x] Verify protocol works with real robot (type 99 confirmed)
- [ ] Verify protocol by sniffing actual BLE traffic with nRF Connect
- [ ] Document Type 98 protocol differences
- [ ] Test light control values
- [ ] Determine if multiple robots can be controlled independently
