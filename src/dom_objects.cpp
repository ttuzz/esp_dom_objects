// dom_objects.cpp
#include "dom_objects.h"
#include "dom_schema.h"
#include <ArduinoJson.h>
#include <map>
#include <set>
#include <stddef.h>

using namespace std;

// runtime objects are stored generically (JSON-backed) to support arbitrary schemas
static std::map<String, struct GenericObject*> objects;
static std::set<String> subscribers;
static unsigned long lastSend = 0;

// map of registered typed object instances (name -> pointer)
static std::map<String, void*> typedObjects;

// schemas and typed instances are defined in dom_objects_data.cpp
extern const ObjSchema laserSchema;
extern const ObjSchema plasmaSchema;

// Generic JSON-backed object container
struct GenericObject {
  DynamicJsonDocument *doc;
  GenericObject(size_t cap = 256) { doc = new DynamicJsonDocument(cap); doc->to<JsonObject>(); }
  ~GenericObject() { delete doc; }
  JsonObject state() { return doc->as<JsonObject>(); }
};

// create a GenericObject initialized from schema (returns pointer owned by objects map)
static GenericObject *create_object_from_schema(const String &name) {
  if (!dom_schema_exists(name)) return nullptr;
  const ObjSchema &s = *dom_get_schema(name);
  size_t cap = 256 + s.fieldCount * 64;
  GenericObject *g = new GenericObject(cap);
  JsonObject st = g->state();
  for (uint8_t i = 0; i < s.fieldCount; ++i) {
    const FieldSchema &f = s.fields[i];
    if (strcmp(f.type, "boolean") == 0) st[f.name] = false;
    else if (strcmp(f.type, "number") == 0) st[f.name] = 0.0;
    else if (strcmp(f.type, "string") == 0) st[f.name] = String("");
  }
  objects[name] = g;
  return g;
}

// ensure object exists (create from schema or allocate empty)
static GenericObject *ensure_object(const String &name) {
  if (objects.count(name)) return objects[name];
  if (dom_schema_exists(name)) return create_object_from_schema(name);
  // Do not create a generic fallback object for unknown names. Caller must
  // handle the nullptr (e.g., return not_found). This keeps runtime state
  // pointer-based except for schema-backed objects.
  return nullptr;
}

static void sendJson(const JsonDocument &doc) {
  String out;
  serializeJson(doc, out);
  Serial.println(out);
}

// single reusable scratch document to avoid frequent DynamicJsonDocument
// allocations in hot paths. Make it large enough for the biggest message we
// expect; functions will clear it on entry.
static DynamicJsonDocument &dom_scratch_doc() {
  static DynamicJsonDocument scratch(4096);
  scratch.clear();
  return scratch;
}


// register pointer to typed struct instance for later sync
void dom_register_typed_object(const String &name, void *ptr) {
  typedObjects[name] = ptr;
}

// schema registry is implemented in dom_schema.cpp

// write a JsonVariant value into a typed struct field using either
// a direct absolute address (f.addr) when present or an offset from
// the base pointer (basePtr + f.offset) otherwise.
static void write_variant_to_typed_field(void *basePtr, const FieldSchema &f, JsonVariantConst v) {
  if (!basePtr && f.addr == nullptr) return;
  char *addr = nullptr;
  if (f.addr != nullptr) {
    addr = (char*)f.addr;
  } else {
    addr = ((char*)basePtr) + f.offset;
  }
  if (!addr) return;
  if (strcmp(f.type, "number") == 0) {
    double val = 0.0;
    if (v.is<double>()) val = v.as<double>();
    else if (v.is<long>()) val = (double)v.as<long>();
    *((double*)addr) = val;
  } else if (strcmp(f.type, "boolean") == 0) {
    bool b = v.is<bool>() ? v.as<bool>() : false;
    *((bool*)addr) = b;
  } else if (strcmp(f.type, "string") == 0) {
    const char *s = v.is<const char*>() ? v.as<const char*>() : "";
    String *sp = (String*)addr;
    *sp = String(s);
  }
}

// sync JSON object into typed struct for object 'name'
static void sync_json_to_typed(const String &name, JsonObject st) {
  if (!typedObjects.count(name)) return;
  void *base = typedObjects[name];
  if (!dom_schema_exists(name)) return;
  const ObjSchema &s = *dom_get_schema(name);
  for (uint8_t i = 0; i < s.fieldCount; ++i) {
    const FieldSchema &f = s.fields[i];
    // skip fields that have neither an addr nor a non-zero offset
    if (f.addr == nullptr && f.offset == 0) continue;
    if (st.containsKey(f.name)) write_variant_to_typed_field(base, f, st[f.name]);
  }
}

// maximum number of subscribed objects to actively poll/update per tick
static size_t max_active_subscribers = 5;

// caller can adjust this at runtime if needed
void dom_set_max_active_subscribers(size_t n) {
  max_active_subscribers = n;
}

static void handle_discover(const String &id, const String &path) {
  DynamicJsonDocument &doc = dom_scratch_doc();
  doc["type"] = "discover.response";
  doc["id"] = id;
  // discovered only when a schema exists and is marked discoverable
  bool found = false;
  if (dom_schema_exists(path)) {
    const ObjSchema &schemaDef = *dom_get_schema(path);
    if (schemaDef.discoverable) found = true;
  }
  doc["found"] = found;
  if (found) {
    JsonObject schema = doc.createNestedObject("schema");
    schema["name"] = path;
    // subscription metadata (runtime)
    int sub_count = subscribers.count(path) ? 1 : 0;
    schema["subscriber_count"] = sub_count;
    schema["subscribed"] = subscribers.count(path) > 0;
    JsonArray fields = schema.createNestedArray("fields");
    if (dom_schema_exists(path)) {
      const ObjSchema &schemaDef2 = *dom_get_schema(path);
      // expose schema-level hints
      schema["subscribable"] = schemaDef2.subscribable;
      schema["readOnly"] = schemaDef2.readOnly;
      schema["discoverable"] = schemaDef2.discoverable;
      for (uint8_t i = 0; i < schemaDef2.fieldCount; ++i) {
        JsonObject f = fields.createNestedObject();
        f["name"] = schemaDef2.fields[i].name;
        f["type"] = schemaDef2.fields[i].type;
      }
    } else if (objects.count(path)) {
      // infer fields from runtime object
      JsonObject st = objects[path]->state();
      for (JsonPair p : st) {
        JsonObject f = fields.createNestedObject();
        f["name"] = p.key().c_str();
        // best-effort type inference
        if (p.value().is<bool>()) f["type"] = "boolean";
        else if (p.value().is<long>() || p.value().is<double>()) f["type"] = "number";
        else f["type"] = "string";
      }
    }
  }
  sendJson(doc);
}

static void handle_get(const String &id, const String &path) {
  DynamicJsonDocument &doc = dom_scratch_doc();
  doc["type"] = "state";
  doc["id"] = id;
  doc["path"] = path;
  if (objects.count(path)) {
    JsonObject obj = doc.createNestedObject("value");
    JsonObject st = objects[path]->state();
    // include runtime subscription metadata alongside the value
    JsonObject meta = doc.createNestedObject("_meta");
    size_t sub_count = 0;
    if (subscribers.count(path)) {
      for (const String &s : subscribers) if (s == path) ++sub_count;
    }
    meta["subscriber_count"] = (int)sub_count;
    meta["subscribed"] = subscribers.count(path) > 0;
    // include schema-level hints if available
    if (dom_schema_exists(path)) {
      const ObjSchema &s = *dom_get_schema(path);
      meta["subscribable"] = s.subscribable;
      meta["readOnly"] = s.readOnly;
      meta["discoverable"] = s.discoverable;
    }
    if (dom_schema_exists(path)) {
      const ObjSchema &s = *dom_get_schema(path);
      for (uint8_t i = 0; i < s.fieldCount; ++i) {
        const FieldSchema &f = s.fields[i];
        const char *fname = f.name;
        if (st.containsKey(fname)) obj[fname] = st[fname];
        else {
          if (strcmp(f.type, "boolean") == 0) obj[fname] = false;
          else if (strcmp(f.type, "number") == 0) obj[fname] = 0.0;
          else obj[fname] = String("");
        }
      }
    } else {
      for (JsonPair p : st) obj[p.key().c_str()] = p.value();
    }
  } else {
    doc["error"] = "not_found";
  }
  sendJson(doc);
}

static void handle_subscribe(const String &id, const String &path) {
  // subscribe only allowed if object was discoverable (schema exists and discoverable)
  if (!dom_schema_exists(path)) {
    DynamicJsonDocument &doc = dom_scratch_doc();
    doc["type"] = "subscribe.response";
    doc["id"] = id;
    doc["path"] = path;
    doc["error"] = "not_found";
    sendJson(doc);
    return;
  }
  const ObjSchema &schema = *dom_get_schema(path);
  if (!schema.discoverable) {
  DynamicJsonDocument &doc = dom_scratch_doc();
    doc["type"] = "subscribe.response";
    doc["id"] = id;
    doc["path"] = path;
    doc["error"] = "not_discoverable";
    sendJson(doc);
    return;
  }
  if (!schema.subscribable) {
    DynamicJsonDocument &doc = dom_scratch_doc();
    doc["type"] = "subscribe.response";
    doc["id"] = id;
    doc["path"] = path;
    doc["error"] = "not_subscribable";
    sendJson(doc);
    return;
  }

  // lazy-init object from schema if needed
  if (!objects.count(path)) create_object_from_schema(path);

  subscribers.insert(path);
  DynamicJsonDocument &doc = dom_scratch_doc();
  doc["type"] = "subscribe.response";
  doc["id"] = id;
  doc["path"] = path;
  // include runtime subscription metadata
  int sub_count = subscribers.count(path) ? 1 : 0;
  doc["subscriber_count"] = sub_count;
  doc["subscribed"] = sub_count > 0;
  sendJson(doc);
  // immediate state for convenience
  handle_get(String("get-")+path, path);
}

static void handle_unsubscribe(const String &id, const String &path) {
  subscribers.erase(path);
  DynamicJsonDocument &doc = dom_scratch_doc();
  doc["type"] = "unsubscribe.response";
  doc["id"] = id;
  doc["path"] = path;
  // include runtime subscription metadata
  size_t sub_count = 0;
  if (subscribers.count(path)) {
    for (const String &s : subscribers) if (s == path) ++sub_count;
  }
  doc["subscriber_count"] = (int)sub_count;
  doc["subscribed"] = subscribers.count(path) > 0;
  // hint to client: object removed from subscription list; client may delete cached view
  doc["removed"] = true;
  sendJson(doc);
}

static void handle_set(const String &id, const String &path, JsonObject changes) {
  // respect schema readOnly hint
  if (dom_schema_exists(path)) {
    const ObjSchema &s = *dom_get_schema(path);
    if (s.readOnly) {
      DynamicJsonDocument &doc = dom_scratch_doc();
      doc["type"] = "set.response";
      doc["id"] = id;
      doc["path"] = path;
      doc["error"] = "read_only";
      sendJson(doc);
      return;
    }
  }
  GenericObject *g = ensure_object(path);
  if (!g) {
    // object doesn't exist and no schema -> not_found
    DynamicJsonDocument &doc = dom_scratch_doc();
    doc["type"] = "set.response";
    doc["id"] = id;
    doc["path"] = path;
    doc["error"] = "not_found";
    sendJson(doc);
    return;
  }
  JsonObject st = g->state();
  // apply changes generically
  for (JsonPair p : changes) {
    st[p.key().c_str()] = p.value();
  }
  // sync into any registered typed struct for this object
  sync_json_to_typed(path, st);
  // send only an update delta (clients merge into their cache)
  // Only send updates if there are active subscribers and the schema allows
  // subscriptions. Always acknowledge the set regardless.
  bool should_send_update = false;
  if (subscribers.count(path)) {
    should_send_update = true;
    if (dom_schema_exists(path)) {
      const ObjSchema &ss = *dom_get_schema(path);
      if (!ss.subscribable) should_send_update = false;
    }
  }
  if (should_send_update) {
    DynamicJsonDocument &up = dom_scratch_doc();
    up["type"] = "update";
    up["path"] = path;
    JsonObject changesOut = up.createNestedObject("changes");
    for (JsonPair p : changes) changesOut[p.key().c_str()] = p.value();
    sendJson(up);
  }
  // acknowledge set
  DynamicJsonDocument &ack = dom_scratch_doc();
  ack["type"] = "set.response";
  ack["id"] = id;
  ack["path"] = path;
  sendJson(ack);
}

static void process_line_internal(const String &line) {
  DynamicJsonDocument msg(512);
  auto err = deserializeJson(msg, line);
  if (err) return;
  String type = msg["type"].as<String>();
  String id = msg.containsKey("id") ? msg["id"].as<String>() : String();
  String path = msg.containsKey("path") ? msg["path"].as<String>() : String();
  if (type == "discover") handle_discover(id, path);
  else if (type == "get") handle_get(id, path);
  else if (type == "subscribe") handle_subscribe(id, path);
  else if (type == "unsubscribe") handle_unsubscribe(id, path);
  else if (type == "set") {
    if (msg.containsKey("changes")) handle_set(id, path, msg["changes"].as<JsonObject>());
  }
  else if (type == "delete") {
    if (msg.containsKey("field") && objects.count(path)) {
      String f = msg["field"].as<String>();
      JsonObject st = objects[path]->state();
      st[f] = String("deleted");
      // Only emit update if subscribers exist and schema allows it
      bool send_del = false;
      if (subscribers.count(path)) {
        send_del = true;
        if (dom_schema_exists(path)) {
          const ObjSchema &ss = *dom_get_schema(path);
          if (!ss.subscribable) send_del = false;
        }
      }
      if (send_del) {
        DynamicJsonDocument &up = dom_scratch_doc();
        up["type"] = "update";
        up["path"] = path;
        JsonObject c = up.createNestedObject("changes");
        c[f] = String("deleted");
        sendJson(up);
      }
      // also send a full 'state' message so clients can display the complete object
      DynamicJsonDocument &full = dom_scratch_doc();
      full["type"] = "state";
      full["path"] = path;
      JsonObject fv = full.createNestedObject("value");
      for (JsonPair pr : st) fv[pr.key().c_str()] = pr.value();
      sendJson(full);
    }
  }
}


void dom_process_line(const String &line) {
  process_line_internal(line);
}

// dom_tick: send current state/updates for subscribed objects (no mutation).
void dom_tick() {
  unsigned long now = millis();
  if (now - lastSend <= 500) return;
  lastSend = now;

  size_t sent = 0;
  for (auto it = subscribers.begin(); it != subscribers.end() && sent < max_active_subscribers; ++it) {
    const String &p = *it;
    if (!objects.count(p)) continue;
    // respect schema-level subscribable hint
    if (dom_schema_exists(p)) {
      const ObjSchema &ss = *dom_get_schema(p);
      if (!ss.subscribable) continue;
    }
    GenericObject *g = objects[p];
    JsonObject st = g->state();

  DynamicJsonDocument &up = dom_scratch_doc();
  up["type"] = "update";
  up["path"] = p;
  JsonObject ch = up.createNestedObject("changes");

    if (dom_schema_exists(p)) {
      const ObjSchema &s = *dom_get_schema(p);
      for (uint8_t i = 0; i < s.fieldCount; ++i) {
        const FieldSchema &f = s.fields[i];
        const char *fname = f.name;
        if (st.containsKey(fname)) ch[fname] = st[fname];
        else {
          if (strcmp(f.type, "boolean") == 0) ch[fname] = false;
          else if (strcmp(f.type, "number") == 0) ch[fname] = 0.0;
          else ch[fname] = String("");
        }
      }
    } else {
      for (JsonPair pr : st) ch[pr.key().c_str()] = pr.value();
    }

    sendJson(up);
    ++sent;
  }
}

// Set numeric field on object and emit update
void dom_set_field_number(const String &path, const char *field, double value) {
  GenericObject *g = ensure_object(path);
  if (!g) return; // nothing to do for unknown objects
  JsonObject st = g->state();
  st[field] = value;
  // Only send update if subscribers exist and schema allows it
  bool should_send = false;
  if (subscribers.count(path)) {
    should_send = true;
    if (dom_schema_exists(path)) {
      const ObjSchema &ss = *dom_get_schema(path);
      if (!ss.subscribable) should_send = false;
    }
  }
  if (should_send) {
    DynamicJsonDocument &up = dom_scratch_doc();
    up["type"] = "update";
    up["path"] = path;
    JsonObject ch = up.createNestedObject("changes");
    ch[field] = value;
    sendJson(up);
  }
  // sync typed struct if registered
  sync_json_to_typed(path, st);
}

// Push a registered typed struct's fields into the JSON runtime and emit an update
void dom_push_struct_to_json(const String &name) {
  if (!typedObjects.count(name)) return;
  if (!dom_schema_exists(name)) return;
  void *base = typedObjects[name];
  const ObjSchema &s = *dom_get_schema(name);

  GenericObject *g = ensure_object(name);
  JsonObject st = g->state();

  // write each field from the struct into JSON using either the
  // absolute address (f.addr) when present or offset from base.
  for (uint8_t i = 0; i < s.fieldCount; ++i) {
    const FieldSchema &f = s.fields[i];
    if (f.addr == nullptr && f.offset == 0) continue;
    char *addr = nullptr;
    if (f.addr != nullptr) addr = (char*)f.addr;
    else addr = ((char*)base) + f.offset;
    if (!addr) continue;
    if (strcmp(f.type, "number") == 0) {
      double val = *((double*)addr);
      st[f.name] = val;
    } else if (strcmp(f.type, "boolean") == 0) {
      bool b = *((bool*)addr);
      st[f.name] = b;
    } else if (strcmp(f.type, "string") == 0) {
      String *sp = (String*)addr;
      st[f.name] = *sp;
    }
  }

  // emit update delta
  // Only send update messages if there are subscribers for this object.
  if (!subscribers.count(name)) {
    // no subscribers: update internal state but skip emitting update
    return;
  }

  // respect schema-level subscribable hint
  if (dom_schema_exists(name)) {
    const ObjSchema &ss = *dom_get_schema(name);
    if (!ss.subscribable) return;
  }

  DynamicJsonDocument &up = dom_scratch_doc();
  up["type"] = "update";
  up["path"] = name;
  JsonObject ch = up.createNestedObject("changes");
  for (uint8_t i = 0; i < s.fieldCount; ++i) {
    const FieldSchema &f = s.fields[i];
    if (st.containsKey(f.name)) ch[f.name] = st[f.name];
  }
  sendJson(up);
}
