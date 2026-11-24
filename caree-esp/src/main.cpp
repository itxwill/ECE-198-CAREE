// This exercise box polls the laptop for its assigned student's rep counts.
// Phone frames go to /api/phone/frame, where PhoneService uses Person and
// RepCounter to count movements. Roster.credit saves the new counts.
// Heartbeat replies carry those totals, partial-rep beeps and effort prompts.
// The box displays the totals and sends dial answers back on its next polls.
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>
#include "hardware_config.h"
#include "secrets.h"

// Optional fixed laptop address in secrets.h, e.g. #define CAREE_SERVER_IP "172.20.10.6".
// Leave it out and the box finds the laptop by itself.
#ifndef CAREE_SERVER_IP
#define CAREE_SERVER_IP ""
#endif

// Flask also serves the box API on 8080 (allowed by the laptop firewall on private hotspots).
constexpr uint16_t SERVER_PORT = 8080;
constexpr uint16_t POLL_INTERVAL_MS = 500;
constexpr uint16_t SCAN_TIMEOUT_MS = 250;      // Per address while searching for the laptop.
constexpr uint32_t SCAN_PAUSE_MS = 2000;       // Pause after a full pass found nothing.
constexpr int LOST_AFTER_FAILED_POLLS = 6;     // ~3 s without replies -> search again.
constexpr uint32_t MAX_SCAN_HOSTS = 1024;

Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);
bool displayReady = false;
bool dashboardConnected = false;
bool assigned = false;
bool beeping = false;
bool serverKnown = false;
unsigned long beepEndsAt = 0;
unsigned long lastPoll = 0;
unsigned long lastDisplay = 0;
unsigned long lastSuccessfulPoll = 0;
unsigned long lastReconnect = 0;
unsigned long scanPausedUntil = 0;
int failedPolls = 0;
uint32_t scanStep = 0;
IPAddress serverIp;
IPAddress lastServer;
IPAddress scanning;
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

// Effort rating: after a set the laptop sends "effort_prompt"; the participant turns the
// knob to 0-10 and holds it still for EFFORT_HOLD_MS to save.
constexpr unsigned long EFFORT_HOLD_MS = 3000;
constexpr unsigned long EFFORT_GIVE_UP_MS = 180000;  // Matches the server's prompt expiry.
bool askingEffort = false;
bool effortMoved = false;
int effortValue = 0;
float knobSmoothed = -1;
unsigned long effortAskedAt = 0;
unsigned long effortChangedAt = 0;
unsigned long effortSavedShownUntil = 0;
String effortCommandId;    // Prompt being answered on screen.
String effortAnswerId;     // Saved answer waiting to reach the laptop.
int effortAnswerValue = 0;

// Convert an IPv4 address to an integer for subnet calculations.
uint32_t toNumber(const IPAddress& ip) {
  return (uint32_t(ip[0]) << 24) | (uint32_t(ip[1]) << 16) | (uint32_t(ip[2]) << 8) | uint32_t(ip[3]);
}

// Turn a numeric scan candidate back into an address for the heartbeat.
IPAddress fromNumber(uint32_t value) {
  return IPAddress(value >> 24, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF);
}

// Passive buzzers need a tone while active buzzers use a steady pin level.
void setBuzzer(bool on) {
  if (BUZZER_IS_PASSIVE) {
    ledcWriteTone(BUZZER_CHANNEL, on ? 2400 : 0);
  } else {
    digitalWrite(BUZZER_PIN, (on == BUZZER_ACTIVE_HIGH) ? HIGH : LOW);
  }
}

// Acknowledge a beep after it ends and turn off the link LED when replies stop.
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

// Knob position as 0-10. Smoothing plus a dead band stops the number flickering between steps.
int readKnob() {
  const float raw = analogRead(ROTARY_PIN);
  knobSmoothed = knobSmoothed < 0 ? raw : 0.9f * knobSmoothed + 0.1f * raw;
  const float position = knobSmoothed * 10.0f / 4095.0f;
  if (fabsf(position - effortValue) > 0.7f) effortValue = constrain(int(position + 0.5f), 0, 10);
  return effortValue;
}

// Sample the initial knob position so only a later change starts the hold timer.
void startEffortPrompt(const String& id) {
  askingEffort = true;
  effortMoved = false;
  effortCommandId = id;
  knobSmoothed = -1;
  effortValue = 0;
  for (int i = 0; i < 20; i++) readKnob();  // Settle on the current knob position.
  effortAskedAt = effortChangedAt = millis();
  Serial.printf("Effort prompt %s\n", id.c_str());
}

// Save a rating after the knob moves and settles, or close an unanswered prompt.
void updateEffort(unsigned long now) {
  if (!askingEffort) return;
  const int before = effortValue;
  if (readKnob() != before) {
    effortMoved = true;
    effortChangedAt = now;
  }
  if (effortMoved && now - effortChangedAt >= EFFORT_HOLD_MS) {
    effortAnswerId = effortCommandId;
    effortAnswerValue = effortValue;
    askingEffort = false;
    effortSavedShownUntil = now + 2500;
    Serial.printf("Effort saved: %d/10\n", effortAnswerValue);
  } else if (now - effortAskedAt >= EFFORT_GIVE_UP_MS) {
    askingEffort = false;  // Nobody answered; the laptop drops the prompt too.
  }
}

// Show the hold countdown while choosing a rating and briefly confirm a saved answer.
void renderEffort(unsigned long now) {
  const bool saved = !askingEffort;
  const int value = saved ? effortAnswerValue : effortValue;
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print(saved ? "EFFORT SAVED" : "HOW HARD WAS THAT?");
  display.setTextSize(3);
  display.setCursor(0, 12);
  display.print(value);
  display.setTextSize(2);
  display.print("/10");
  display.drawRect(0, 40, 128, 8, SSD1306_WHITE);
  display.fillRect(0, 40, value * 128 / 10, 8, SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 54);
  if (saved) {
    display.print("Thanks!");
  } else if (!effortMoved) {
    display.print("Turn the knob 0-10");
  } else {
    const unsigned long held = now - effortChangedAt;
    display.printf("Hold still: saving %lu", (EFFORT_HOLD_MS - min(held, EFFORT_HOLD_MS) + 999) / 1000);
  }
}

void renderDisplay() {
  if (!displayReady) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  const unsigned long now = millis();
  if (askingEffort || static_cast<long>(effortSavedShownUntil - now) > 0) {
    renderEffort(now);
    display.display();
    return;
  }
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("CAREE BOX");
  display.setTextSize(2);
  display.setCursor(0, 12);
  display.print(deviceNumber);
  display.setTextSize(1);
  display.setCursor(0, 33);
  display.print(WiFi.status() != WL_CONNECTED ? "Joining WiFi..." :
                dashboardConnected ? "Dashboard connected" :
                serverKnown ? "Waiting for Flask..." : "Finding laptop...");
  display.setCursor(0, 44);
  display.print(dashboardConnected && assigned ? studentName.substring(0, 21) : "Not linked to student");
  display.setCursor(0, 55);
  if (dashboardConnected && assigned) {
    display.printf("%s R:%d P:%d", exercise == "arms" ? "ARM" : "LEG", fullReps, partialReps);
  } else if (WiFi.status() == WL_CONNECTED && !serverKnown) {
    display.print("Try " + scanning.toString());
  } else if (WiFi.status() == WL_CONNECTED) {
    display.print(WiFi.localIP().toString());
  }
  display.display();
}

// Refresh the assignment and handle the first command returned by the laptop.
void handleReply(JsonDocument& reply) {
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
    // Heartbeat retries can repeat a prompt that is already on screen or answered.
    if (id.length() && strcmp(type, "effort_prompt") == 0 && assigned && !askingEffort
        && id != effortCommandId && id != effortAnswerId) {
      startEffortPrompt(id);
    } else if (id.length() && id != lastCommandId && strcmp(type, "partial_rep") == 0 && assigned) {
      lastCommandId = id;
      pendingCommandId = id;
      const int duration = constrain(command["duration_ms"] | 180, 50, 500);
      setBuzzer(true);
      beeping = true;
      beepEndsAt = millis() + duration;
      Serial.printf("Partial rep beep: %s\n", id.c_str());
    }
  }
}

// Sends one heartbeat to ip. Returns true only when a CAREE server answered properly.
bool postHeartbeat(const IPAddress& ip, uint16_t connectTimeoutMs, bool quiet) {
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(connectTimeoutMs);
  http.setTimeout(800);
  String endpoint = "http://" + ip.toString() + ":" + String(SERVER_PORT) + "/api/devices/heartbeat";
  StaticJsonDocument<512> request;
  request["device_id"] = deviceId;
  request["name"] = deviceName;
  if (acknowledgedCommandId.length()) request["acknowledged_command_id"] = acknowledgedCommandId;
  // A saved effort answer rides along (and acknowledges its prompt) until the laptop accepts it.
  const bool sendingEffort = effortAnswerId.length() > 0 && !quiet;
  if (sendingEffort) {
    request["acknowledged_command_id"] = effortAnswerId;
    request["effort"]["command_id"] = effortAnswerId;
    request["effort"]["value"] = effortAnswerValue;
  }
  String payload;
  serializeJson(request, payload);
  if (!http.begin(client, endpoint)) return false;
  http.addHeader("Content-Type", "application/json");
  const int status = http.POST(payload);
  bool ok = false;
  if (status == 200) {
    StaticJsonDocument<2048> reply;
    const DeserializationError error = deserializeJson(reply, http.getString());
    if (!error && reply["device_id"].is<const char*>()) {
      if (sendingEffort) {
        Serial.printf("Effort %d/10 sent to laptop\n", effortAnswerValue);
        effortAnswerId = "";
      }
      handleReply(reply);
      ok = true;
    } else if (!quiet) {
      Serial.println("Invalid dashboard response");
    }
  } else if (!quiet) {
    Serial.printf("Heartbeat HTTP %d at %s\n", status, endpoint.c_str());
  }
  http.end();
  return ok;
}

// Search order: last laptop found, CAREE_SERVER_IP, the hotspot gateway (laptop hotspot),
// then every address in the subnet (phone hotspot: the laptop is just another client).
// Returns false once a full pass is done.
bool nextCandidate(IPAddress& out) {
  const IPAddress self = WiFi.localIP();
  const uint32_t mask = toNumber(WiFi.subnetMask());
  const uint32_t network = toNumber(self) & mask;
  const uint32_t hosts = (~mask < MAX_SCAN_HOSTS) ? ~mask : MAX_SCAN_HOSTS;
  while (true) {
    const uint32_t step = scanStep++;
    IPAddress candidate;
    if (step == 0) {
      candidate = lastServer;
    } else if (step == 1) {
      if (!candidate.fromString(CAREE_SERVER_IP)) continue;
    } else if (step == 2) {
      candidate = WiFi.gatewayIP();
    } else if (step - 3 + 1 < hosts) {
      candidate = fromNumber(network + (step - 3 + 1));
    } else {
      scanStep = 0;
      return false;
    }
    if (toNumber(candidate) == 0 || candidate == self) continue;
    out = candidate;
    return true;
  }
}

// Probe one candidate per call and pause before retrying an unsuccessful scan.
void searchForLaptop(unsigned long now) {
  if (static_cast<long>(now - scanPausedUntil) < 0) return;
  IPAddress candidate;
  if (!nextCandidate(candidate)) {
    scanPausedUntil = now + SCAN_PAUSE_MS;
    return;
  }
  scanning = candidate;
  renderDisplay();
  if (postHeartbeat(candidate, SCAN_TIMEOUT_MS, true)) {
    serverIp = lastServer = candidate;
    serverKnown = true;
    failedPolls = 0;
    scanStep = 0;
    Serial.printf("Found CAREE laptop at %s:%u\n", candidate.toString().c_str(), SERVER_PORT);
  }
}

// Restart discovery while keeping the previous address as the first candidate.
void forgetServer(const char* reason) {
  if (!serverKnown) return;
  serverKnown = false;
  dashboardConnected = false;
  scanStep = 0;
  scanPausedUntil = 0;
  Serial.printf("Lost laptop (%s); searching again\n", reason);
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
  analogReadResolution(12);
  analogSetPinAttenuation(ROTARY_PIN, ADC_11db);  // Full 0-3.3 V knob range.

  WiFi.mode(WIFI_STA);
  deviceId = WiFi.macAddress();
  deviceId.toLowerCase();
  String compact = deviceId;
  compact.replace(":", "");
  deviceNumber = compact.substring(6);
  deviceNumber.toUpperCase();
  deviceName = "ESP32-" + deviceNumber;
  Serial.printf("CAREE box %s | MAC %s\n", deviceNumber.c_str(), deviceId.c_str());

  Wire.begin(OLED_SDA_PIN, OLED_SCL_PIN);
  Wire.setTimeOut(50);
  // Probe both OLED addresses. If neither works, heartbeats and the buzzer still run.
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
  Serial.printf("Joining hotspot \"%s\"...\n", HOTSPOT_SSID);
}

void loop() {
  const unsigned long now = millis();
  updateIndicators();
  updateEffort(now);
  const bool online = WiFi.status() == WL_CONNECTED;
  // After Wi-Fi drops, rediscover the laptop when the hotspot connection returns.
  if (!online) forgetServer("WiFi dropped");
  // No HTTP requests during a beep, so network timeouts cannot hold SIG active.
  if (!beeping && online) {
    if (!serverKnown) {
      searchForLaptop(now);
    } else if (now - lastPoll >= POLL_INTERVAL_MS) {
      lastPoll = now;
      if (postHeartbeat(serverIp, 800, false)) {
        failedPolls = 0;
      } else if (++failedPolls >= LOST_AFTER_FAILED_POLLS) {
        forgetServer("no replies");
      }
    }
    updateIndicators();
  }
  if (!online && now - lastReconnect >= 15000) {
    lastReconnect = now;
    WiFi.reconnect();
  }
  if (now - lastDisplay >= 250) {
    lastDisplay = now;
    renderDisplay();
  }
  delay(5);
}
