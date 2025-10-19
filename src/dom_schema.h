#ifndef DOM_SCHEMA_H
#define DOM_SCHEMA_H

#include "dom_objects.h"

// Schema registry API
void dom_register_schema(const ObjSchema &s);
bool dom_schema_exists(const String &name);
const ObjSchema* dom_get_schema(const String &name);

#endif // DOM_SCHEMA_H
