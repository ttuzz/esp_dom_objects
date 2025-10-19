#ifndef OBJELERIM_H
#define OBJELERIM_H

#include "dom_objects.h"
// typed laser struct and instance (declarations only)
typedef struct {
  bool enabled;
  double power;
  String mode;
} laser_obj;

typedef struct {
  double temperature;
  bool active;
  String profile;
} plasma_obj;


// Concrete instances and schemas are defined in objelerim.cpp.
// This header only provides extern declarations so other units can reference
// the builtins without creating duplicate definitions.
extern laser_obj laser_instance;
extern plasma_obj plasma_instance;
extern const ObjSchema laserSchema;
extern const ObjSchema plasmaSchema;

#endif // OBJELERIM_H
