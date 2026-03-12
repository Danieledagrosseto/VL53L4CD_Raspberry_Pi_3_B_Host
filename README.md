# I2C Sensors Host Firmware — Raspberry Pi 3 B+

Python host firmware that reads onboard Sense HAT sensors and an external VL53L4CD time-of-flight distance sensor over I2C.

## Hardware

| Component | I2C Address | Description |
|---|---|---|
| Raspberry Pi 3 Model B+ | — | Host SBC (I2C bus 1) |
| Sense HAT — HTS221 | 0x5F | Temperature & humidity |
| Sense HAT — LPS25H | 0x5C | Barometric pressure & temperature |
| Sense HAT — LSM9DS1 | 0x1C / 0x6A | 9-DoF IMU (magnetometer, accel, gyro) |
| VL53L4CD breakout ×N | 0x70 default, configurable | Time-of-flight ranging (up to ~1300 mm), any number of units |

## Project Structure

```
main.py                     # Main entry point — sensor loop and Sense HAT LED status
i2c_sensor.py               # Generic I2C base class, ADS1115 ADC helper, VL53L4CD driver,
                            #   and discover_vl53l4cd_sensors() auto-discovery helper
I2C_COMMANDS_VL53L4CD.md    # I2C command reference for the VL53L4CD slave firmware
requirements.txt            # Python dependencies
```

## Setup

### 1. Enable I2C on the Raspberry Pi

```bash
sudo raspi-config
# Interface Options → I2C → Enable
```

### 2. Install system packages

```bash
sudo apt update
sudo apt install sense-hat python3-smbus i2c-tools
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Verify I2C devices

```bash
i2cdetect -y 1
```

Expected output should show addresses for the Sense HAT sensors and the VL53L4CD at `0x70`.

## Running

```bash
python3 main.py
```

The program will:
1. Scan I2C bus 1 and log all discovered devices.
2. Auto-discover every VL53L4CD sensor present on the bus (any number, any address).
3. Initialise the Sense HAT and log the configuration of each discovered VL53L4CD.
4. Enter a polling loop (every 2 seconds) that logs temperature, humidity, pressure, orientation, and a distance reading from **each** VL53L4CD.
5. Update the bottom-right LED on the Sense HAT matrix as a status indicator (green / amber / red based on temperature).
6. Shut down cleanly on `SIGINT` (Ctrl+C) or `SIGTERM`.

## Sense HAT LED Status

| Colour | Condition |
|---|---|
| Green | Temperature ≤ 35 °C |
| Amber | 35 °C < Temperature ≤ 50 °C |
| Red | Temperature > 50 °C |

## Sensor Data Fields

### Sense HAT

| Field | Unit | Source IC |
|---|---|---|
| `temperature_hts221_c` | °C | HTS221 |
| `humidity_pct` | % RH | HTS221 |
| `temperature_lps25h_c` | °C | LPS25H |
| `pressure_mbar` | mbar | LPS25H |
| `pitch_deg`, `roll_deg`, `yaw_deg` | degrees | LSM9DS1 |
| `accel_x/y/z_g` | g | LSM9DS1 |
| `gyro_x/y/z_rads` | rad/s | LSM9DS1 |
| `mag_x/y/z_ut` | µT | LSM9DS1 |

### VL53L4CD

| Field | Unit |
|---|---|
| `distance_mm` | mm |
| `range_status` | ST status code |
| `sigma_mm` | mm |
| `signal_kcps` | kcps |
| `ambient_kcps` | kcps |

## Multiple VL53L4CD Sensors

All VL53L4CD sensors are auto-discovered at startup via `discover_vl53l4cd_sensors()` in `i2c_sensor.py`. The function:

1. Scans the I2C bus for all responding addresses.
2. Skips known Sense HAT addresses (`0x1C`, `0x46`, `0x5C`, `0x5F`, `0x6A`).
3. Sends a Read Config command (`0x04`) to each remaining address.
4. Registers the address as a VL53L4CD if the 13-byte response is self-consistent (returned `i2c_address` byte matches, `firmware_rev` ≠ `0xFF`).

No code changes are needed when adding or removing sensors — just wire them up and assign each a unique I2C address. See the [I2C_COMMANDS_VL53L4CD.md](I2C_COMMANDS_VL53L4CD.md#change-i2c-address-sequence) address-change sequence.

## Extending — Adding Other Sensors

To add a different I2C sensor:

- **Subclass `I2CSensor`** in `i2c_sensor.py` (see `ADS1115` or `VL53L4CD` as examples).
- **Use `I2CSensor` directly** with the raw `read_register` / `write_register` helpers.

Then instantiate the sensor in `setup_external_sensors()` in `main.py` and add a read call inside the main loop.

## VL53L4CD Command Protocol

The VL53L4CD breakout runs a custom I2C slave firmware. Full command reference is in [I2C_COMMANDS_VL53L4CD.md](I2C_COMMANDS_VL53L4CD.md).

### Quick reference

| Command | Code | Purpose |
|---|---|---|
| Ranging | `0x00` | Trigger a single distance measurement |
| Set timing | `0x01` | Set time budget and inter-measurement period |
| Offset calibration | `0x02` | Run and save offset calibration |
| XTalk calibration | `0x03` | Run and save crosstalk calibration |
| Read config | `0x04` | Read current EEPROM configuration (13 bytes) |
| Restore defaults | `0x05` | Reset EEPROM params to factory defaults |
| Set thresholds | `0x07` | Update sigma and signal thresholds |
| Restart | `0x08` | Reload EEPROM values and re-apply settings |

Default slave address is `0x70`. The address can be changed (range `0x08`–`0x7F`) and is persisted in EEPROM. Assign each sensor a unique address and the host firmware will discover all of them automatically at startup.

## Dependencies

| Package | Purpose |
|---|---|
| `sense-hat` | Sense HAT sensor and LED matrix access |
| `smbus2` | I2C bus communication |
| `RPi.GPIO` | GPIO pin access |
| `gpiozero` | High-level GPIO interface |

## License

MIT
