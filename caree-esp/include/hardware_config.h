#pragma once

constexpr int OLED_SDA_PIN = 32;
constexpr int OLED_SCL_PIN = 33;
constexpr int OLED_WIDTH = 128;
constexpr int OLED_HEIGHT = 64;
constexpr int BUZZER_PIN = 25;
constexpr int STATUS_LED_PIN = 26;

// SIG/NC buzzer modules are commonly active buzzers. Change these if needed.
constexpr bool BUZZER_ACTIVE_HIGH = true;
constexpr bool BUZZER_IS_PASSIVE = false;
constexpr bool LED_ACTIVE_HIGH = true;
