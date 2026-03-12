"""
i2c_sensor.py — Generic I2C sensor helper for Raspberry Pi
Wraps smbus2 for raw register reads/writes and provides a base class
to subclass for specific sensor ICs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import smbus2

log = logging.getLogger(__name__)


# ── Bus utilities ─────────────────────────────────────────────────────────────

def scan_i2c_bus(bus: int = 1) -> list[int]:
    """
    Scan an I2C bus and return a list of responding device addresses (7-bit).
    Mirrors the behaviour of `i2cdetect -y <bus>`.
    """
    found = []
    try:
        with smbus2.SMBus(bus) as b:
            for addr in range(0x03, 0x78):  # valid 7-bit range
                try:
                    b.read_byte(addr)
                    found.append(addr)
                except OSError:
                    pass  # no device at this address
    except FileNotFoundError:
        log.error("I2C bus %d not found. Enable it with: sudo raspi-config", bus)
    return found


# ── Base sensor class ─────────────────────────────────────────────────────────

@dataclass
class I2CSensor:
    """
    Lightweight wrapper around a single I2C device.

    Subclass this and override `read()` / `parse()` for specific ICs,
    or use the raw helpers (`read_register`, `write_register`) directly.
    """

    bus:     int
    address: int
    name:    str = "I2CSensor"
    _smbus:  Optional[smbus2.SMBus] = field(default=None, init=False, repr=False)

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def open(self) -> None:
        """Open the underlying SMBus handle. Called automatically on first use."""
        if self._smbus is None:
            self._smbus = smbus2.SMBus(self.bus)
            log.debug("[%s] opened bus %d", self.name, self.bus)

    def close(self) -> None:
        """Release the SMBus handle."""
        if self._smbus is not None:
            self._smbus.close()
            self._smbus = None
            log.debug("[%s] closed bus %d", self.name, self.bus)

    def __enter__(self) -> "I2CSensor":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Raw register access ───────────────────────────────────────────────────

    def read_register(self, register: int, length: int = 1) -> list[int]:
        """
        Read `length` bytes starting at `register`.
        Returns a list of ints (one per byte).
        """
        self.open()
        return self._smbus.read_i2c_block_data(self.address, register, length)

    def read_byte_register(self, register: int) -> int:
        """Read a single byte from `register`."""
        self.open()
        return self._smbus.read_byte_data(self.address, register)

    def read_word_register(self, register: int, little_endian: bool = True) -> int:
        """
        Read a 16-bit unsigned word from `register`.
        Most sensors use little-endian (LSB first).
        """
        data = self.read_register(register, 2)
        if little_endian:
            return data[0] | (data[1] << 8)
        return (data[0] << 8) | data[1]

    def write_register(self, register: int, value: int) -> None:
        """Write a single byte `value` to `register`."""
        self.open()
        self._smbus.write_byte_data(self.address, register, value)

    def write_block(self, register: int, data: list[int]) -> None:
        """Write a block of bytes starting at `register`."""
        self.open()
        self._smbus.write_i2c_block_data(self.address, register, data)

    # ── Default read — override in subclasses ─────────────────────────────────

    def read(self, length: int = 1) -> bytes:
        """
        Default: read `length` raw bytes from the device (no register address).
        Override this in subclasses to return parsed sensor data.
        """
        self.open()
        raw = self._smbus.read_i2c_block_data(self.address, 0x00, length)
        return bytes(raw)

    def who_am_i(self, register: int = 0x0F) -> int:
        """
        Read the WHO_AM_I register (0x0F is common across many ST sensors).
        Returns the device ID byte.
        """
        return self.read_byte_register(register)


# ── Example subclass: ADS1115 16-bit ADC ─────────────────────────────────────

class ADS1115(I2CSensor):
    """
    Texas Instruments ADS1115 — 16-bit, 4-channel ADC.
    Default I2C address: 0x48  (ADDR pin → GND)

    Usage:
        adc = ADS1115(bus=1, address=0x48)
        voltage = adc.read_single_ended(channel=0)
    """

    # Register map
    _REG_CONVERSION = 0x00
    _REG_CONFIG     = 0x01

    # Config: OS=1 (single), MUX=AIN0/GND, PGA=±4.096 V, MODE=single-shot,
    #         DR=128 SPS, COMP disabled
    _CONFIG_BASE = 0x8583

    # PGA full-scale voltage table (V) indexed by PGA[2:0]
    _PGA_FS = {0: 6.144, 1: 4.096, 2: 2.048, 3: 1.024, 4: 0.512, 5: 0.256}

    def __init__(self, bus: int = 1, address: int = 0x48, pga: int = 1):
        """
        pga: PGA gain setting (default 1 → ±4.096 V, ~0.125 mV/LSB).
        """
        super().__init__(bus=bus, address=address, name="ADS1115")
        self._pga = pga
        self._fs_voltage = self._PGA_FS[pga]

    def read_single_ended(self, channel: int = 0) -> float:
        """
        Start a single-shot conversion on AIN{channel}/GND and return voltage (V).
        channel: 0–3
        """
        if channel not in range(4):
            raise ValueError(f"Channel must be 0-3, got {channel}")

        mux = 0x4000 | (channel << 12)   # MUX[14:12] = 100b + channel
        pga = (self._pga & 0x07) << 9
        config = self._CONFIG_BASE | mux | pga

        # Write config (big-endian)
        self.write_block(self._REG_CONFIG, [(config >> 8) & 0xFF, config & 0xFF])

        # Wait for conversion (max 8 ms at 128 SPS)
        import time
        time.sleep(0.009)

        # Read result
        raw = self.read_word_register(self._REG_CONVERSION, little_endian=False)

        # Convert to signed 16-bit
        if raw > 32767:
            raw -= 65536

        return round(raw * (self._fs_voltage / 32768.0), 6)

    def read(self, length: int = 2) -> bytes:
        """Override: return raw conversion register bytes."""
        return bytes(self.read_register(self._REG_CONVERSION, 2))


class VL53L4CD(I2CSensor):
    """
    VL53L4CD breakout with custom host-firmware command protocol.

    Default protocol address is 0x70. Commands are sent as documented in
    I2C_COMMANDS_VL53L4CD.md and read back from a 15-byte response buffer.
    """

    UNIT_MM = 0x52
    UNIT_CM = 0x51
    UNIT_IN = 0x50

    CMD_RANGING = 0x00
    CMD_SET_TIMING = 0x01
    CMD_OFFSET_CAL = 0x02
    CMD_XTALK_CAL = 0x03
    CMD_READ_CONFIG = 0x04
    CMD_RESTORE_DEFAULTS = 0x05
    CMD_SET_THRESHOLDS = 0x07
    CMD_RESTART = 0x08

    def __init__(self, bus: int = 1, address: int = 0x70):
        super().__init__(bus=bus, address=address, name="VL53L4CD")

    def _write_cmd(self, command: int, payload: list[int] | None = None) -> None:
        """Write a command byte + optional payload to the sensor."""
        self.open()
        if payload is None:
            payload = []
        self._smbus.write_i2c_block_data(self.address, command, payload)

    def set_timing(self, time_budget_ms: int, inter_measurement_ms: int) -> None:
        """Configure timing (command 0x01)."""
        tb = max(10, min(200, int(time_budget_ms)))
        im = max(0, min(5000, int(inter_measurement_ms)))
        payload = [tb, (im >> 8) & 0xFF, im & 0xFF]
        self._write_cmd(self.CMD_SET_TIMING, payload)

    def read_config(self) -> dict:
        """Read 13-byte device configuration buffer (command 0x04)."""
        self._write_cmd(self.CMD_READ_CONFIG)
        data = self.read(length=13)
        offset = (data[4] << 8) | data[5]
        if offset > 32767:
            offset -= 65536
        return {
            "i2c_address": data[0],
            "time_budget_ms": data[1],
            "inter_measurement_ms": (data[2] << 8) | data[3],
            "offset_mm": offset,
            "xtalk_kcps": (data[6] << 8) | data[7],
            "sigma_threshold_mm": (data[8] << 8) | data[9],
            "signal_threshold_kcps": (data[10] << 8) | data[11],
            "firmware_rev": data[12],
        }

    def trigger_ranging(self, unit: int = UNIT_MM) -> None:
        """Trigger single ranging measurement (command 0x00)."""
        self._write_cmd(self.CMD_RANGING, [unit])

    def read_ranging_result(
        self,
        initial_wait_s: float = 0.05,
        poll_interval_s: float = 0.005,
        timeout_s: float = 0.5,
    ) -> dict:
        """
        Trigger one range in mm, poll until range_status == 0 (data ready),
        then return parsed result.

        Returns None values if the measurement times out or stays invalid.
        """
        self.trigger_ranging(self.UNIT_MM)

        # Initial wait — let sensor start the measurement before we start polling
        time.sleep(initial_wait_s)

        deadline = time.monotonic() + timeout_s
        data = None
        while time.monotonic() < deadline:
            buf = self.read(length=15)
            range_status = buf[2]
            distance = (buf[0] << 8) | buf[1]
            # status 0 = valid, distance 0xFFFF = buffer not yet filled
            if range_status == 0 and distance != 0xFFFF:
                data = buf
                break
            time.sleep(poll_interval_s)

        if data is None:
            log.warning("[VL53L4CD] Ranging timed out — data not ready within %.1f s", timeout_s)
            return {
                "distance_mm": None,
                "range_status": None,
                "signal_rate_kcps_raw": None,
                "ambient_rate_kcps_raw": None,
                "sigma_mm_raw": None,
                "ambient_per_spad_raw": None,
                "signal_per_spad_raw": None,
                "num_spads_raw": None,
            }

        return {
            "distance_mm": (data[0] << 8) | data[1],
            "range_status": data[2],
            "signal_rate_kcps_raw": (data[3] << 8) | data[4],
            "ambient_rate_kcps_raw": (data[5] << 8) | data[6],
            "sigma_mm_raw": (data[7] << 8) | data[8],
            "ambient_per_spad_raw": (data[9] << 8) | data[10],
            "signal_per_spad_raw": (data[11] << 8) | data[12],
            "num_spads_raw": (data[13] << 8) | data[14],
        }

    def set_thresholds(self, sigma_mm: int, signal_kcps: int) -> None:
        """Set sigma/signal thresholds (command 0x07)."""
        payload = [
            (sigma_mm >> 8) & 0xFF,
            sigma_mm & 0xFF,
            (signal_kcps >> 8) & 0xFF,
            signal_kcps & 0xFF,
        ]
        self._write_cmd(self.CMD_SET_THRESHOLDS, payload)

    def restore_defaults(self) -> None:
        """Restore defaults from firmware command 0x05."""
        self._write_cmd(self.CMD_RESTORE_DEFAULTS)

    def restart(self) -> None:
        """Reload EEPROM values from firmware command 0x08."""
        self._write_cmd(self.CMD_RESTART)

    def read(self, length: int = 15) -> bytes:
        """Read response buffer from command protocol (register pointer 0x00)."""
        self.open()
        return bytes(self._smbus.read_i2c_block_data(self.address, 0x00, length))
