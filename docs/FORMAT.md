# Binary FLA (Flash 5 through CS6) — Complete format notes

Comprehensive, continuously-updated reference for the binary Adobe Flash
authoring document (`.fla`) format — the OLE2 compound-document variant
used by Flash 5 / MX / MX 2004 / 8 / CS3 / CS4 / CS5 / CS6 (not the
XFL zip/xml format also available from CS5+).

This is the product of reverse engineering Flash 8's `flash.exe` with
Ghidra + olefile + capstone, resulting in a working Python decoder
(`fla_decoder/decoder.py`) that achieves **100% byte consumption**
across a test corpus of 17 FLAs / 166 symbols spanning Flash 5
through CS6 (2,705,725/2,705,729 bytes — 4 bytes OLE2 padding).

> Most of the format was **undocumented anywhere public** — even JPEXS
> Free Flash Decompiler cannot read binary FLA. The format below was
> reverse-engineered from the Flash 8 authoring tool directly.

---

## 1. Container

The file is a **Microsoft Compound File Binary / OLE2** (same as `.doc`,
`.xls`, `.msi`). Magic: `D0 CF 11 E0 A1 B1 1A E1`. Python access via
`olefile`; every stream inside is addressable by path.

Typical streams:

| stream    | role |
|-----------|------|
| `Contents` | doc-level DOM: symbol library table, sound entries, publish settings |
| `Page 1..N` | per-scene stage state (same MFC format as Symbol streams) |
| `Symbol N`  | one library item per stream (movie clip / graphic / shape / button / text) |
| `Media N`   | raw asset bytes: audio PCM/MP3, JPEG, PNG, lossless bitmaps |

### Contents stream: symbol library table

The `Contents` stream contains a library item record for each symbol.
Each record includes a `"Symbol N"` MFC CString (u8 charlen + UTF-16LE)
followed by a `ff fe ff` Flash string with the library name. The symbol
number maps directly to the `Symbol N` OLE stream.

Extractable via `scripts/extract_library.py`.

---

## 2. MFC serialization protocol (class tags)

Every stream is a sequence of serialized MFC `CObject` instances. The wire
protocol is Microsoft's `CArchive` tagged-class format (reverse-engineered
by decompiling `CArchive::ReadObject` @ `0x00ee3e6c` and `WriteObject` @
`0x00ee3dd3` in `flash.exe`).

### Class tags (little-endian u16)

| tag value           | meaning |
|---------------------|---------|
| `0x0000`            | null object pointer — also marks end of children list |
| `0xFFFF`            | **new class definition** follows: `u16 schema`, `u16 nameLen`, `nameLen` ASCII bytes |
| `0x8000 \| idx`     | back-reference to previously-declared class `idx` (1-based) |
| `0x7FFF` + `u32 idx`| extended back-ref with 32-bit index (large streams only) |

After the class tag (new or back-ref), the class's own `Serialize(CArchive&)`
writes/reads its fields.

### Class indexing

Each `Symbol` stream maintains its own class-table. The first time a class
is written, the full `0xFFFF ... name` is emitted and the class receives
index 1. Subsequent occurrences of that class use the compact `(idx | 0x8000)`
back-reference.

Pattern example: `0xFFFF 0001 0009 "CPicShape"` declares CPicShape; later
`04 80` (LE) = `0x8004` back-refs class 4 to create another instance.

### Length-prefixed strings (`FF FE FF <len> <chars>`)

Used for UTF-16LE `CString` fields inside records. Three-byte BOM `FF FE FF`
then a `u8` char count then `len × 2` UTF-16LE bytes. Max length is 255
chars (single-byte length field).

### The root object

A stream starts with a one-byte `0x01` root-object header, then a class tag
(always `0xFFFF` for the first class). After that the root object's
`Serialize` runs.

### Serialize virtual slot

For every MFC class, `Serialize` is at **primary vtable slot 2** (byte
offset +8). `CArchive::ReadObject` dispatches via
`(**(code **)(*(int *)pObj + 8))(ar)`.

---

## 3. Class hierarchy (from Flash 8 `flash.exe`)

All 20 CPic* classes plus supporting CMedia* / CMorph* / CColorDef.
Their `CRuntimeClass` descriptors live in the binary's `.data` section; our
`research/data/runtime_classes.json` has the full list with sizes, schemas,
base pointers, and CreateObject function VAs.

```
CObject
├── CPicObj                (116 B, schema 1)   — base of all "stage" objects
│   ├── CPicPage           (212 B)             — a scene/page
│   ├── CPicLayer          (176 B)             — a timeline layer
│   ├── CPicBitmap         (152 B)
│   ├── CPicText           (320 B)
│   ├── CPicSoundCreator   (124 B)
│   ├── CPicSwf            (308 B)
│   ├── CPicOle            (164 B)
│   ├── CPicFont           (120 B)
│   ├── CPicVideo          (160 B)
│   ├── CPicVideoStream    (196 B)
│   ├── CPicTempClipboardObj (140 B)
│   ├── CPicShape          (300 B, schema 1)
│   │   └── CPicFrame      (672 B)             — timeline frame (inherits from shape!)
│   └── CPicSymbol         (244 B)
│       ├── CPicShapeObj   (244 B)             — library-item shape wrapper
│       ├── CPicSprite     (408 B)             — movie clip
│       │   └── CPicScreen (440 B)             — MX2004+ "Screens" feature
│       └── CPicButton     (548 B)
├── CPicMorphShape         (156 B)             — shape tween
├── CMediaElem             (232 B)
│   ├── CMediaBits         (324 B)             — bitmap data wrapper
│   ├── CMediaSound        (308 B)
│   ├── CMediaVideo        (252 B)
│   └── CMediaVideoStream  (384 B)
├── CDocumentPage          (600 B)
├── CColorDef              (128 B)
├── CQTAudioSettings
├── CMorphCurve            (40 B)
├── CMorphSegment          (52 B)
└── CMorphHintItem         (32 B)
```

CPicFrame inheriting from CPicShape is unusual but intentional — every frame
has its own "drawable shape" as the underlying canvas.

### MFI Importer SDK classes

`flash.exe` also contains 79 `MFI*` classes (Macromedia Flash Importer SDK),
e.g. `MFIShapeModule`, `MFIFillStyle`, `MFIContourShape`, `MFICubic`,
`MFIShapeEdgePath`. These are Flash's **public plugin importer API**
(never released to the public in full form). Their field structure mirrors
the internal CPic* hierarchy and was useful as confirmation of the format.

---

## 4. Concrete on-wire layouts

### `CPicObj::Serialize` (base of most CPic* classes)

```
u8   schema                  — CPicObj version
u8   flags                   — packed bit field
──── children list ────
loop:
    class_tag := ar.read_class_tag()
    if tag == NULL: break
    child := dispatch_serialize_for(class_of_tag, ar)
    append child to linked list at this+0x14
end loop
if schema >= 1: 2 × s32 point        — registration/origin (often INT_MIN sentinel)
if schema >= 3: u8  extra1_flags
if schema >= 4: u8  extra2_flag
```

### `CPicShape::Serialize`

```
CPicObj::Serialize(ar)            — base fields first (including children)
u8  shape_schema                   — different from shape_data_schema below
6 × u32  matrix    (see §5)
shape_data (see §6)
```

### `CPicFrame::Serialize` (loading path at 0x8fe3fa)

```
CPicShape::Serialize(ar)           — includes base CPicObj too, so full inherited chain
u8   frame_schema
u16  field_18c
if frame_schema > 2:  u16 → field_188
else:                  u8  → field_188
if frame_schema > 1:  s16 → field_190
if frame_schema > 4:  u16 sound_ref (NOT u32 — confirmed via Ghidra + empirical)
if frame_schema > 5:  u16 count + count × (u32 + u16 + u16)
if frame_schema > 6:  u16 + u8 + u32 + s32 (field_238/23c/240/244)
if frame_schema > 7:  u16 → field_248
if frame_schema > 8:  CString field_250 (threshold=23)
  Schema branch:
    >= 19: FUN_8facd0 (timeline: u32 type_id + u32 fmt + u32 init +
           u32 count + count × u32 char_ids + CString label)
    10-18: FUN_8fd980 (u32 + variable data)
    4-9:   FUN_8faad0 + FUN_8f9570 (jump table)
if frame_schema > 10: u32 field_258 + u32 field_25c
if frame_schema > 11: u32 field_254 (clamped ≤ 1)
if frame_schema > 12: ReadObject CPicMorphShape
if frame_schema > 13: u32 field_1e4
if frame_schema > 14: ReadObject CObList
if frame_schema > 15: CString field_298 (threshold=23)
if frame_schema > 19: u32 field_294
if frame_schema > 20: u32 field_24c
if frame_schema >= 22: u32 field_264
if frame_schema >= 24: u32 field_194 + u32 field_198 (bool)
```

Tested with schemas: 0, 1, 2, 3, 7, 13, 18, 24, 26, 29, 32, 46,
114, 128, 174, 202, 243, 252, 255 — all parse with 100% consumption.

### `CPicPage::Serialize` (loading path at 0x905dde)

```
CPicObj::Serialize(ar)
u8  page_schema
if page_schema != 4: u16 field_78
if page_schema >= 5: u16 field_7c
if page_schema <  2: FUN_903e30 (processing only, no archive reads)
if page_schema >= 7: u32 field_b4
if page_schema >= 3: u32 count + count × (u32, u32) — field_84 array (FUN_a47720)
```

### `CPicLayer::Serialize` (loading path at 0xf3e8cf)

```
CPicObj::Serialize(ar)
u8   layer_schema
CString layer_name                   — FUN_f34c30 ALWAYS reads (skip path via 0xaee770)
if layer_schema <= 3: u8 field_type
if layer_schema >= 4: u8 type + u8 locked + u8 visible
if layer_schema >= 5: u32 color (ARGB)
if layer_schema >= 6: u32 field_8c + u32 field_90
if layer_schema >= 8: u32 field_98
u8   layer_mode                      — unconditional
ReadObject parent_ref                — unconditional (u16 null tag if no parent)
if 7 <= layer_schema < 9: ReadObject
if 2 <= layer_schema < 6: u8
if 3 <= layer_schema < 9: u8
if layer_schema >= 9: u8
if layer_schema >= 10: u8
```

### `CPicText::Serialize` (loading path at 0x929cf4)

Inheritance: CPicText : CPicObj (NOT CPicShape — no shape body).

```
CPicObj::Serialize(ar)
u8   text_schema
24B  matrix                          → 0xf2c400
16B  bounds (4 × s32 twips)          → 0xf2c760
u8   field_c8
if text_schema >= 3: u8 (discarded)
if text_schema >= 5: u32 field_120 (else u16 if == 4)
if text_schema >= 4: u16 field_124
if text_schema >= 4: CString field_128   (via FUN_920900, threshold=10)
if text_schema >= 4 and field_121 & 0x20: CString field_12c
if multiline (field_121 & 0x40): text_run (FUN_91d310)
text_body (FUN_9295c0):
    u16 text_length
    text_run (FUN_91d310):
        u8 run_schema + u16 char_count + CColorDef font_name +
        u32 color + u8 bold + u8 italic + conditional fields
    text_length × 2 bytes UTF-16LE text data
if text_schema >= 6:  CString field_134  (via FUN_920900, threshold=10)
if text_schema >= 9:  FUN_937590 sub-object at field_74
if text_schema >= 8:  u32 field_10c (clamped 0/1)
if text_schema >= 11: CString field_138
if text_schema >= 12: CString field_130
if text_schema >= 13: u8 filter_flag + optional filter + u16 field_64
```

**CColorDef (FUN_91d230):** NOT the same as MFC CString. Reads
u8 count + count×2 bytes UTF-16LE (schema >= 10) or count bytes
ASCII (schema < 10). Used for font names in text runs.

### `CPicBitmap::Serialize` (loading path at 0x8e8810)

Inheritance: CPicBitmap : CPicObj.

```
CPicObj::Serialize(ar)
u8   bitmap_schema
24B  matrix at this+0x78
u16  media_id (or ReadObject via sound manager at runtime)
if bitmap_schema >= 2: u8 filter_flag
    if filter_flag != 0: filter Serialize (0x84e1e0)
```

### `CPicSymbol::Serialize` (loading path at 0x91719c)

Inheritance: CPicSymbol : CPicObj.

```
CPicObj::Serialize(ar)
u8   symbol_schema
24B  matrix at this+0x78             → 0xf2c400
u16  field_b0
u16  field_cc (if symbol_schema > 1)
FUN_009024f0 → field_90 struct      — u8 skip + 4 × u16
FUN_00916540 → CString name         (threshold at [0x12b9718]=13)
u32  media_ref                       (via 0x4c9350)
if symbol_schema >= 11: u8 flag + CStrings + frame data
```

### `CPicSprite::Serialize` (VA 0x00913d80)

Inheritance: CPicSprite : CPicSymbol : CPicObj.

```
CPicSymbol::Serialize(ar)
u8   sprite_schema
if sprite_schema >= 2: FUN_8facd0 (timeline sub-object)
FUN_913bc0 → CString               (threshold=7)
if sprite_schema >= 3: FUN_5c5b00
if sprite_schema >= schema_level_6: FUN_937590
if sprite_schema >= 5: u32 → field_190
if sprite_schema >= 8: FUN_5d4790
```

### `CMediaSound::Serialize` (fully decoded)

Referenced from `Contents` stream. The data record layout we decoded for
every sound in a FLA:

```
<u16 "Media N" ascii prefix>  — raw UTF-16LE label (no length prefix)
<u16str filename>             — e.g. "pickUp0.wav"
<u8 mediaIdByte>              — usually = the numeric Media index
<u16str importPath>           — e.g. ".\Sound\pickUp0.wav"
<u32 tstamp1> <u32 val> <u32 val>
<u32 tstamp2> <u32 val> <u32 val>
<u32 tstamp3> <u32 val> <u32 val>
<u8 = 0x07> <u8 hasLinkage> 00 00 00
[<u16str linkage>]           — present iff hasLinkage == 1
... filler, then:
<u8 = 0x0a> <u8 rateTag> 00 <u32 sampleCount>
```

`rateTag` map: `0x0a → 22050 Hz`, `0x0e → 44100 Hz`, `0x0f → 44100 Hz stereo`.
The samples live in the separate `Media N` stream (raw 16-bit PCM, or
MP3 frames when imported as MP3).

---

## 5. Matrix (6 × u32)

Flash's 2-D affine, stored LE:

```
u32 a   — 16.16 fixed-point (1.0 == 0x00010000)
u32 b   — 16.16 fixed-point
u32 c   — 16.16 fixed-point
u32 d   — 16.16 fixed-point
u32 tx  — integer twips (not fixed-point!) → ÷20 for pixels
u32 ty  — integer twips
```

Applied as `(x', y') = (a·x + c·y + tx, b·x + d·y + ty)`. The `a/b/c/d`
quartet is standard 2×2 scale-rotate; `tx/ty` are pure translation.

The peculiar split of units (16.16 FP for rotation/scale, integer twips for
translation) is a legacy of Flash's internal rendering pipeline.

---

## 6. Shape data (`FUN_00f3da60` @ `0x00f3da60`)

The geometry block inside a CPicShape. This was the **single hardest part**
to reverse.

### Header

```
u8   shape_data_schema       — (0, 1, 2, ..., 5 observed; 5 is modern)
u32  edge_count_hint         — approximate; informational only
u16  fill_style_count
```

### Fill styles (×fill_style_count)

```
if shape_data_schema < 3:    // legacy solids
    u32  color
    u16  flags
else:                         // modern style reader (FUN_00f3c430)
    u32  color                                  — ARGB or RGBA packed
    u8   subtype_flags
    u8   more_flags
    switch subtype_flags (mask 0x70):
        no bits set        →  SOLID (no additional bytes)
        bit 0x10           →  GRADIENT:
                              matrix (24 B)
                              u8   num_stops (cap 15)
                              if caps_flag: u16 grad_hints; u8 grad_type
                              stops × num_stops: u8 position + u32 color
        bit 0x20           →  type-0x20 (unknown):
                              matrix + u32 id + 4 × u16
        bit 0x40           →  BITMAP:
                              matrix + u32 bitmap_id
```

> **caps_flag** = `CPicShape.shape_schema > 2` — **not** `shape_data_schema`.
> Getting this wrong mis-aligns the gradient-extras reads and cascades
> into garbage coordinates. This was our worst bug.

### Line styles

```
u16  line_style_count
per-style:
    u32  stroke_color            — ARGB (overwrites fill.color at end)
    u16  flags
    inline_fill    (4 B, bit-packed, FUN_00f3c8c0 — see below)
    if caps_flag:
        u8 start_cap  u8 end_cap  u8 joins  u8 reserved
        u16 miter_limit
        full fill_style (variable, same reader as above)
```

### Inline compact fill (`FUN_00f3c8c0`, 4 B)

Highly packed 4-byte color encoding used inside line styles for space saving:

```
s16  sv
u16  uv
flags_bit = (uv >> 14) & 2
if sv == 0:
    b = uv & 0xff
    subtype = b & 7
    switch subtype:
        case 2: u16 field_a = uv >> 3
        case 3: field_a = (b>>3)&7, field_b = (b>>6)&3, field_c = (uv>>8)&3
        case 4: field_a = (b>>3)&3, field_b = (b>>5)&3, field_c = (uv & 0x180) >> 7
        case 5: six bit-packed fields from uv
else:
    subtype = 1 (simple color) with x = sv, y = uv & 0x7fff
```

### Edge stream

The actual path geometry. Runs for `shape_data_schema >= 2`:

```
loop:
    u8 edge_flags
    if edge_flags == 0: break      // terminator
    if edge_flags & 0x40:
        if edge_flags & 0x80: read 3 × u8  style_change values
        else:                 read 3 × u16 style_change values
        (interpret as: fill0_idx, fill1_idx, line_idx — top bit
         may indicate "unchanged"; indices are 1-based)
    delta1 = read_coord_delta(type = edge_flags      & 3)    // "move" from prev
    delta2 = read_coord_delta(type = (edge_flags>>2) & 3)    // control offset
    delta3 = read_coord_delta(type = (edge_flags>>4) & 3)    // to offset

    from = prev_to + delta1                // starts at (0,0) for first edge
    ctrl = from    + delta2
    to   = from    + delta3
    prev_to = to
    if (edge_flags & 0x0c) == 0:
        emit straight edge (store midpoint(from,to) as implicit control)
    else:
        emit curved edge (quadratic Bezier from,ctrl,to)
    // optionally persist (fill0, fill1, line_style) with this edge
end loop

if shape_data_schema > 4:
    s32 cubic_count
    per cubic: 4 × (s32 x, s32 y)             // 32 bytes per cubic Bezier anchor set
```

### Coordinate delta types (`FUN_00f3c150`)

Two bits per delta from `edge_flags` select the encoding:

```
type 0 (0 bytes):   (dx,dy) = (0, 0)
type 1 (4 bytes):   (dx,dy) = (s16, s16)            — fine precision, ±12.8 px
type 2 (8 bytes):   (dx,dy) = (s32, s32)            — full range
type 3 (4 bytes):   (dx,dy) = (s16<<7, s16<<7)      — coarse precision, wider range
```

All values accumulate into Flash's internal "ultra-twip" coordinate system:

**1 px = 2560 ultra-twips  (= 20 twips × 128)**

Divide decoded coords by 2560 to get SVG pixels.

### Edge builders (post-delta)

```
straight edge (FUN_00f26d00):
    out.p0 = from
    out.p1 = midpoint(from, to)        // synthetic control = midpoint
    out.p2 = to
    out.type_marker = 1

curved edge (FUN_00f26cc0):
    out.p0 = from
    out.p1 = ctrl
    out.p2 = to
    out.type_marker = 0
```

All edges are stored as **quadratic Beziers** internally; straight edges
get a synthetic midpoint control so the downstream renderer only has one
path type to handle. When emitting SVG we check `type_marker` and render
straight edges as `L` commands, curves as `Q` commands.

---

## 7. Lossless bitmap format (`Media N` streams)

Used when a bitmap is imported with "Lossless (PNG/GIF)" compression.
Ported from JPEXS's `LosslessImageBinDataReader.java`.

```
u8   = 0x03                   magic byte 1
u8   = 0x05                   magic byte 2
u16  rowSize
u16  width
u16  height
u32  frameLeft                (twips)
u32  frameRight
u32  frameTop
u32  frameBottom
u8   flags                    bit 0 = hasAlpha
u8   variant                  1 = chunked-zlib
loop:
    u16 chunkLen
    if chunkLen == 0: break
    chunkLen bytes            — concatenate into one zlib stream
```

After zlib-inflating the concatenated stream you get raw pixels in order:

```
for each y:
    for each x:
        u8 A, u8 B, u8 G, u8 R
```

Alpha is "1-based premultiplied":
- `A == 0 || A == 255`: literal.
- `0 < A < 255`: subtract 1, then scale RGB by `256 / A_new` to un-premultiply.

### Example recovered: `openScreen.fla` Media 1
500 × 176 RGBA PNG — the blue-pumpkins title-screen artwork. Confirmed by
direct visual inspection.

---

## 8. Audio format (`Media N` streams, referenced from `CMediaSound`)

Two cases:

1. **MP3 source** — filename ends `.mp3`. Stream bytes are raw MP3 frames;
   write to disk as `.mp3`.
2. **WAV source** — filename ends `.wav`. Stream bytes are raw 16-bit PCM
   samples. Metadata from the CMediaSound record tells us:
   - sample rate (from `rateTag`: 0x0a→22050, 0x0e→44100, 0x0f→44100 stereo)
   - sample count (u32 from the `0a XX 00 <u32>` block)
   - derived: channels = `stream_size / (2 × sample_count)` (1 or 2)
   - bit depth: always 16-bit in this project's FLAs

Wrap in a standard WAV header (`wave.open(...)`) and the file plays.

---

## 9. Specific constants observed

| constant             | meaning |
|----------------------|---------|
| `0x80000000` (INT_MIN) | "uninitialized bounds/point" sentinel — appears in many CPicObj/CPicShape 2-u32 point fields |
| `1 px = 2560` ultra-twips | shape coordinate unit (see §6) |
| `1 px = 20 twips`   | matrix translation unit (see §5), audio subsample units, bounds |
| `0x00010000` (= 1.0 in 16.16 FP) | identity matrix scale element |

---

## 10. Known limitations / open items

Things we have NOT fully decoded (project never needed them):

- **CPicFrame trailing fields** past the inherited shape data
  (timeline control, frame actions, labels) — partially decompiled,
  not yet in the Python decoder; tolerant EOF handling lets the
  decoder bail gracefully past this point
- **CPicSprite-specific fields** — children go through the CPicObj
  loop, but per-sprite metadata isn't decoded
- **CPicText layout** — multiple-inheritance thunks in the vtable make
  this thornier to reach from Python; for now we extract text strings
  directly with a regex over the stream bytes
- **CPicMorphShape** — only 1 symbol in this project uses it; contents
  known to include `CMorphSegment` and `CMorphCurve` children
- **Full field-by-field parse for CPicPage / CPicLayer tails**
- **Meaning of the `inline_fill` packed subtypes 2/3/4/5** —
  we read them but don't currently render them semantically
- **Stroke width units** — the `flags16` field's exact interpretation
  is still heuristic (our scale factor 0.05 is empirical)

---

## 11. Reference: our decoder implementation

File                                  | purpose
--------------------------------------|---------------------------------------
`fla_decoder/decoder.py`              | Pure-Python decoder (olefile + struct)
`fla_decoder/to_svg.py`               | SVG emitter with gradient + matrix-transform support
`fla_decoder/audio.py`                | audio extractor using §8 layout
`fla_decoder/lossless.py`             | lossless-bitmap extractor using §7 layout
`fla_decoder/bitmaps.py`              | raw + DefineBitsLossless bitmap extractor
`scripts/decode.py`                   | batch driver across all FLAs in a directory
`scripts/audit.py`                    | list unrendered symbols + their text/script content
`scripts/inspect_symbols.py`           | per-symbol class tree / strings / bounds inventory
`scripts/extract_library.py`          | symbol library table + timeline extraction
`scripts/extract_media.py`            | one-shot CLI for audio + bitmaps + lossless
`docs/RE_JOURNEY.md`                  | RE journey log (where each function was found)
`research/find_class_refs.py`         | PE parser to locate CRuntimeClass structs
`research/decode_runtime_classes.py`  | scan .data for class descriptors
`research/find_serialize.py`          | locate Serialize VAs via vtable slot 2 (primary)
`research/data/runtime_classes.json`  | all 46 CRuntimeClass entries from flash.exe
`research/data/serialize_vas.json`    | Serialize function VAs per class

## 11.5 Recovery scanner

`CPicFrame` has dozens of schema-dependent tail fields for
`frame_schema >= 9` including variable-length helpers that the
structured parser can't fully decode. Two recovery strategies:

1. **Class-declaration recovery**: finds `FFFF 01 00 09 00 CPicShape`
   class-tag patterns and parses the shape body that follows.

2. **Signature recovery**: scans for the 10-byte CPicShape header
   tail (`00 00  00 00 00 80  00 00 00 80` = NULL child tag + two
   INT_MIN point sentinels). Tries parsing at offsets -2, -1, 0, -3.
   Accepts candidates with ≥ 3 edges, plausible schema (≤ 8), and
   coordinates within ±50k px.

**Taken-region tracking**: each recovered shape marks a byte region
to prevent duplicate recovery. Region size is capped to
`n_edges × 12 + 1000` bytes (based on observed ~8-12 bytes per edge)
to prevent one inflated shape from blocking nearby shapes.

**End-marker scan**: child classes (CPicText, CPicSprite, CPicButton)
that can't precisely consume their bytes scan forward for the pattern
`00 00  00 00 00 80  00 00 00 80` followed by a valid parent schema
byte, to reposition the reader for the parent's continuation.

**Results**: 100% shape coverage (31,168 shapes / 16.3M edges, 0
missed across 9 FLAs). 806/841 symbols render to SVG (96%). The
remaining 35 symbols are genuinely shape-less composition containers
(33 CPicSprite + 2 empty CPicFrame).

The scan's main false-positive guard is checking that the byte right
before our signature is a plausible schema byte (≤ 8) and the next is a
plausible flags byte (≤ 0x40).

## 12. Where this stands as public knowledge

At time of writing (April 2026), this document represents the **most
complete public description** of the Flash 8 binary FLA format. The
JPEXS project explicitly lists binary FLA import as "no one knows the
meaning of every byte" — this note and the accompanying decoder
materially narrow that gap.

Areas still undocumented beyond this note:
- Full per-byte semantics of CPicFrame timeline control
- Bit layout of CPicText's font/format records
- Some edge-case schemas (legacy pre-Flash-8 formats)
