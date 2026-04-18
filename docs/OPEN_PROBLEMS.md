# Open problems

## Current state

The decoder parses **100% of bytes** across a 17-FLA / 166-symbol test
corpus (4 bytes OLE2 padding excluded). It renders shapes to SVG and
extracts all recoverable metadata from binary FLAs spanning Flash 5
through CS6.

**What's fully extracted:**
- All vector shapes (fills, strokes, gradients, transforms)
- Audio (WAV/MP3 from Media streams)
- Bitmaps (JPEG/PNG/lossless) + CPicBitmap metadata (schema, matrix, media_id)
- Symbol library table (names, types, timestamps from Contents stream DOM)
- Timeline keyframes with labels, char_ids, instance names
- Timeline composition (FUN_8facd0: type_id, format_type, char_ids, label)
- All AS2 scripts (frame scripts, onClipEvent handlers)
- Text: font name (CColorDef), font color, bold/italic, text content (text runs)
- Layer metadata (name, type, lock, visible, outline color, mode, parent ref)
- Shape tweens (CPicMorphShape with morph coordinates)
- Background color + frame rate
- Publish settings (130+ key-value pairs per FLA)
- Library folder hierarchy
- CS4 IK bones (BridgeTree/ikTreeStates XML)
- CS4 motion tweens (AnimationCore XML)
- Stage dimensions
- CPicFrame full tail (schema >= 19): 12 post-timeline fields
- CPicLayer: all conditional fields (mode, parent ReadObject, schema 2-10 u8s)
- CPicPage: field_b4 + field_84 array (2 × u32 per entry)

**Byte consumption: 100.0%** (2,705,725/2,705,729 — 4 bytes OLE2 padding
across 17 FLAs / 166 symbols, including Digital Classroom lesson files)

**Frame schemas tested:** 0, 1, 2, 3, 7, 13, 18, 24, 26, 29, 32, 46,
114, 128, 174, 202, 243, 252, 255 — all parse correctly.

**What remains partially decoded:**
- Per-frame placement data (transform matrix, depth, blend mode)
- char_id → symbol mapping (runtime-computed, resolvable by naming)
- CS4 3D transforms (no test file found with Rotation_X/Y/Translation_Z)

---

## What you need to continue RE

1. **A copy of `flash.exe`** from Flash Professional 8 (16.8 MB Win32
   PE from archive.org). See `research/README.md` for sourcing.
2. **Ghidra 12.x** with the binary loaded and auto-analyzed.
3. **Sample `.fla` files** in `tests/fixtures/` (gitignored).

---

## Workflow that worked

1. Find the class's **primary vtable** via its constructor (look for
   `mov [esi], imm32` vtable writes). The primary vtable is at `[esi]`
   (offset 0), not `[edi]` or other offsets.
2. **Slot 2** of the primary vtable is the real Serialize. (Slot 4
   is `ret 8` / no-op for all CPicObj-derived classes.)
3. Find the **loading path** by following the `je` from the save/load
   mode check (`not eax; test al, 1; je loading_path`).
4. Trace the archive reads: `0x407a10` = u8, `0x4694f0` = u16,
   `0x407a60` / `0x40a820` = u32, `0x4710e0` = CString,
   `0xf2c400` = matrix (24B), `0xf2c760` = rect (16B).
5. Implement in `fla_decoder/decoder.py`, test with `scripts/decode.py`.

**End-marker scan technique:** When a child class can't precisely
consume its bytes, scan forward for `00 00  00 00 00 80  00 00 00 80`
(null tag + INT_MIN point). Use the **LAST valid match** (closest to
stream end) because the CPicPage boundary is always outermost. Validate
by checking: byte at marker+12 is a valid page_schema (≤ 15), and if
page_schema >= 3, field_84_count at marker+21 must be < 1000.

---

## Remaining gaps

### 1. CPicFrame schema > 8 tail — SOLVED

Schema >= 19 is fully decoded field-by-field with FUN_8facd0 timeline
parsing and 12 post-timeline fields. Schemas 10-18 use the end-marker
scan fallback. All schemas are tested and parse correctly:
0, 1, 2, 3, 7, 13, 18, 24, 26, 29, 32, 46, 114, 128, 174, 202,
243, 252, 255.

**Key findings:**
- Sound field at schema > 4 is **u16** (not u32)
- FUN_8facd0 (schema >= 19) reads: u32 type_id + u32 format_type +
  u32 init + u32 count + count × u32 char_ids + CString label
- 12 post-timeline fields: f258/f25c (>10), f254 (>11), ReadObject
  morph (>12), f1e4 (>13), ReadObject oblist (>14), CString f298 (>15),
  f294 (>19), f24c (>20), f264 (>=22), f194+f198 (>=24)
- CPicPage: u32 field_b4 (schema >= 7), array field_84 (>= 3, 2×u32)

### 2. Per-frame placement matrix

FUN_8f9570 (loading path at 0x8f9c8c) reads per-frame data:

```
u8   entry_schema
u8   frame_type
3x   FUN_8f9400 → CString (threshold at [0x12b88f4]=10)
u16  field
if entry_schema > 0:   s16
if entry_schema >= 2:  CString (threshold=10)
if entry_schema >= 3:  3x CString + u32
if entry_schema >= 4:  7x u32 (PLACEMENT MATRIX: a,b,c,d,tx,ty,depth)
if entry_schema >= 5:  u32 + 3x CString + more
```

The test corpus has entry_schema 0-2, so the schema >= 4 placement
matrix is not populated. FLAs from newer Flash versions (CS3/CS4)
likely have higher schemas with actual matrix values.

### 3. char_id → symbol mapping

The `char_id` is a direct array index into a global table at
`0x13c2b68`. Registration happens via FUN_494310 (vtable+0x38
returns the char_id). The mapping is runtime-computed and doesn't
appear as an explicit table in the binary.

**Practical workaround:** Frame labels map to library names by naming
convention. The library table is extracted from the Contents stream
DOM via `scripts/extract_library.py`.

### 4. CPicText — SOLVED

Full layout confirmed from Ghidra. Text runs and text body are
structurally parsed.

**CColorDef format (FUN_91d230):** u8 count + count×2 UTF-16LE
(schema >= 10) or count bytes ASCII (schema < 10). NOT the same
as MFC CString format.

**Text run (FUN_91d310, loading at 0x91d830):**
```
u8  run_schema              (from archive, sets CColorDef threshold)
u16 char_count
CColorDef font_name         (u8 len + len×2 UTF-16LE)
u32 font_color              (RGBA)
u8  bold, u8 italic
if run_schema >= 3: u8 align + 3×u8
if run_schema >= 5: CColorDef highlight
if run_schema >= 6: 3×u8
if run_schema >= 7: CColorDef url_color
if run_schema >= 8: u8
u8 field_8d4 + 5×u16 (indent, spacing, margins)
```

**Text body (FUN_9295c0):**
u16 text_length + text_run + text_length×2 bytes UTF-16LE

**Post-body fields (schema >= 6..13):**
CColorDef field_134, FUN_937590 font sub-object (schema >= 9),
u32 field_10c, CColorDef field_138/field_130, filter data.

### 5. CPicMorphShape — SOLVED

Shape tweens are fully decoded. The class is read via
CArchive::ReadObject in CPicFrame schema > 12 (FUN_771700 at
CRuntimeClass 0x12946d8). Morph coordinates are in twips (1/20 pixel),
not ultra-twips. Children include CMorphSegment and CMorphCurve with
start/end coordinate pairs.

### 6. CPicBitmap metadata — MOSTLY SOLVED

Pixel data is fully extracted. Symbol-level metadata now decoded:
bitmap_schema(u8), matrix(24B), media_id(u16), filter_flag(u8).

**Full layout (loading path at 0x8e8810):**
```
u8  bitmap_schema
24B matrix at this+0x78
u16 media_id (or ReadObject via sound manager at runtime)
if schema >= 2:  u8 filter_flag
  if filter_flag != 0:  filter Serialize (0x84e1e0, complex)
```

Missing: linkage name (stored elsewhere in Contents stream),
smoothing flag (may be in filter data), compression quality.

### 7. CPicLayer — SOLVED

All fields decoded from Ghidra (loading path at 0xf3e8cf):
```
u8  layer_schema
CString layer_name          (FUN_f34c30: ALWAYS reads, threshold=11)
if schema <= 3: u8 field_type
if schema >= 4: u8 type + u8 locked + u8 visible
if schema >= 5: u32 color (ARGB)
if schema >= 6: u32 field_8c + u32 field_90
if schema >= 8: u32 field_98
u8  layer_mode               (ALWAYS, unconditional)
ReadObject parent_ref        (ALWAYS, unconditional)
if 7 <= schema < 9: ReadObject
if 2 <= schema < 6: u8
if 3 <= schema < 9: u8
if schema >= 9: u8
if schema >= 10: u8
```

Key finding: FUN_f34c30 has a SKIP path (0xf34d46 via 0xaee770)
that reads a CString even when schema < threshold. The layer name
CString is ALWAYS consumed from the archive.

---

## Things we know NOT to chase

- **OLE2 container parsing.** `olefile` handles it.
- **XFL/zip format.** Post-CS5, handled by `lifeart/fla-viewer`.
- **Writing .fla files.** Read-only is hard enough.
- **AS3 bytecode.** Pre-CS5 uses AS1/AS2 as readable strings.

---

## Useful Ghidra anchors

**IMPORTANT: vtable slot 4 is NOT the real Serialize.** Slot 4 for
CPicObj is `ret 8` (no-op). The actual serialization is at **slot 2
of the PRIMARY vtable**. CPicSprite has multiple inheritance and 5
vtable pointers; `serialize_vas.json` has the secondary vtable
(at `this+0xf4`), not the primary.

### Primary vtable slot 2 (the REAL Serialize for each class)

| Class | Primary vtable | Slot 2 (Serialize) | Notes |
|---|---|---|---|
| `CPicObj` | `0x01085ffc` | `0x00902d70` | Base: schema, flags, children loop, point |
| `CPicShape` | `0x01086954` | `0x00910e40` | CPicObj base + shape_schema + matrix + shape_data |
| `CPicFrame` | `0x01085d4c` | `0x008fdb80` | CPicShape base + frame tail (loading at 0x8fe3fa) |
| `CPicSymbol` | `0x01086d84` | `0x00916800` | CPicObj base + symbol fields (loading at 0x91719c) |
| `CPicSprite` | `0x01086c1c` | `0x00913d80` | CPicSymbol base + sprite sub-objects |
| `CPicText` | `0x0108738c` | `0x00929800` | CPicObj base + text fields (loading at 0x929cf4) |

### CPicSymbol::Serialize call map (loading path at 0x91719c)

```
CPicObj::Serialize(archive)          → 0x00902d70
u8  symbol_schema
24B matrix at this+0x78              → 0x00f2c400 (matrix reader)
u16 field_b0
u16 field_cc (if symbol_schema > 1)
FUN_009024f0(archive, &field_90)     — u8 skip + 4x u16
FUN_00916540 → CString name          (threshold at [0x12b9718]=13)
u32 media_ref                        (via 0x4c9350 or 0x5b1a90)
if symbol_schema >= 11: u8 flag + CStrings + frame data
```

### CPicSprite::Serialize call map (VA 0x00913d80)

```
CPicSymbol::Serialize(archive)       → 0x00916800
u8  sprite_schema                    (read directly from stream)
if sprite_schema >= 2:
    FUN_008facd0(archive, &field_f4)  — timeline data
FUN_00913bc0(archive, sprite_schema, &field_160)  — CString (threshold=7)
if sprite_schema >= 3:
    FUN_005c5b00(archive, &field_164)
if sprite_schema >= schema_level_6:
    FUN_00937590(&field_150, archive, mode)
if sprite_schema >= 5:
    u32 → field_190
if sprite_schema >= 8:
    FUN_005d4790(&field_15c, archive)
```

### Archive read helpers

| VA | Function | Reads |
|---|---|---|
| `0x00407a10` | — | u8 from archive |
| `0x004694f0` | — | u16 from archive |
| `0x00407a60` / `0x0040a820` | — | u32/s32 from archive |
| `0x004710e0` | — | CString (calls 0xee16bc for length prefix) |
| `0x00f2c400` | — | Matrix (24B = 6 × s32) |
| `0x00f2c760` | — | Rect (16B = 4 × s32) |
| `0x008f9400` | — | Conditional CString (threshold at [0x12b88f4]=10) |
| `0x008f9120` | — | Conditional CString (threshold at [0x12b88bc]=23) |
| `0x00920900` | — | Conditional CString (threshold at [0x12bae88]=10) |
| `0x0091d230` | — | CColorDef: u8 count + count×2 UTF-16LE (≥10) or ASCII |
| `0x0091d310` | — | Text run (run_schema + char_count + CColorDef + formatting) |
| `0x009295c0` | — | Text body (u16 length + text_run + UTF-16LE data) |
| `0x00f34c30` | — | Layer CString (ALWAYS reads, skip path via 0xaee770) |
| `0x008e8710` | — | CPicBitmap::Serialize (schema + matrix + media_id + filter) |

### Timeline VAs

| VA | Function | Purpose |
|---|---|---|
| `0x008facd0` | FUN_8facd0 | Timeline sub-object reader (type dispatch) |
| `0x008f9570` | FUN_8f9570 | Per-frame data reader (schema-gated fields) |
| `0x00498020` | FUN_498020 | Per-frame sub-structure (schema + count + entries) |
| `0x00494310` | FUN_494310 | Global table init + char_id registration |
| `0x13c2b68` | — | Global char_id → CPic* lookup table |

### Other anchors

| VA | Notes |
|---|---|
| `0x00ee3e6c` | CArchive::ReadObject — the Rosetta Stone |
| `0x00ee16bc` | CString length prefix reader (u8/u16/u32 + mode) |
| `0x00f3c430` | Fill-style reader; takes `caps_flag = shape_schema > 2` |

`research/data/runtime_classes.json` has all 46 CRuntimeClass entries.
`research/data/serialize_vas.json` has Serialize VAs per class (note:
these are from the SECONDARY vtable for CPicSprite, not the primary).

---

## How to verify progress

```bash
python3 scripts/decode.py path/to/fla_dir/ /tmp/out
python3 scripts/audit.py path/to/fla_dir/
python3 scripts/extract_library.py path/to/fla_dir/ library.json
```

- All 166 symbols across 17 fixture FLAs should decode without errors
- Byte consumption should be 100% (4 bytes OLE2 padding allowed)
- Frame schemas 0-255 should all parse correctly
- No `_frame_tail_unparsed` flags for schema >= 19

---

## CS3/CS4/CS6 support status

The decoder was built from Flash 8's `flash.exe` but successfully handles
CS3, CS4, and CS6 binary FLAs. Flash CS3 (2007) and CS4 (2008) added
features to the binary FLA format; CS5 (2010) introduced the XFL/zip
format but continued to support binary FLA through CS6 (2012).

### Flash CS3 (v9)
- **ActionScript 3.0 + AVM2** — AS3 bytecode in symbol streams
- Files targeting Flash Player 9+ may have different serialization

### Flash CS4 (v10) — mostly solved
- **Inverse Kinematics / Bone Tool** — SOLVED. IK armatures stored as
  embedded XML (`<BridgeTree>`, `<_ikTreeStates>`) in Page/Contents streams.
- **Object-based motion tweening** — SOLVED. Stored as `<AnimationCore>`
  XML with per-property bezier keyframe tracks.
- **3D transforms** — NOT TESTED. No binary FLA with 3D transforms found.
  Would appear as Rotation_X/Y, Translation_Z in AnimationCore XML.
- **Motion Editor curves** — SOLVED (part of AnimationCore XML).

### What's needed
1. **CS4 FLA with 3D transforms** — a binary FLA using the 3D Rotation
   or 3D Translation tools, to verify the Rotation_X/Y/Translation_Z
   extraction path. Tutorial sites from 2008-2009 are mostly dead.
2. **CS4 `Flash.exe`** for Ghidra disassembly of any new CPic classes:
   - https://archive.org/details/adobe-flash-professional-cs-4.7z

### The test corpus
17 FLAs / 166 symbols spanning Flash 5 through CS6: Flash 8 era files,
CS3 motion tweening, CS4 IK bones + motion tweens + metallic buttons,
CS6 motion tweening, plus 4 Digital Classroom lesson files and 4
EduTech Wiki IK/bone FLAs. 100% byte consumption (4 bytes OLE2 padding).
84 FLAs scanned total (including 43 from Digital Classroom ISO) — none
contained CS4 3D rotation/translation data.

### CS4 format findings (verified)

Two CS4 binary FLAs were successfully decoded with the existing parser.
Shape extraction works unchanged — the MFC CPic* class protocol is
identical between Flash 8 and CS4.

**Stream naming difference:** CS4 uses `S N timestamp`, `P N timestamp`,
`M N timestamp` instead of `Symbol N`, `Page N`, `Media N`. The timestamp
is a Unix epoch (seconds since 1970). `scripts/decode.py` handles both.

**IK Bone data (from shape-ik-rubberman-animation.fla):**
Stored as embedded XML strings in Page and Contents streams:
- `<BridgeTree>` — armature definition (name, color, exportType)
- `<nodes>` — bone hierarchy (parent-child tree of 21 nodes)
- `<Matrix a b c d tx ty Name>` — per-bone transform (rotation/position)
- `<_ikTreeStates>` — animation states (10 states, 80 frames)
- `ikNode_N` / `ikBoneName_N` — comma-separated name mappings

**Object-based motion tweens (from flash-cs-4-motion-tweening-adjusted.fla):**
Stored as `<AnimationCore>` XML in Page/Symbol streams:
- TimeScale (fps × 1000), duration (ms), TimeMap (easing type/strength)
- Per-property bezier keyframe tracks:
  - Motion_X, Motion_Y, Rotation_Z
  - Scale_X, Scale_Y, Skew_X, Skew_Y
  - Brightness_Amount, Filters
- Each keyframe: anchor point + next/previous cubic handles + timevalue

**3D transforms:** Not found in test fixtures. Would need a CS4 FLA
that uses the 3D Rotation or 3D Translation tools. The data likely
appears as Rotation_X, Rotation_Y, Translation_Z properties in the
AnimationCore XML, or as additional matrix fields.

### CDocumentPage fields (investigated)

CDocumentPage::Serialize at primary vtable slot 2 (VA `0x008c9af0`,
loading path at `0x008ca190`). Schema value is 23 (Flash 8) or 25 (CS4).

**Confirmed layout:**
```
u8  doc_schema
FUN_8c7550 → CString page_name    (threshold at [0x12b4b78]=21)
FUN_8c9940 → CString scene_name   (conditional CString reader)
if doc_schema >= 2: u32 → field_19c (via 0x4c9350)
if doc_schema >= 3 and < 4: u8 → field_19a (bool)
if doc_schema >= 4: bool → field_19a (via 0x5af0a0)
if doc_schema >= 6: FUN_8c9940 → field_1e0 (CString)
if doc_schema >= 7: u32 → field_1e4 (via 0x40a820)
if doc_schema >= 5: FUN_8781c0 → field_1c (complex)
if doc_schema >= 8: FUN_ecc6b1 → field_21c
if doc_schema == 9: 4x FUN_8fd980 (timeline data)
if doc_schema >= 10: complex extended handling
```

**Note:** Frame rate and background color are NOT in CDocumentPage.
They are stored elsewhere in the Contents stream as a binary pattern:
RGBA(4B) + RGBA(4B) + u16(0) + u16(fps). The decoder finds them via
pattern matching in `extract_all.py`.
