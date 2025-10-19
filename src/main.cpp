// Main firmware: delegates object handling to dom_objects module
#include <Arduino.h>
#include "dom_objects.h"
#include "objelerim.h"

void setup() {
  Serial.begin(115200);
  // seed pseudo-random generator from floating analog pin
  randomSeed(analogRead(A0));
  dom_init();
}

// Read newline-delimited JSON lines from Serial into a buffer and
// process complete lines. Also drops partial buffers if no bytes
// arrive within RX_TIMEOUT_MS.
static void process_serial_input() {
  static String buf;
  static unsigned long lastRx = 0;
  const unsigned long RX_TIMEOUT_MS = 300; // ms
  while (Serial.available()) {
    char c = (char)Serial.read();
    // record time of this received byte
    lastRx = millis();
    if (c == '\n') {
      String line = buf;
      buf = "";
  dom_process_line(line);
      // reset lastRx so timeout doesn't immediately clear next packet
      lastRx = 0;
    } else {
      buf += c;
      if (buf.length() > 4000) buf = "";
    }
  }

  // if we have a partial buffer and no bytes arrived for RX_TIMEOUT_MS, drop it
  if (buf.length() > 0 && lastRx != 0) {
    if ((millis() - lastRx) > RX_TIMEOUT_MS) {
      buf = "";
      lastRx = 0;
    }
  }
}

void loop() {
  process_serial_input();

  // produce a random float and push into runtime once per second
  static unsigned long _last_random_ms = 0;
  const unsigned long RANDOM_INTERVAL_MS = 1000;
  unsigned long _now = millis();
  if (_last_random_ms == 0) _last_random_ms = _now;
  if ((_now - _last_random_ms) >= RANDOM_INTERVAL_MS) {
    _last_random_ms = _now;
  // generate a random float in range 10.00 .. 40.00
  float rnd = (float)random(1000, 4000) / 100.0f;
  // Struct-first workflow: write directly to your typed structs and
  // then push their values into the JSON runtime so clients see them.
  laser_instance.power = rnd;
  dom_push_struct_to_json(String("laser"));
  plasma_instance.temperature = (double)rnd;
  dom_push_struct_to_json(String("plasma"));
  }

  dom_tick();
}