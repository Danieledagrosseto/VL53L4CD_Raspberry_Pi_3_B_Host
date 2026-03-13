"""
Quick setup utility for VL53L4CD breakout firmware.

Menu:
1) Change I2C address
2) Change time budget (intermeasurement is always forced to 0)
3) Change offset (runs offset calibration)
4) Read stored configuration
"""

from __future__ import annotations

import time

try:
    import smbus2  # type: ignore[import-not-found]
    _SMBUS2_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - platform dependent import
    smbus2 = None  # type: ignore[assignment]
    _SMBUS2_IMPORT_ERROR = exc

from i2c_sensor import VL53L4CD, discover_vl53l4cd_sensors, scan_i2c_bus


MIN_I2C_ADDR = 0x08
MAX_I2C_ADDR = 0x7F
MIN_TIME_BUDGET_MS = 10
MAX_TIME_BUDGET_MS = 200
MIN_OFFSET_TARGET_MM = 10
MAX_OFFSET_TARGET_MM = 1000
MIN_OFFSET_SAMPLES = 5
MAX_OFFSET_SAMPLES = 255


def _parse_int(value: str) -> int:
    """
    Parse decimal or hex (0x..) integer input.
    """
    return int(value.strip(), 0)


def _prompt_int(prompt: str, min_value: int, max_value: int) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            value = _parse_int(raw)
        except ValueError:
            print("Invalid number. Use decimal (e.g. 42) or hex (e.g. 0x2A).")
            continue

        if value < min_value or value > max_value:
            print(f"Value out of range [{min_value}..{max_value}]")
            continue
        return value


def _prompt_int_with_default(prompt: str, default: int, min_value: int, max_value: int) -> int:
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default

        try:
            value = _parse_int(raw)
        except ValueError:
            print("Invalid number. Use decimal (e.g. 42) or hex (e.g. 0x2A).")
            continue

        if value < min_value or value > max_value:
            print(f"Value out of range [{min_value}..{max_value}]")
            continue
        return value


def _change_address(bus_num: int) -> None:
    current_addr = _prompt_int(
        f"Current address [{hex(MIN_I2C_ADDR)}..{hex(MAX_I2C_ADDR)}]: ",
        MIN_I2C_ADDR,
        MAX_I2C_ADDR,
    )
    new_addr = _prompt_int(
        f"New address [{hex(MIN_I2C_ADDR)}..{hex(MAX_I2C_ADDR)}]: ",
        MIN_I2C_ADDR,
        MAX_I2C_ADDR,
    )

    if current_addr == new_addr:
        print("Current and new address are the same. Nothing to do.")
        return

    sequence = [0xA0, 0xAA, 0xA5, new_addr]

    if smbus2 is None:
        print(
            "smbus2 is unavailable in this environment. "
            "Run this tool on Raspberry Pi Linux to change address."
        )
        if _SMBUS2_IMPORT_ERROR is not None:
            print(f"Import detail: {_SMBUS2_IMPORT_ERROR}")
        return

    try:
        with smbus2.SMBus(bus_num) as bus:
            for byte_value in sequence:
                bus.write_i2c_block_data(current_addr, 0x00, [byte_value])
                time.sleep(0.02)
    except OSError as exc:
        print(f"Address change failed at {hex(current_addr)}: {exc}")
        return

    print(f"Address change sequence sent. New address should be {hex(new_addr)}")

    # Verify by reading config from the new address.
    sensor = VL53L4CD(bus=bus_num, address=new_addr)
    try:
        cfg = sensor.read_config()
        print(
            "Verified: "
            f"addr={hex(cfg['i2c_address'])} "
            f"tb={cfg['time_budget_ms']}ms "
            f"inter={cfg['inter_measurement_ms']}ms"
        )
    except OSError as exc:
        print(f"Could not verify at {hex(new_addr)}: {exc}")


def _change_time_budget(bus_num: int) -> None:
    addr = _prompt_int(
        f"Sensor address [{hex(MIN_I2C_ADDR)}..{hex(MAX_I2C_ADDR)}]: ",
        MIN_I2C_ADDR,
        MAX_I2C_ADDR,
    )
    time_budget_ms = _prompt_int(
        f"Time budget ms [{MIN_TIME_BUDGET_MS}..{MAX_TIME_BUDGET_MS}]: ",
        MIN_TIME_BUDGET_MS,
        MAX_TIME_BUDGET_MS,
    )

    sensor = VL53L4CD(bus=bus_num, address=addr)
    try:
        # Requirement: intermeasurement is always 0.
        sensor.set_timing(time_budget_ms=time_budget_ms, inter_measurement_ms=0)
        time.sleep(0.05)
        cfg = sensor.read_config()
    except OSError as exc:
        print(f"Timing update failed at {hex(addr)}: {exc}")
        return

    print(
        "Timing updated: "
        f"addr={hex(cfg['i2c_address'])} "
        f"tb={cfg['time_budget_ms']}ms "
        f"inter={cfg['inter_measurement_ms']}ms"
    )


def _change_offset(bus_num: int) -> None:
    addr = _prompt_int(
        f"Sensor address [{hex(MIN_I2C_ADDR)}..{hex(MAX_I2C_ADDR)}]: ",
        MIN_I2C_ADDR,
        MAX_I2C_ADDR,
    )
    target_mm = _prompt_int(
        f"Calibration target distance mm [{MIN_OFFSET_TARGET_MM}..{MAX_OFFSET_TARGET_MM}]: ",
        MIN_OFFSET_TARGET_MM,
        MAX_OFFSET_TARGET_MM,
    )
    samples = _prompt_int(
        f"Number of samples [{MIN_OFFSET_SAMPLES}..{MAX_OFFSET_SAMPLES}]: ",
        MIN_OFFSET_SAMPLES,
        MAX_OFFSET_SAMPLES,
    )

    sensor = VL53L4CD(bus=bus_num, address=addr)
    payload = [(target_mm >> 8) & 0xFF, target_mm & 0xFF, (samples >> 8) & 0xFF, samples & 0xFF]

    try:
        sensor.write_block(sensor.CMD_OFFSET_CAL, payload)
        print("Offset calibration started. Keep target fixed while calibration runs...")
        time.sleep(2.0)
        cfg = sensor.read_config()
    except OSError as exc:
        print(f"Offset calibration failed at {hex(addr)}: {exc}")
        return

    print(
        "Offset calibration completed: "
        f"addr={hex(cfg['i2c_address'])} "
        f"offset={cfg['offset_mm']}mm"
    )


def _read_config(bus_num: int) -> None:
    addr = _prompt_int(
        f"Sensor address [{hex(MIN_I2C_ADDR)}..{hex(MAX_I2C_ADDR)}]: ",
        MIN_I2C_ADDR,
        MAX_I2C_ADDR,
    )
    sensor = VL53L4CD(bus=bus_num, address=addr)
    try:
        cfg = sensor.read_config()
    except OSError as exc:
        print(f"Read config failed at {hex(addr)}: {exc}")
        return

    print(f"  i2c_address      : {hex(cfg['i2c_address'])}")
    print(f"  time_budget_ms   : {cfg['time_budget_ms']} ms")
    print(f"  inter_measure_ms : {cfg['inter_measurement_ms']} ms")
    print(f"  offset_mm        : {cfg['offset_mm']} mm")
    print(f"  xtalk_kcps       : {cfg['xtalk_kcps']}")
    print(f"  sigma_threshold  : {cfg['sigma_threshold_mm']} mm")
    print(f"  signal_threshold : {cfg['signal_threshold_kcps']} kcps")
    print(f"  firmware_rev     : {cfg['firmware_rev']}")


def _show_menu() -> None:
    print("\nVL53L4CD Fast Setup")
    print("1) Change address")
    print("2) Change time_budget (intermeasurement always 0)")
    print("3) Change offset")
    print("4) Read stored configuration")
    print("0) Exit")


def _list_devices(bus_num: int) -> None:
    """Scan the bus and print every discovered VL53L4CD with its address."""
    print(f"Scanning I2C bus {bus_num} for VL53L4CD sensors...")
    sensors = discover_vl53l4cd_sensors(bus_num)
    if not sensors:
        # Fall back to raw bus scan so any address at least shows up.
        raw = scan_i2c_bus(bus_num)
        if raw:
            print(f"  No VL53L4CD confirmed, but raw I2C addresses found: {[hex(a) for a in raw]}")
        else:
            print("  No I2C devices found.")
        return
    for s in sensors:
        try:
            cfg = s.read_config()
            print(
                f"  {hex(s.address)}  fw_rev={cfg['firmware_rev']}  "
                f"tb={cfg['time_budget_ms']}ms  "
                f"inter={cfg['inter_measurement_ms']}ms  "
                f"offset={cfg['offset_mm']}mm"
            )
        except OSError as exc:
            print(f"  {hex(s.address)}  (read error: {exc})")


def main() -> None:
    print("VL53L4CD fast setup utility")
    bus_num = _prompt_int_with_default("I2C bus number [0..10] (default 1): ", 1, 0, 10)
    _list_devices(bus_num)

    while True:
        _show_menu()
        choice = input("Select option: ").strip()

        if choice == "1":
            _change_address(bus_num)
        elif choice == "2":
            _change_time_budget(bus_num)
        elif choice == "3":
            _change_offset(bus_num)
        elif choice == "4":
            _read_config(bus_num)
        elif choice == "0":
            print("Exiting.")
            return
        else:
            print("Invalid option.")


if __name__ == "__main__":
    main()
