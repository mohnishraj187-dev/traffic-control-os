#include <WiFi.h>
#include <HTTPClient.h>

const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// Use your laptop's LAN IP, not 127.0.0.1, when the ESP32 calls the backend.
const char* LANE_NAME = "Eastbound camera";
const char* API_BASE_URL = "http://192.168.1.10:5000/api/iot/signal-state";
const char* IOT_TOKEN = "dev-traffic-node";

// These pins are usable on many ESP32-CAM boards when the SD card is not used.
// Change them if your wiring or board variant needs different GPIOs.
const int RED_PIN = 12;
const int YELLOW_PIN = 13;
const int GREEN_PIN = 14;

String extractSignal(const String& body) {
  int key = body.indexOf("\"signal\"");
  if (key < 0) return "stop";
  int colon = body.indexOf(':', key);
  int firstQuote = body.indexOf('"', colon + 1);
  int secondQuote = body.indexOf('"', firstQuote + 1);
  if (firstQuote < 0 || secondQuote < 0) return "stop";
  return body.substring(firstQuote + 1, secondQuote);
}

void applySignal(const String& signal) {
  digitalWrite(RED_PIN, signal == "stop" ? HIGH : LOW);
  digitalWrite(YELLOW_PIN, (signal == "slow" || signal == "ai") ? HIGH : LOW);
  digitalWrite(GREEN_PIN, signal == "go" ? HIGH : LOW);
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
}

void setup() {
  pinMode(RED_PIN, OUTPUT);
  pinMode(YELLOW_PIN, OUTPUT);
  pinMode(GREEN_PIN, OUTPUT);
  applySignal("stop");
  connectWifi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }

  HTTPClient http;
  String url = String(API_BASE_URL) + "?token=" + IOT_TOKEN + "&lane=" + LANE_NAME;
  url.replace(" ", "%20");
  http.begin(url);
  int status = http.GET();
  if (status == 200) {
    String body = http.getString();
    applySignal(extractSignal(body));
  } else {
    applySignal("stop");
  }
  http.end();

  delay(1000);
}
