"""
I2C Sensors Host Firmware - Main Entry Point
Raspberry Pi 3 Model B+ with Sense HAT

Sense HAT onboard sensors (all on I2C bus 1):
  HTS221  @ 0x5F  - Temperature & Humidity
  LPS25H  @ 0x5C  - Pressure & Temperature
  LSM9DS1 @ 0x1C  - Magnetometer
  LSM9DS1 @ 0x6A  - Accelerometer & Gyroscope

Run with: python3 main.py [--start-mode joystick|immediate]
"""

import time
import logging
import signal
import threading
import argparse
import os
from typing import Optional, Any

try:
    from sense_hat import SenseHat
except ImportError:
    SenseHat = Any
from i2c_sensor import (
    I2CSensor,
    VL53L4CD,
    scan_i2c_bus,
    discover_vl53l4cd_sensors,
    ensure_i2c_arm_baudrate,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 0.1   # How often to read sensors (seconds)
I2C_BUS = 1               # Raspberry Pi hardware I2C bus
I2C_SPEED_HZ = 400_000
DEFAULT_START_MODE = os.getenv("START_MODE", "joystick").strip().lower()
VALID_START_MODES = {"joystick", "immediate"}
IDLE_STATUS_LOG_SEC = 5.0

# Sense HAT LED colours (R, G, B)
COLOUR_OK    = (0, 64, 0)
COLOUR_WARN  = (64, 32, 0)
COLOUR_ERROR = (64, 0, 0)
COLOUR_OFF   = (0, 0, 0)

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True

def _handle_signal(signum, frame):
    global _running
    log.info("Shutdown signal received — stopping.")
    _running = False

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Sense HAT helpers ─────────────────────────────────────────────────────────

def read_sense_hat(sense: SenseHat) -> dict:
    """Read all built-in Sense HAT sensors and return a flat dict."""
    orientation = sense.get_orientation_degrees()
    acceleration = sense.get_accelerometer_raw()
    gyroscope    = sense.get_gyroscope_raw()
    compass      = sense.get_compass_raw()

    return {
        # LSM9DS1 — orientation
        "pitch_deg":  round(orientation["pitch"], 2),
        "roll_deg":   round(orientation["roll"],  2),
        "yaw_deg":    round(orientation["yaw"],   2),
        # LSM9DS1 — accelerometer (g)
        "accel_x_g":  round(acceleration["x"], 4),
        "accel_y_g":  round(acceleration["y"], 4),
        "accel_z_g":  round(acceleration["z"], 4),
        # LSM9DS1 — gyroscope (rad/s)
        "gyro_x_rads": round(gyroscope["x"], 4),
        "gyro_y_rads": round(gyroscope["y"], 4),
        "gyro_z_rads": round(gyroscope["z"], 4),
        # LSM9DS1 — compass (µT)
        "mag_x_ut":   round(compass["x"], 2),
        "mag_y_ut":   round(compass["y"], 2),
        "mag_z_ut":   round(compass["z"], 2),
    }


def update_led_status(sense: SenseHat, data: dict):
    """Show runtime heartbeat on the Sense HAT LED matrix corner pixel."""
    # Light a single corner pixel as a heartbeat indicator
    try:
        sense.set_pixel(7, 7, COLOUR_OK)
    except OSError:
        pass


def _startup_blink_worker(sense: SenseHat, stop_event: threading.Event, interval_s: float = 0.25):
    """Blink the LED matrix red while initialisation is in progress."""
    show_red = True
    while not stop_event.is_set():
        try:
            sense.clear(COLOUR_ERROR if show_red else COLOUR_OFF)
        except OSError as exc:
            log.error("Startup LED update failed: %s", exc)
            break
        show_red = not show_red
        stop_event.wait(interval_s)


def wait_for_joystick_press(sense: SenseHat) -> bool:
    """Block until joystick press is detected or shutdown is requested."""
    log.info("Waiting for joystick press to start acquisition loop...")
    while _running:
        for event in sense.stick.get_events():
            if event.action == "pressed":
                log.info("Joystick pressed (%s). Starting loop.", event.direction)
                return True
        time.sleep(0.05)
    return False


def resolve_start_mode(cli_mode: Optional[str]) -> str:
    """Resolve startup mode from CLI and environment with validation."""
    mode = (cli_mode or DEFAULT_START_MODE).strip().lower()
    if mode not in VALID_START_MODES:
        log.warning(
            "Invalid start mode '%s'. Falling back to 'joystick'. Valid modes: %s",
            mode,
            sorted(VALID_START_MODES),
        )
        return "joystick"
    return mode


# ── External I2C sensor setup ────────────────────────────────────────────────

def setup_external_sensors(bus: int) -> list[I2CSensor]:
    """
    Auto-discover all VL53L4CD sensors present on the I2C bus.
    Each responding address that passes the config handshake is registered.
    """
    sensors: list[I2CSensor] = discover_vl53l4cd_sensors(bus)
    if not sensors:
        log.warning("No VL53L4CD sensors found on bus %d", bus)
    return sensors


# ── Main loop ─────────────────────────────────────────────────────────────────

def main(start_mode: str):
    log.info("=== I2C Sensor Host Firmware starting ===")
    log.info("Startup mode: %s", start_mode)

    if ensure_i2c_arm_baudrate(I2C_SPEED_HZ):
        log.warning(
            "I2C baudrate updated to %d Hz in boot config. Reboot Raspberry Pi to apply.",
            I2C_SPEED_HZ,
        )

    sense = None
    external_sensors: list[I2CSensor] = []
    blink_stop_event: Optional[threading.Event] = None
    blink_thread: Optional[threading.Thread] = None

    try:
        # Initialise Sense HAT if present; external sensors must still run without it.
        try:
            sense = SenseHat()
            log.info("Sense HAT initialised.")

            try:
                sense.clear()
                blink_stop_event = threading.Event()
                blink_thread = threading.Thread(
                    target=_startup_blink_worker,
                    args=(sense, blink_stop_event),
                    daemon=True,
                )
                blink_thread.start()
            except OSError as exc:
                log.warning("Sense HAT LED unavailable (%s). Continuing without LED status.", exc)
        except Exception as exc:
            sense = None
            log.warning("Sense HAT unavailable (%s). Continuing without Sense HAT.", exc)

        # Scan the I2C bus and log discovered devices
        log.info("Scanning I2C bus %d ...", I2C_BUS)
        discovered = scan_i2c_bus(I2C_BUS)
        if discovered:
            log.info("Devices found: %s", [hex(a) for a in discovered])
        else:
            log.warning("No I2C devices found on bus %d", I2C_BUS)

        # Initialise any external sensors
        external_sensors = setup_external_sensors(I2C_BUS)
        if external_sensors:
            log.info("External sensors registered: %s", [s.name for s in external_sensors])
            for sensor in external_sensors:
                if isinstance(sensor, VL53L4CD):
                    cfg = sensor.read_config()
                    log.info(
                        "[VL53L4CD @ %s] tb=%dms inter=%dms fw_rev=%d",
                        hex(sensor.address),
                        cfg["time_budget_ms"],
                        cfg["inter_measurement_ms"],
                        cfg["firmware_rev"],
                    )
    except Exception as exc:
        log.error("Initialisation failed: %s", exc)
        if sense is not None:
            sense.clear(COLOUR_ERROR)
        return
    finally:
        if blink_stop_event is not None:
            blink_stop_event.set()
        if blink_thread is not None:
            blink_thread.join(timeout=1.0)

    if start_mode == "joystick":
        if sense is None:
            log.warning("Joystick mode requested, but Sense HAT is unavailable. Starting loop immediately.")
        else:
            sense.clear(COLOUR_OK)
            if not wait_for_joystick_press(sense):
                sense.clear(COLOUR_OFF)
                log.info("Startup aborted before acquisition loop.")
                return
            sense.clear(COLOUR_OFF)
    else:
        log.info("Immediate mode selected. Starting acquisition loop.")

    try:
        next_idle_log_at = time.monotonic() + IDLE_STATUS_LOG_SEC
        while _running:
            did_log_sensor_data = False

            # --- Read Sense HAT ---
            if sense is not None:
                try:
                    data = read_sense_hat(sense)
                    update_led_status(sense, data)

                    log.info(
                        "Pitch: %.1f° | Roll: %.1f° | Yaw: %.1f°",
                        data["pitch_deg"],
                        data["roll_deg"],
                        data["yaw_deg"],
                    )
                    did_log_sensor_data = True
                except OSError as exc:
                    log.error("Sense HAT read error: %s", exc)

            # --- Read external sensors ---
            for sensor in external_sensors:
                try:
                    if isinstance(sensor, VL53L4CD):
                        result = sensor.read_ranging_result()
                        log.info(
                            "[VL53L4CD @ %s] distance=%d mm status=%d sigma=%d",
                            hex(sensor.address),
                            result["distance_mm"],
                            result["range_status"],
                            result["sigma_mm_raw"],
                        )
                        did_log_sensor_data = True
                    else:
                        reading = sensor.read()
                        log.info("[%s @ %s] raw bytes: %s",
                                 sensor.name, hex(sensor.address), reading.hex())
                        did_log_sensor_data = True
                except OSError as exc:
                    log.error("[%s] I2C error: %s", sensor.name, exc)

            if not did_log_sensor_data and time.monotonic() >= next_idle_log_at:
                if sense is None and not external_sensors:
                    log.warning(
                        "Loop alive, but no active sensors: Sense HAT unavailable and no external I2C sensors discovered."
                    )
                elif sense is None:
                    log.info("Loop alive: Sense HAT unavailable, external sensor polling active.")
                elif not external_sensors:
                    log.info("Loop alive: Sense HAT active, no external sensors discovered.")
                else:
                    log.info("Loop alive: polling sensors.")
                next_idle_log_at = time.monotonic() + IDLE_STATUS_LOG_SEC

            time.sleep(POLL_INTERVAL_SEC)

    finally:
        if sense is not None:
            sense.clear()
        log.info("LED cleared. Firmware stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="I2C Sensors Host Firmware")
    parser.add_argument(
        "--start-mode",
        choices=sorted(VALID_START_MODES),
        default=None,
        help="Startup mode: joystick waits for press, immediate starts loop right away.",
    )
    args = parser.parse_args()
    main(start_mode=resolve_start_mode(args.start_mode))
