#include "dom_schema.h"
#include <Arduino.h>

// Small, fixed-size registry of schema pointers. This avoids heap
// allocations (no std::map nodes). It assumes that registered ObjSchema
// instances (e.g. laserSchema) are static/global and live for program lifetime.
static const size_t MAX_SCHEMAS = 32;
static const ObjSchema* schema_list[MAX_SCHEMAS];
static size_t schema_count = 0;

void dom_register_schema(const ObjSchema &s) {
  // ignore duplicates: if name already registered, update the pointer
  for (size_t i = 0; i < schema_count; ++i) {
    if (String(schema_list[i]->objName) == String(s.objName)) {
      schema_list[i] = &s;
      return;
    }
  }
  if (schema_count < MAX_SCHEMAS) {
    schema_list[schema_count++] = &s;
  }
}

bool dom_schema_exists(const String &name) {
  for (size_t i = 0; i < schema_count; ++i) if (String(schema_list[i]->objName) == name) return true;
  return false;
}

const ObjSchema* dom_get_schema(const String &name) {
  for (size_t i = 0; i < schema_count; ++i) if (String(schema_list[i]->objName) == name) return schema_list[i];
  return nullptr;
}
