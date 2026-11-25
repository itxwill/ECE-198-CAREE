# CAREE ESP32 device

PlatformIO environment: `esp32dev`, Arduino ESP32 2.0.17 (Espressif32 platform 6.10.0).

## Wiring

Use the **GPIO labels printed on your board**, not the physical header position.
USB-C describes the connector; it does not change the selected GPIO numbers.

| Peripheral connection | ESP32 GPIO / connection |
| --- | --- |
| OLED SDA | GPIO32 (D32) |
| OLED SCL | GPIO33 (D33) |
| OLED VCC | 3V3 for a 3.3 V-compatible module |
| OLED GND | GND |
| Buzzer SIG | GPIO25 (D25) |
| Buzzer NC | Leave disconnected |
| Buzzer GND | GND |
| Buzzer VCC | Supply specified by the buzzer module; use a module with a 3.3 V-compatible SIG input |
| External LED anode (+) | GPIO26 (D26), through a 330-ohm series resistor |
| External LED cathode (-) | GND |

GPIO34/35 are input-only and cannot drive these peripherals. I2C is explicitly routed to GPIO32/33 by the firmware.
All modules need a common ground. Do not connect a buzzer's power input to a GPIO.
If a module requires 5 V, its signal interface must still be safe for the ESP32's 3.3 V GPIO.

The default display is an SSD1306 128x64 I2C OLED. Firmware tries addresses 0x3C and 0x3D; it still runs if no OLED is detected. An SH1106 display requires a different driver.

`include/hardware_config.h` contains pins, display size and polarity settings. The SIG/NC buzzer defaults to **active-high active-buzzer mode**. Set `BUZZER_ACTIVE_HIGH` false for an active-low module; set `BUZZER_IS_PASSIVE` true for a passive piezo module needing a 2.4 kHz tone. GPIO15 is deliberately unused because it is a boot-strapping pin.

## Build and upload

Double-click `../start-caree.bat` to start Flask and the serial monitor without uploading.
To specify the USB port, run `..\start-caree.bat COM13` from this project folder.
To upload first, use `..\start-caree.bat upload COM13` (or `--upload`). Without a COM port, PlatformIO selects one automatically.
Flask runs in its own window; stop it with Ctrl+C before launching another copy.
After a successful upload, the same window automatically opens the serial monitor at 115200 baud while Flask continues running. An explicit COM port is used for both upload and monitoring. Close the monitor with Ctrl+C before another upload; the serial port cannot be used by both at once. Failed uploads remain visible and do not start monitoring. `..\start-caree.bat --check`
checks the launcher dependencies without starting Flask or flashing a board.

The supplied hotspot credentials are in ignored `include/secrets.h`. For a fresh checkout, copy `include/secrets.example.h` to `include/secrets.h` and set the credentials.

```powershell
pio run
pio device list
pio run --target upload --upload-port COMx
pio device monitor --port COMx --baud 115200
```

Use the ESP32's USB serial port, not a Bluetooth COM port. Close serial monitors before uploading. If the uploader waits at Connecting, hold BOOT until writing starts.

## Runtime

1. Turn on the laptop's hotspot and restart the updated Flask app.
2. The box joins the hotspot in `secrets.h` (2.4 GHz only; on iPhone turn on **Maximize Compatibility**), then finds the laptop by itself: it tries the last laptop it found, an optional `CAREE_SERVER_IP`, the hotspot gateway, then every address on the hotspot. The OLED shows `Finding laptop...` and the address being tried. Once found it sends `http://<laptop>:8080/api/devices/heartbeat` every 500 ms, and searches again after ~3 s without replies. Flask serves the box API on port 8080 as well as 5000.
3. The OLED shows a six-hex-digit device label, e.g. `A1B2C3`. Select the matching `ESP32-A1B2C3` in the student's Device dropdown. The full Wi-Fi MAC is the backend device ID.
4. GPIO26's LED lights only while Flask is responding and the device is assigned to a logged student. Selecting **No device**, removing that student, or losing the Flask connection turns it off. The OLED shows the assigned student's name, selected exercise and counts.
5. Link the student's visible camera detection. A **new automatically detected partial rep** in the selected exercise queues a 180 ms beep. Editing counts manually does not beep. Full reps do not beep.

Notifications expire after five seconds. Commands are acknowledged after the pulse completes, and repeated delivery of the same command does not replay it during the same device boot. Unassigning/removing a student clears their pending notifications. No beeps are stored across Flask restarts.

## Phone join

1. Install the laptop dependencies from the repository root with `python -m pip install -r requirements.txt`. If the local OpenVINO pose model is absent, the phone worker downloads `yolo26n-pose.pt` on its first frame. Send a frame once with internet access before running an offline class.
2. Turn on the laptop hotspot, power on each box and connect it and each phone to the hotspot, and start Flask with `start-caree.bat` or `python web/webapp.py`. Open the instructor dashboard at `http://localhost:5000`.
3. Under **Join on your phone**, find the four-digit game code and open the hotspot's **https** URL on each phone. Allow port 8443 on the laptop's hotspot firewall if needed. The laptop generates its own certificate, so tap through the browser's “not private” warning for that address once.
4. Enter the game code, your name and the six-character code on your box's OLED. You can leave the box field blank to exercise without one.
5. Prop the phone sideways so the front camera sees you from the side. Select **Arms** or **Legs**, tap **Start camera** and allow camera access. Counts go directly to your student record and paired box. Keep the exercise page visible while counting.

For pin references, see [Espressif's I2C documentation](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/i2c.html).
