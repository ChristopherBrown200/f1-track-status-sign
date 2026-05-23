/*
F1 Track Status Sign ESP32 Code
By: Christopher Brown (https://github.com/ChristopherBrown200)

Reads from pi sercer and displasy the current track status

Flag effects:
  0 No Session          Dim white
  1 Green flag          Solid green
  2 Yellow flag         Ribbon effect - yellow over green
  3 Safey Car Deployed  Pulse Orange
  4 Safety car          Ribbon effect - orange over yellow
  5 Red flag            Solid Red
  6 VSC                 Pulse Yellow
  7 VSC ending          Quickly Flash Yellow
  - Winner              Rotating team colour bands

  - Server unreachable  Ribbon effect - red
  - Connecting to WiFi  Ribbon effect - white

Libraries needed (install via Arduino Library Manager):
  FastLED      by Daniel Garcia
  ArduinoJson  by Benoit Blanchon
*/

// == Libraries ===================================================================================
#include <FastLED.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// == LED Values ==================================================================================
// LED Hardware Details Constants
#define LED_PIN     32
#define NUM_LEDS    48
#define LED_TYPE    WS2812B
#define COLOR_ORDER GRB

// LED State Array
CRGB leds[NUM_LEDS];

// Colors Constants
#define COL_DEFAULT     CRGB(15, 15, 10)
#define COL_GREEN       CRGB(0, 210, 0)
#define COL_GREEN_DIM   CRGB(0, 60, 0)
#define COL_YELLOW      CRGB(255, 200, 0)
#define COL_YELLOW_DIM  CRGB(71, 56, 0)
#define COL_ORANGE      CRGB(255, 100, 0)
#define COL_RED         CRGB(220, 0, 0)
#define COL_WHITE       CRGB(255, 255, 255)

// LED Effect Constants
#define BRIGHTNESS          255
#define RIBBON_EFFECT_WIDTH 7

// == Connectivity Constants ======================================================================
// Wi-Fi Details and Server IP
#include "credentials.h"

// Data Server Details
#define HOST_PORT         5000
#define POLL_INTERVAL     10000
#define MAX_FAILED_POLLS  3

// == State Values ================================================================================
// Connectivity
bool wifiConnected      = false;
bool serverUnreachable  = false;
unsigned long lastPoll  = 0;
int  failedPollsCount   = 0;

// Track Status
int  currentStatus  = 0;
bool showingWinner  = false;
CRGB winnerColor1   = CRGB::White;
CRGB winnerColor2   = CRGB::Black;

// Effect Details
unsigned long lastEffect  = 0;
uint32_t  effectStep      = 0;
bool     effectToggle     = false;

// == Setup =======================================================================================
void setup() {
  Serial.begin(115200);

  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection(TypicalLEDStrip);
  FastLED.setBrightness(BRIGHTNESS);

  bootAnimation();
  connectWifi();

  // Runs polling the server on another core to keep smooth animation of effects
  xTaskCreatePinnedToCore(fetchTask, "fetchTask", 8192, NULL, 1, NULL, 0);
}

// == Main ========================================================================================
void loop() {
  // If WiFi connection is lost reconnect
  if (WiFi.status() != WL_CONNECTED) {
    wifiConnected = false;
    connectWifi();
    return;
  }

  // Run LED Animations
  runEffect(millis());
}

// == WiFi ========================================================================================
void connectWifi() {
  Serial.printf("Connecting to %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    ribbonEffect(millis(), CRGB::Black, COL_WHITE);
  }

  wifiConnected = true;
  Serial.println("WiFi connected!");
}

// == Polling Track Status From Server ============================================================
// Task sent to other core
void fetchTask(void* parameter){
  while (true){
    if (wifiConnected) fetchStatus();

    vTaskDelay(POLL_INTERVAL / portTICK_PERIOD_MS);
  }
}

// Fetches and Pharses Status Data
void fetchStatus() {
  HTTPClient http;
  String url = "http://" + String(HOST_IP) + ":" + String(HOST_PORT) + "/status";
  http.begin(url);
  int code = http.GET();

  if (code == 200) {
    String body = http.getString();
    DynamicJsonDocument doc(512);

    if (!deserializeJson(doc, body)) {
      failedPollsCount = 0;
      serverUnreachable = false;

      // If Color Present Start Winner Animation
      if (!doc["winner_color"].isNull()) {
        const char* hex = doc["winner_color"].as<const char*>();
        CRGB color = hexToRgb(hex);

        if (!showingWinner) {
          Serial.printf("Winner color: #%s\n", hex);
          winnerColor1 = color;
          winnerColor2 = CRGB(color.r / 5, color.g / 5, color.b / 5);
          showingWinner = true;
          effectStep = 0;
        }
        http.end();
        return;
      }

      // Clears Winner if Showing
      if (showingWinner) {
        Serial.println("Winner color cleared — returning to normal.");
        showingWinner = false;
        effectStep = 0;
      }

      // Resets Winner if New Session is Started
      bool sessionActive = doc["session_active"].as<bool>();
      if (showingWinner && sessionActive) {
        Serial.println("New session started — resetting winner display.");
        showingWinner = false;
        effectStep = 0;
      }

      int status = String(doc["status"].as<const char*>()).toInt();
      status = constrain(status, 0, 7);

      if (status != currentStatus) {
        Serial.printf("Status %d -> %d (%s)\n",
          currentStatus, status, doc["message"].as<const char*>());
        currentStatus = status;
        effectStep = 0;
        effectToggle = false;
      }
    }
  } else {
    failedPollsCount++;
    Serial.printf("HTTP error %d — is the Pi server running? (%d/%d)\n", code, failedPollsCount, MAX_FAILED_POLLS);
    if (failedPollsCount >= MAX_FAILED_POLLS) serverUnreachable = true;
  }

  http.end();
}

// Converts Hex Color into CRGB
CRGB hexToRgb(const char* hex) {
  if (!hex || strlen(hex) < 6) return CRGB::White;
  long value = strtol(hex, nullptr, 16);
  return CRGB((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF);
}

// == LED Effects =================================================================================
// Start Up Boot Animation
void bootAnimation() {
  for (int i = 0; i < NUM_LEDS; i += 2) {
    leds[i] = COL_RED;
    leds[NUM_LEDS - i + 1] = COL_YELLOW;
    FastLED.show();
    delay(30);
  }
  for (int b = 255; b >= 0; b -= 5) {
    FastLED.setBrightness(b);
    FastLED.show();
    delay(10);
  }
  FastLED.setBrightness(BRIGHTNESS);
  fill_solid(leds, NUM_LEDS, CRGB::Black);
  FastLED.show();
}

// Selects Effect Based on Status
void runEffect(unsigned long now) {
  if (serverUnreachable) {
    ribbonEffect(now, CRGB::Black, COL_RED);
    return;
  }
  if (showingWinner) {
    effectWinner(now);
    return;
  }

  switch (currentStatus) {
    case 0:
      fill_solid(leds, NUM_LEDS, COL_DEFAULT);
      FastLED.show();
      break;
    case 1:
      solidColorEffect(COL_GREEN);
      break;
    case 2:
      ribbonEffect(now, COL_GREEN_DIM, COL_YELLOW);
      break;
    case 3:
      effectPulse(now, COL_ORANGE);
      break;
    case 4:
      ribbonEffect(now, COL_YELLOW_DIM, COL_ORANGE);
      break;
    case 5:
      solidColorEffect(COL_RED);
      break;
    case 6:
      effectPulse(now, COL_YELLOW);
      break;
    case 7:
      effectFastFlash(now, COL_YELLOW);
      break;
  }
}

// Makes All LEDs a Single Color
void solidColorEffect(CRGB color){
  fill_solid(leds, NUM_LEDS, color);
  FastLED.show();
}

// Makes All LEDs Pulse a Single Color
void effectPulse(unsigned long now, CRGB color) {
  if (now - lastEffect < 20) return;
  lastEffect = now;

  CRGB dim = CRGB(color.r / 5, color.g / 5, color.b / 5);

  float val = abs(sin(effectStep * 0.025f));
  CRGB c = blend(dim, color, (uint8_t)(val * 255));
  fill_solid(leds, NUM_LEDS, c);
  FastLED.show();
  effectStep++;
}

// Makes All LEDs Quickly Flash a Single Color
void effectFastFlash(unsigned long now, CRGB color) {
  if (now - lastEffect < 120) return;
  lastEffect   = now;
  effectToggle = !effectToggle;
  fill_solid(leds, NUM_LEDS, effectToggle ? COL_YELLOW : CRGB::Black);
  FastLED.show();
}

// Makes a segment of LEDs Move Around the Sign Over a Background Color
void ribbonEffect(unsigned long now, CRGB mainColor, CRGB ribbonColor) {
  if (now - lastEffect < (80)) return;

  lastEffect = now;

  for (int i = 0; i < NUM_LEDS; i++){
    int zone = ((i + effectStep) % NUM_LEDS) / RIBBON_EFFECT_WIDTH;
    leds[i] = (zone == 0) ? ribbonColor : mainColor;
  }

  FastLED.show();
  effectStep++;
}

// Displays Effect with Winning Team Colors
void effectWinner(unsigned long now) {
  if (now - lastEffect < 80) return;
  lastEffect = now;
  int seg = NUM_LEDS / 2;
  for (int i = 0; i < NUM_LEDS; i++) {
    int zone = ((i + effectStep) % NUM_LEDS) / seg;
    leds[i] = (zone == 0) ? winnerColor1 : winnerColor2;
  }
  FastLED.show();
  effectStep++;
}


