#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>
#include "hardware_config.h"
#include "secrets.h"

Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);
bool displayReady = false;
bool dashboardConnected = false;
bool assigned = false;
bool beeping = false;
unsigned long beepEndsAt = 0;
unsigned long lastPoll = 0;
unsigned long lastDisplay = 0;
unsigned long lastSuccessfulPoll = 0;
unsigned long lastReconnect = 0;
String deviceId;
String deviceNumber;
String deviceName;
String studentName;
String exercise;
String lastCommandId;
String acknowledgedCommandId;
String pendingCommandId;
int fullReps = 0;
int partialReps = 0;
constexpr int BUZZER_CHANNEL = 0;

void setBuzzer(bool on) {
  if (BUZZER_IS_PASSIVE) {
    ledcWriteTone(BUZZER_CHANNEL, on ? 2400 : 0);
  } else {
    digitalWrite(BUZZER_PIN, (on == BUZZER_ACTIVE_HIGH) ? HIGH : LOW);
  }
}

void updateIndicators() {
  const unsigned long now = millis();
  if (beeping && static_cast<long>(now - beepEndsAt) >= 0) {
    setBuzzer(false);
    beeping = false;
    acknowledgedCommandId = pendingCommandId;
  }
  if (WiFi.status() != WL_CONNECTED || now - lastSuccessfulPoll > 3000) {
    dashboardConnected = false;
  }
  const bool linked = dashboardConnected && assigned;
  digitalWrite(STATUS_LED_PIN, (linked == LED_ACTIVE_HIGH) ? HIGH : LOW);
}

void renderDisplay() {
  if (!displayReady) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("CAREE DEVICE");
  display.setTextSize(2);
  display.setCursor(0, 12);
  display.print(deviceNumber);
  display.setTextSize(1);
  display.setCursor(0, 33);
  display.print(WiFi.status() != WL_CONNECTED ? "Joining WiFi..." :
                dashboardConnected ? "Dashboard connected" : "Waiting for Flask...");
  display.setCursor(0, 44);
  display.print(dashboardConnected && assigned ? studentName.substring(0, 21) : "Not linked to student");
  display.setCursor(0, 55);
  if (dashboardConnected && assigned) {
    display.printf("%s R:%d P:%d", exercise == "arms" ? "ARM" : "LEG", fullReps, partialReps);
  } else if (WiFi.status() == WL_CONNECTED) {
    display.print(WiFi.localIP().toString());
  }
  display.display();
}

void pollDashboard() {
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(800);
  http.setTimeout(800);
  String endpoint = "http://" + WiFi.gatewayIP().toString() + ":5000/api/devices/heartbeat";
  StaticJsonDocument<256> request;
  request["device_id"] = deviceId;
  request["name"] = deviceName;
  if (acknowledgedCommandId.length()) request["acknowledged_command_id"] = acknowledgedCommandId;
  String payload;
  serializeJson(request, payload);
  if (!http.begin(client, endpoint)) return;
  http.addHeader("Content-Type", "application/json");
  const int status = http.POST(payload);
  if (status == 200) {
    StaticJsonDocument<2048> reply;
    const DeserializationError error = deserializeJson(reply, http.getString());
    if (!error && reply["device_id"].is<const char*>()) {
      if (!dashboardConnected) Serial.printf("Dashboard connected: %s\n", endpoint.c_str());
      dashboardConnected = true;
      lastSuccessfulPoll = millis();
      assigned = !reply["student"].isNull();
      if (assigned) {
        studentName = reply["student"]["name"].as<String>();
        exercise = reply["student"]["mode"].as<String>();
        fullReps = reply["student"]["reps"] | 0;
        partialReps = reply["student"]["partial"] | 0;
      }
      JsonObject command = reply["commands"][0];
      if (!command.isNull()) {
        String id = command["id"].as<String>();
        const char* type = command["type"] | "";
        if (id.length() && id != lastCommandId && strcmp(type, "partial_rep") == 0 && assigned) {
          lastCommandId = id;
          pendingCommandId = id;
          const int duration = constrain(command["duration_ms"] | 180, 50, 500);
          setBuzzer(true);
          beeping = true;
          beepEndsAt = millis() + duration;
          Serial.printf("Partial rep beep: %s\n", id.c_str());
        }
      }
    } else {
      dashboardConnected = false;
      Serial.println("Invalid dashboard response");
    }
  } else {
    dashboardConnected = false;
    Serial.printf("Heartbeat HTTP %d at %s\n", status, endpoint.c_str());
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(STATUS_LED_PIN, OUTPUT);
  digitalWrite(STATUS_LED_PIN, LED_ACTIVE_HIGH ? LOW : HIGH);
  pinMode(BUZZER_PIN, OUTPUT);
  if (BUZZER_IS_PASSIVE) {
    ledcSetup(BUZZER_CHANNEL, 2400, 8);
    ledcAttachPin(BUZZER_PIN, BUZZER_CHANNEL);
  }
  setBuzzer(false);

  WiFi.mode(WIFI_STA);
  deviceId = WiFi.macAddress();
  deviceId.toLowerCase();
  String compact = deviceId;
  compact.replace(":", "");
  deviceNumber = compact.substring(6);
  deviceNumber.toUpperCase();
  deviceName = "ESP32-" + deviceNumber;
  Serial.printf("CAREE device %s | MAC %s\n", deviceNumber.c_str(), deviceId.c_str());

  Wire.begin(OLED_SDA_PIN, OLED_SCL_PIN);
  Wire.setTimeOut(50);
  for (uint8_t address : {0x3C, 0x3D}) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      displayReady = display.begin(SSD1306_SWITCHCAPVCC, address, true, false);
      if (displayReady) {
        Serial.printf("SSD1306 OLED found at 0x%02X\n", address);
        break;
      }
    }
  }
  if (!displayReady) Serial.println("OLED not found; continuing without display");
  renderDisplay();
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  WiFi.begin(HOTSPOT_SSID, HOTSPOT_PASSWORD);
  Serial.println("Joining configured hotspot...");
}

void loop() {
  const unsigned long now = millis();
  updateIndicators();
  // No HTTP requests during a beep, so network timeouts cannot hold SIG active.
  if (!beeping && WiFi.status() == WL_CONNECTED && now - lastPoll >= 500) {
    lastPoll = now;
    pollDashboard();
    updateIndicators();
  }
  if (WiFi.status() != WL_CONNECTED && now - lastReconnect >= 15000) {
    lastReconnect = now;
    WiFi.reconnect();
  }
  if (now - lastDisplay >= 250) {
    lastDisplay = now;
    renderDisplay();
  }
  delay(5);
}
