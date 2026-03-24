"""
i2c_sensor.py — Generic I2C sensor helper for Raspberry Pi
Wraps smbus2 for raw register reads/writes and provides a base class
to subclass for specific sensor ICs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import smbus2  # type: ignore[import-not-found]
    _SMBUS2_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - platform dependent import
    smbus2 = None  # type: ignore[assignment]
    _SMBUS2_IMPORT_ERROR = exc

log = logging.getLogger(__name__)


def _ensure_smbus2_available() -> None:
    if smbus2 is None:
        raise RuntimeError(
            "smbus2 is unavailable on this platform/interpreter. "
            "Run this project on Raspberry Pi Linux with smbus2 installed."
        ) from _SMBUS2_IMPORT_ERROR


def ensure_i2c_arm_baudrate(target_hz: int = 400_000) -> bool:
    """
    Ensure Raspberry Pi I2C controller baudrate is configured in boot config.

    Returns True only when config.txt was updated. A reboot is required for
    changes to take effect.
    """
    if os.name != "posix":
        return False

    config_candidates = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt"))
    config_path = next((p for p in config_candidates if p.exists()), None)
    if config_path is None:
        log.warning("I2C config file not found in /boot; cannot enforce baudrate.")
        return False

    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("Unable to read %s: %s", config_path, exc)
        return False

    updated = False
    found_i2c_dtparam = False
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("dtparam="):
            new_lines.append(line)
            continue

        if "i2c_arm" not in stripped:
            new_lines.append(line)
            continue

        found_i2c_dtparam = True
        if "i2c_arm_baudrate=" in stripped:
            new_line = re.sub(r"i2c_arm_baudrate=\d+", f"i2c_arm_baudrate={target_hz}", line)
            updated = updated or (new_line != line)
            new_lines.append(new_line)
            continue

        if "i2c_arm=on" in stripped:
            new_line = f"{line},i2c_arm_baudrate={target_hz}"
            updated = True
            new_lines.append(new_line)
            continue

        new_lines.append(line)

    if not found_i2c_dtparam:
        new_lines.append(f"dtparam=i2c_arm=on,i2c_arm_baudrate={target_hz}")
        updated = True

    if not updated:
        return False

    try:
        config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except PermissionError:
        log.warning(
            "Permission denied writing %s. Run this program with sudo to set I2C to %d Hz.",
            config_path,
            target_hz,
        )
        return False
    except OSError as exc:
        log.warning("Unable to update %s: %s", config_path, exc)
        return False

    log.info("Updated %s with i2c_arm_baudrate=%d", config_path, target_hz)
    return True


# ── Bus utilities ─────────────────────────────────────────────────────────────

def scan_i2c_bus(bus: int = 1) -> list[int]:
    """
    Scan an I2C bus and return a list of responding device addresses (7-bit).
    Mirrors the behaviour of `i2cdetect -y <bus>`.
    """
    if smbus2 is None:
        log.warning("scan_i2c_bus skipped: smbus2 unavailable in this environment.")
        return []

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


# Sense HAT onboard IC addresses — never probed as external sensors
_SENSE_HAT_ADDRESSES: frozenset[int] = frozenset({0x1C, 0x46, 0x5C, 0x5F, 0x6A})


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
    _smbus:  Optional[Any] = field(default=None, init=False, repr=False)

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def open(self) -> None:
        """Open the underlying SMBus handle. Called automatically on first use."""
        if self._smbus is None:
            _ensure_smbus2_available()
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
    MAX_RANGE_MM = 1300

    # ST status values with gravity=Warning in the VL53L4CD table.
    _WARNING_STATUSES: frozenset[int] = frozenset({1, 2, 6})

    def __init__(self, bus: int = 1, address: int = 0x70):
        super().__init__(bus=bus, address=address, name="VL53L4CD")
        self._detected_time_budget_ms: Optional[int] = None

    def _get_dynamic_timeout_s(self, fallback_s: float = 0.5) -> float:
        """Return timeout based on detected time budget plus 20 ms."""
        if self._detected_time_budget_ms is None:
            return fallback_s
        return max((self._detected_time_budget_ms + 20) / 1000.0, 0.02)

    def _ensure_detected_time_budget_ms(self) -> Optional[int]:
        """Populate the cached time budget from device config when needed."""
        if self._detected_time_budget_ms is None:
            self.read_config()
        return self._detected_time_budget_ms

    def _resolve_ranging_buffer(self, buf: bytes) -> bytes | bytearray | None:
        """Convert a raw 15-byte reply into a terminal ranging payload."""
        range_status = buf[2]
        distance = (buf[0] << 8) | buf[1]

        if distance == 0xFFFF:
            return None

        if range_status == 3:
            data = bytearray(buf)
            data[0] = 0x00
            data[1] = 0x00
            return data

        if range_status == 4:
            data = bytearray(buf)
            data[0] = (self.MAX_RANGE_MM >> 8) & 0xFF
            data[1] = self.MAX_RANGE_MM & 0xFF
            return data

        if range_status == 0 or range_status in self._WARNING_STATUSES:
            return buf

        return None

    def _build_timeout_ranging_buffer(self, last_buf: Optional[bytes]) -> bytearray:
        """Build the fallback payload used after polling timeout."""
        data = bytearray(last_buf) if last_buf is not None else bytearray(15)
        data[0] = (self.MAX_RANGE_MM >> 8) & 0xFF
        data[1] = self.MAX_RANGE_MM & 0xFF
        data[2] = 13
        return data

    def _parse_ranging_buffer(self, data: bytes | bytearray) -> dict[str, int]:
        """Parse a terminal ranging payload into the public result shape."""
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

    def _poll_ranging_result(
        self,
        initial_wait_s: float,
        poll_interval_s: float,
        timeout_s: float,
    ) -> dict[str, int]:
        """Poll the response buffer until a terminal ranging state is reached."""
        if initial_wait_s > 0:
            time.sleep(initial_wait_s)

        deadline = time.monotonic() + timeout_s
        last_buf = None
        while time.monotonic() < deadline:
            buf = self.read(length=15)
            last_buf = buf
            data = self._resolve_ranging_buffer(buf)
            if data is not None:
                return self._parse_ranging_buffer(data)
            time.sleep(poll_interval_s)

        return self._parse_ranging_buffer(self._build_timeout_ranging_buffer(last_buf))

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
        self._detected_time_budget_ms = data[1]
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
        timeout_s: Optional[float] = None,
    ) -> dict:
        """
        Trigger one range in mm and poll until one of these terminal states:
        - status 0 (valid) or warning statuses 1/2/6 -> return measured distance
        - status 3 (below detection threshold) -> return 0 mm
        - status 4 (phase out of valid limit) -> return MAX_RANGE_MM
        - timeout or persistent invalid statuses -> return MAX_RANGE_MM with status 13

        Any other error status keeps polling until timeout.

        Always returns a parsed result payload.
        """
        self.trigger_ranging(self.UNIT_MM)
        resolved_timeout_s = timeout_s if timeout_s is not None else self._get_dynamic_timeout_s()
        return self._poll_ranging_result(initial_wait_s, poll_interval_s, resolved_timeout_s)

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


def read_ranging_result_all(
    sensors: list[VL53L4CD],
    poll_interval_s: float = 0.005,
) -> list[dict[str, int]]:
    """
    Trigger one ranging cycle on all VL53L4CD sensors and poll all results.

    The helper still guarantees at least the devices' remaining dynamic timeout
    margin after that initial wait, avoiding false timeouts near the end of a
    measurement cycle.
    """
    if poll_interval_s <= 0:
        raise ValueError(f"poll_interval_s must be > 0, got {poll_interval_s}")
    if not sensors:
        return []

    longest_time_budget_s = 0.0
    longest_remaining_timeout_s = 0.0
    for sensor in sensors:
        time_budget_ms = sensor._ensure_detected_time_budget_ms()
        if time_budget_ms is not None:
            longest_time_budget_s = max(longest_time_budget_s, time_budget_ms / 1000.0)
            longest_remaining_timeout_s = max(
                longest_remaining_timeout_s,
                max(sensor._get_dynamic_timeout_s() - (time_budget_ms / 1000.0), 0.0),
            )
        else:
            longest_remaining_timeout_s = max(
                longest_remaining_timeout_s,
                sensor._get_dynamic_timeout_s(),
            )

    for sensor in sensors:
        sensor.trigger_ranging(sensor.UNIT_MM)

    initial_wait_s = longest_time_budget_s
    if initial_wait_s > 0:
        time.sleep(initial_wait_s)

    poll_timeout_s = max(longest_remaining_timeout_s, poll_interval_s)
    deadline = time.monotonic() + poll_timeout_s
    results: list[Optional[dict[str, int]]] = [None] * len(sensors)
    last_bufs: list[Optional[bytes]] = [None] * len(sensors)
    pending = set(range(len(sensors)))

    while pending and time.monotonic() < deadline:
        resolved_any = False
        for index in tuple(pending):
            buf = sensors[index].read(length=15)
            last_bufs[index] = buf
            data = sensors[index]._resolve_ranging_buffer(buf)
            if data is None:
                continue
            results[index] = sensors[index]._parse_ranging_buffer(data)
            pending.remove(index)
            resolved_any = True

        if pending and not resolved_any:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break
            time.sleep(min(poll_interval_s, remaining_s))

    for index in pending:
        results[index] = sensors[index]._parse_ranging_buffer(
            sensors[index]._build_timeout_ranging_buffer(last_bufs[index])
        )

    return [result for result in results if result is not None]


# ── Multi-sensor discovery ────────────────────────────────────────────────────

def discover_vl53l4cd_sensors(bus: int = 1) -> list[VL53L4CD]:
    """
    Scan I2C bus and return a VL53L4CD instance for every responding address
    that replies to the Read Config command (0x04) with a self-consistent
    13-byte payload (i2c_address byte matches queried address, firmware_rev
    is not 0xFF).

    Sense HAT onboard addresses are skipped automatically.
    """
    sensors: list[VL53L4CD] = []
    for addr in scan_i2c_bus(bus):
        if addr in _SENSE_HAT_ADDRESSES:
            continue
        sensor = VL53L4CD(bus=bus, address=addr)
        try:
            cfg = sensor.read_config()
            if cfg["i2c_address"] == addr and cfg["firmware_rev"] != 0xFF:
                log.info(
                    "Discovered VL53L4CD @ %s (fw_rev=%d tb=%dms inter=%dms)",
                    hex(addr), cfg["firmware_rev"],
                    cfg["time_budget_ms"], cfg["inter_measurement_ms"],
                )
                sensors.append(sensor)
            else:
                sensor.close()
        except OSError:
            sensor.close()
    return sensors
