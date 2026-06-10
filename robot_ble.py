"""BLE controller for iM.Master robot using BlueZ mgmt socket for advertising."""

import asyncio
import ctypes
import ctypes.util
import os
import random
import socket
import struct
import time

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

COMPANY_ID = 61951  # 0xF1FF - robot response company ID
COMPANY_ID_ALT = 65521  # 0xFFF1 - alternate byte order
MGMT_OP_ADD_ADVERTISING = 0x003E
MGMT_OP_REMOVE_ADVERTISING = 0x003F

# Motor bit patterns: [LF][LB][RF][RB]
COMMANDS = {
    "forward": "1010",
    "backward": "0101",
    "left": "1000",
    "right": "0010",
    "spin_left": "0110",
    "spin_right": "1001",
    "stop": "0000",
}

SPEEDS = {1: "01", 2: "10", 3: "11"}


class MgmtSocket:
    """BlueZ management socket for LE advertising."""

    def __init__(self, hci_index: int = 0):
        self._index = hci_index
        self._sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, 1)
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        sockaddr = struct.pack("<HHH", 31, 0xFFFF, 3)  # AF_BT, HCI_DEV_NONE, HCI_CHANNEL_CONTROL
        ret = libc.bind(self._sock.fileno(), sockaddr, len(sockaddr))
        if ret != 0:
            raise OSError(f"mgmt bind failed: {os.strerror(ctypes.get_errno())}")
        self._sock.settimeout(2.0)
        # Drain pending events
        try:
            while True:
                self._sock.recv(1024)
        except socket.timeout:
            pass

    def _send_cmd(self, opcode: int, params: bytes) -> int:
        """Send mgmt command, return status byte."""
        # Drain any pending events first
        self._sock.settimeout(0.01)
        try:
            while True:
                self._sock.recv(1024)
        except (socket.timeout, BlockingIOError):
            pass
        self._sock.settimeout(2.0)

        cmd = struct.pack("<HHH", opcode, self._index, len(params)) + params
        self._sock.send(cmd)
        time.sleep(0.1)
        try:
            resp = self._sock.recv(1024)
            if len(resp) >= 9:
                cmd_op, status = struct.unpack_from("<HB", resp, 6)
                return status
        except socket.timeout:
            pass
        return -1

    def add_advertisement(self, instance: int, adv_data: bytes) -> bool:
        """Register a BLE advertisement with given AD structures."""
        params = struct.pack("<B", instance)
        params += struct.pack("<I", 1)       # flags: connectable
        params += struct.pack("<H", 0)       # duration (0 = indefinite)
        params += struct.pack("<H", 0)       # timeout (0 = no timeout)
        params += struct.pack("<B", len(adv_data))  # adv_data_len
        params += struct.pack("<B", 0)       # scan_rsp_len
        params += adv_data
        return self._send_cmd(MGMT_OP_ADD_ADVERTISING, params) == 0

    def remove_advertisement(self, instance: int) -> bool:
        """Remove a BLE advertisement."""
        params = struct.pack("<B", instance)
        return self._send_cmd(MGMT_OP_REMOVE_ADVERTISING, params) == 0

    def close(self):
        self._sock.close()


class RobotController:
    """Controls the iM.Master robot via BLE advertising."""

    def __init__(self, speed: int = 3):
        self.phone_id = bytes([random.randint(1, 254) for _ in range(3)])
        self.device_id: bytes | None = None
        self.device_type: str = "98"
        self.speed = speed
        self.password = 0
        self._mgmt: MgmtSocket | None = None
        self._ad_instance = 1
        self._current_payload: bytes | None = None
        self._keepalive_task: asyncio.Task | None = None

    def _get_mgmt(self) -> MgmtSocket:
        if self._mgmt is None:
            self._mgmt = MgmtSocket()
        return self._mgmt

    def _release_mgmt(self):
        """Release mgmt socket so BlueZ can use the adapter."""
        if self._mgmt:
            self._mgmt.remove_advertisement(self._ad_instance)
            self._mgmt.close()
            self._mgmt = None

    def _build_command_byte(self, motor_bits: str) -> int:
        binary = SPEEDS[self.speed] + "00" + motor_bits
        return int(binary, 2)

    def _crc(self, motor_bits: str) -> int:
        binary = SPEEDS[self.speed] + "00" + motor_bits
        return bin(int(binary, 2)).count("1")

    def _make_packet(self, motor_bits: str, light: int = 0) -> bytes:
        """Build the BLE payload for the robot."""
        cmd = self._build_command_byte(motor_bits)
        crc = self._crc(motor_bits)
        dev_id = self.device_id or b"\x00\x00\x00"

        return bytes([
            self.phone_id[2],
            dev_id[0], dev_id[1], dev_id[2],
            0x00, 0x00,
            self.password & 0xFF,
            cmd,
            light,
            0x00,
            cmd,
            crc,
            0xC3,
            int(self.device_type, 16),
        ])

    def _make_search_packet(self) -> bytes:
        dt_byte = int(self.device_type, 16)
        return bytes([
            self.phone_id[2],
            0x00, 0x00, 0x00,
            0x00, 0x00, 0x00,
            0x00, 0x00, 0x00,
            0x00, 0x00,
            0xE1,
            dt_byte,
        ])

    def _get_adv_company_id(self) -> int:
        """Company ID for our advertisements = phone_id[1] << 8 | phone_id[0]."""
        return (self.phone_id[1] << 8) | self.phone_id[0]

    def _make_ad_struct(self, payload: bytes) -> bytes:
        """Wrap payload in AD structure: [len][type=0xFF][company_lo][company_hi][data]."""
        cid = self._get_adv_company_id()
        cid_lo = cid & 0xFF
        cid_hi = (cid >> 8) & 0xFF
        return bytes([len(payload) + 3, 0xFF, cid_lo, cid_hi]) + payload

    def _advertise(self, payload: bytes) -> bool:
        """Set the current advertisement data."""
        mgmt = self._get_mgmt()
        mgmt.remove_advertisement(self._ad_instance)
        ad_data = self._make_ad_struct(payload)
        return mgmt.add_advertisement(self._ad_instance, ad_data)

    def _start_keepalive(self):
        """Start background task that continuously re-advertises."""
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    def _stop_keepalive(self):
        """Stop the keepalive loop."""
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def _keepalive_loop(self):
        """Re-register advertisement periodically to prevent adapter timeout."""
        try:
            while True:
                payload = self._current_payload
                if payload is not None:
                    self._advertise(payload)
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass

    async def connect(self, timeout: float = 10.0) -> bool:
        """Discover robot and start keepalive."""
        if not await self.discover(timeout):
            return False
        # Start keepalive with stop command
        self._current_payload = self._make_packet(COMMANDS["stop"])
        self._start_keepalive()
        return True

    async def discover(self, timeout: float = 10.0) -> bool:
        """Discover the robot: advertise search packet and scan for response."""
        print(f"Searching for robot (type {self.device_type})...")

        search_data = self._make_search_packet()
        if not self._advertise(search_data):
            print("Failed to start search advertisement")
            return False
        print("Broadcasting search... scanning for response...")

        found = asyncio.Event()

        def _detection_callback(device: BLEDevice, ad_data: AdvertisementData):
            mfr = ad_data.manufacturer_data.get(COMPANY_ID)
            if mfr is None:
                mfr = ad_data.manufacturer_data.get(COMPANY_ID_ALT)
            if mfr and len(mfr) >= 14:
                if mfr[12] == 0xD2:
                    expected = int(self.device_type, 16)
                    if mfr[13] == expected:
                        self.device_id = bytes(mfr[3:6])
                        print(f"Found robot! Device ID: {self.device_id.hex()}")
                        found.set()

        scanner = BleakScanner(detection_callback=_detection_callback)
        await scanner.start()
        try:
            await asyncio.wait_for(found.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print("Robot not found within timeout.")
        finally:
            await scanner.stop()
            self._get_mgmt().remove_advertisement(self._ad_instance)

        return self.device_id is not None

    async def send_command(self, command: str, duration: float = 0.0):
        """Send a movement command."""
        if command not in COMMANDS:
            raise ValueError(f"Unknown command: {command}. Use: {list(COMMANDS.keys())}")

        motor_bits = COMMANDS[command]
        self._current_payload = self._make_packet(motor_bits)
        print(f"Sending: {command} (speed {self.speed})")

        if duration > 0:
            await asyncio.sleep(duration)
            await self.stop()

    async def stop(self):
        """Stop the robot."""
        self._current_payload = self._make_packet(COMMANDS["stop"])
        print("Stopped")

    def close(self):
        """Clean up."""
        self._stop_keepalive()
        if self._mgmt:
            self._mgmt.remove_advertisement(self._ad_instance)
            self._mgmt.close()
            self._mgmt = None
