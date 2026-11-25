#pragma once
// Copy to secrets.h (ignored by git). The SSID must match exactly, including capitals,
// spaces and an iPhone's curly apostrophe (write it as "\xE2\x80\x99").
// The box only joins 2.4 GHz Wi-Fi: on iPhone turn on Personal Hotspot > Maximize Compatibility.
constexpr const char* HOTSPOT_SSID = "YOUR_HOTSPOT_NAME";
constexpr const char* HOTSPOT_PASSWORD = "YOUR_HOTSPOT_PASSWORD";
// Optional: pin the laptop's address. Leave commented out and the box finds the laptop itself.
// #define CAREE_SERVER_IP "172.20.10.6"
