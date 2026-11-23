#pragma once

constexpr int OLED_SDA_PIN = 32;
constexpr int OLED_SCL_PIN = 33;
constexpr int OLED_WIDTH = 128;
constexpr int OLED_HEIGHT = 64;
constexpr int BUZZER_PIN = 25;
constexpr int STATUS_LED_PIN = 26;
// Grove rotary angle sensor for the effort rating. Must be an ADC1 pin (GPIO32-39):
// ADC2 pins such as GPIO13 cannot be read while Wi-Fi is on. Power the sensor from 3V3, not 5V.
constexpr int ROTARY_PIN = 34;

// SIG/NC buzzer modules are commonly active buzzers. Change these if needed.
constexpr bool BUZZER_ACTIVE_HIGH = true;
constexpr bool BUZZER_IS_PASSIVE = false;
constexpr bool LED_ACTIVE_HIGH = true;
