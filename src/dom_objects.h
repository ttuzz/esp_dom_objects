#ifndef DOM_OBJECTS_H
#define DOM_OBJECTS_H

/*
How to add a new global typed object (struct) for the dom_objects runtime

1) Add the typed struct definition to this header (dom_objects.h):
   e.g.:
     typedef struct {
       bool enabled;
       double power;
       String mode;
     } my_obj;

2) Add an extern instance declaration in this header:
     extern my_obj my_instance;

3) In dom_objects_data.cpp define the instance (one translation unit):
     my_obj my_instance = { false, 0.0, String("") };

4) Add the FieldSchema entries that reference the instance fields by
   absolute address (addr) in dom_objects_data.cpp. Example:
     static const FieldSchema myFields[] = {
       { "enabled", "boolean", 0, &my_instance.enabled },
       { "power",   "number",  0, &my_instance.power   },
       { "mode",    "string",  0, &my_instance.mode    }
     };

5) Define an ObjSchema for the object in dom_objects_data.cpp and register it
   with the runtime during dom_init() or by calling dom_register_schema():
     const ObjSchema mySchema = { "my", myFields, sizeof(myFields)/sizeof(myFields[0]) };
     dom_register_schema(mySchema);

6) Register the typed instance so the runtime can locate the base pointer:
     dom_register_typed_object(String("my"), &my_instance);
   (do this in dom_register_builtins() in dom_objects_data.cpp)

7) Usage: write directly to the typed struct fields in your code and then call
     dom_push_struct_to_json("my");
   to publish changes, or update via dom_set_field_number() to mutate JSON first.

Notes:
- The instance must be defined before any FieldSchema that uses its address.
- The runtime prefers FieldSchema.addr (absolute address). Offsets are supported
  as a fallback but addr is recommended for direct MCU usage.
- If you allocate instances dynamically, set FieldSchema.addr at runtime after
  allocation instead of using static address initializers.
- Keep concrete definitions (instance and ObjSchema) in a single .cpp to avoid
  multiple-definition linker errors; put only 'extern' declarations in headers.

Turkce (ASCII only):

Yeni bir global tipli nesne (struct) eklemek icin takip edilecek adimlar:

1) Tip tanimini bu headera ekle (dom_objects.h). Ornegin:
     typedef struct {
       bool enabled;
       double power;
       String mode;
     } my_obj;

2) Bu header icine extern bildirimi ekle:
     extern my_obj my_instance;

3) dom_objects_data.cpp icinde instance'i tanimla ve baslangic degerlerini ver:
     my_obj my_instance = { false, 0.0, String("") };

4) FieldSchema dizisine, alanlar icin instance adreslerini ekle:
     static const FieldSchema myFields[] = {
       { "enabled", "boolean", 0, &my_instance.enabled },
       { "power",   "number",  0, &my_instance.power   },
       { "mode",    "string",  0, &my_instance.mode    }
     };

5) ObjSchema tanimini yap ve runtime'a kaydet (dom_init veya dom_register_schema ile):
  const ObjSchema mySchema = { "my", myFields, sizeof(myFields)/sizeof(myFields[0]) };
  dom_register_schema(mySchema);

6) Typed instance'i kaydet:
  dom_register_typed_object(String("my"), &my_instance);
   (bu islemi dom_register_builtins() icinde yap)

7) Kullanim: Kod icinde struct alanlarini dogrudan guncelle ve yayinlamak icin
  dom_push_struct_to_json("my") cagri yap; veya JSON uzerinden mutate etmek
  istersen dom_set_field_number() kullan.

Notlar:
- Instance, FieldSchema icinde adresi kullanilan alanlardan once tanimlanmali.
- Runtime addr (mutlak adres) kullanimini tercih eder; offset fallback olarak
  desteklenir ama addr tavsiye edilir.
- Dinamik allocation kullaniliyorsa FieldSchema.addr runtime'da ayarlanmalidir.
- Tanimlar ve iliskili ObjSchema/bildirimler tek bir .cpp icinde tutulmali; header
  sadece extern bildirimleri icerir.

*/


#include <Arduino.h>
#include <map>

struct FieldSchema {
  const char *name;
  const char *type; // e.g. "boolean", "number", "string"
  // byte offset into a typed struct instance (0 if not used)
  size_t offset;
  // optional direct pointer to the field inside a typed instance
  // if non-null, code will use this absolute address instead of offset.
  void *addr;
};

struct ObjSchema {
  const char *objName;
  const FieldSchema *fields;
  uint8_t fieldCount;
  // runtime/static hints
  boolean subscribable; // whether clients may subscribe to this object
  boolean readOnly;     // if true, 'set' operations are rejected
  boolean discoverable;   // whether object appears in discovery listings
};



// Initialize builtin objects and state
void dom_init();

// Register an ObjSchema with the runtime. This is a safe way for
// other translation units (e.g. dom_objects_data.cpp) to add schemas
// without accessing internal static maps.
void dom_register_schema(const ObjSchema &s);

// Process a single incoming JSON line (from serial)
void dom_process_line(const String &line);

// Periodic tick; kept for compatibility (no-op)
void dom_tick();

// Randomized schema-driven updater.
void dom_randomize_tick();

// Helper: set a numeric field on an object and emit an 'update' message.
void dom_set_field_number(const String &path, const char *field, double value);

// Register a typed struct instance for an object so sync helpers can
// read/write the struct directly using field offsets.
void dom_register_typed_object(const String &name, void *ptr);

// Push all fields from a registered typed struct into the JSON runtime
// and emit an 'update' message for that object.
void dom_push_struct_to_json(const String &name);

// Schema registry is provided by dom_schema.{h,cpp}. Use dom_register_schema()
// or dom_get_schema() to access schema metadata.

#endif // DOM_OBJECTS_H
