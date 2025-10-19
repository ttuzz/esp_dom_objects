#include "objelerim.h"

// typed instances
laser_obj laser_instance = { false, 0.0, String("yok") };
plasma_obj plasma_instance = { 0.0, false, String("yok") };

// define schema for 'laser' with absolute addresses to fields
static const FieldSchema laserFields[] = {
  { "enabled", "boolean", 0, &laser_instance.enabled },
  { "power",   "number",  0, &laser_instance.power   },
  { "mode",    "string",  0, &laser_instance.mode    }
};

// define schema for 'plasma' with absolute addresses to fields
static const FieldSchema plasmaFields[] = {
  { "temperature", "number", 0, &plasma_instance.temperature },
  { "active",      "boolean", 0, &plasma_instance.active      },
  { "profile",     "string",  0, &plasma_instance.profile     }
};

// ObjSchema definitions (external linkage)
// marks: subscribable, readOnly, discoverable
// Set subscribable=true for builtins so clients may subscribe to updates.
const ObjSchema laserSchema = { "laser", laserFields, 3, true, false, true };
const ObjSchema plasmaSchema = { "plasma", plasmaFields, 3, true, false, true };
