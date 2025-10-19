#include "dom_objects.h"
#include "objelerim.h"
#include <stddef.h>


// register typed instances (called from dom_init)
void dom_register_builtins() {
  dom_register_typed_object(String("laser"), &laser_instance);
  dom_register_typed_object(String("plasma"), &plasma_instance);
}

void dom_init() {
  // initial example object
  // register schema; do not eagerly instantiate all objects (use lazy init)
  dom_register_schema(laserSchema);
  dom_register_schema(plasmaSchema);
  // register any typed instances / builtin objects from data file
  extern void dom_register_builtins();
  dom_register_builtins();
}
