# Open problems

## Current state

The decoder extracts **100% of shapes** (31,168 shapes / 16.3M edges)
and renders **96% of symbols** (806/841) to SVG. The remaining 35
symbols are composition containers (CPicSprite movie clips and empty
CPicFrame scaffolding) that reference shapes from other symbols by
internal character IDs — they contain no inline shape geometry.

**What's fully extracted:**
- All vector shapes (fills, strokes, gradients, transforms)
- Audio (WAV/MP3 from Media streams)
- Bitmaps (JPEG/PNG/lossless)
- Symbol library table (188 names from Contents stream DOM)
- 3,493 timeline keyframes with labels, char_ids, instance names
- All AS2 scripts (frame scripts, onClipEvent handlers)
- Text content, font names, font sizes, bounding rects
- Layer names, page schemas
- Interactive object properties (from onClipEvent scripts)

**What remains partially decoded:**
- Per-frame placement data (transform matrix, depth, blend mode)
- char_id → symbol mapping (runtime-computed, resolvable by naming)
- CPicFrame schema > 8 tail (variable-length helpers)
- CPicMorphShape (shape tweens)
- CPicBitmap symbol-level metadata

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
(null tag + INT_MIN point) with a heuristic check on the byte after
(valid parent schema + ff-fe-ff marker for layers, small schema for
pages). This fixes parent alignment without fully decoding the child.

---

## Remaining gaps

### 1. CPicFrame schema > 8 tail (structural decode)

All shapes are already recovered by the scanner, but 596 symbols have
`_frame_tail_unparsed: True`. Fully decoding the tail would move shapes
from `recovered_shapes` into the structured body tree with proper
frame/layer association.

**Confirmed layout (loading path at 0x8fe3fa):**

```
Schema > 7:   u16 → field_248
Schema > 8:   FUN_8f9120 → CString field_250 (threshold=23)
Schema branch:
  >= 18:      FUN_8facd0 (timeline sub-object, variable-length)
  10-17:      FUN_8fd980 (u32 + variable data)
  4-9:        FUN_8faad0 + FUN_8f9570 (jump table, complex)
Schema > 10:  u32 field_258 + u32 field_25c
Schema > 11:  u32
Schema > 12:  CArchive::ReadObject CPicMorphShape (via FUN_771700)
Schema > 13:  u32 field_1e4
Schema > 14:  CArchive::ReadObject CObList (via FUN_efefd0)
Schema > 15:  CString + complex object creation
```

**Key finding:** FUN_771700 reads a CPicMorphShape via standard
ReadObject (class-tag protocol). FUN_efefd0 reads a CObList the
same way. These use the same machinery the decoder already handles.

**Blocker:** The variable-length middle section (FUN_8facd0 for
schema >= 18) contains the timeline composition data with embedded
CPic* objects via ReadObject. Decoding it requires understanding
FUN_8f9570 (per-frame reader) which reads schema-gated fields
including a 7-u32 placement matrix at schema >= 4.

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

### 4. CPicText exact field layout

The decoder extracts text_schema, matrix, bounds, font name, font
size, and text content. But the exact byte layout between bounds
and font name has some unknown fields (u32, u16, empty CString).
The text content is stored as null-terminated UTF-16LE without a
length prefix.

**Primary vtable slot 2:** `0x00929800` (loading path at 0x929cf4).
Confirmed reads: u8 schema, matrix (24B via 0xf2c400), rect (16B
via 0xf2c760), u8 field_c8, schema-gated fields, CString font
(via FUN_920900, threshold at [0x12bae88]=10), then FUN_9295c0
(internal state, no archive reads), then more CStrings and fields.

### 5. CPicMorphShape

Shape tweens. Primary vtable needs to be found via constructor.
The class is read via CArchive::ReadObject in CPicFrame schema > 12
(FUN_771700 at CRuntimeClass 0x12946d8).

### 6. CPicBitmap metadata

Pixel data is fully extracted. Missing: linkage name, smoothing flag,
compression quality from the symbol-level metadata.

### 7. CPicLayer schema >= 4 tail

Layer name is extracted (schema >= 11 CString via FUN_f34c30,
threshold at [0x12b8a78]=11). The schema >= 4 block has additional
fields (layer type, outline color, visibility, lock, parent index)
that are not yet decoded. End-marker scan skips them for now.

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

- `rendered/total` should stay at 806/841 or go up
- `audit.py` shows 35 unrendered (33 CPicSprite + 2 empty CPicFrame)
- Recovery scanner finds 31,168 shapes with 0 missed
- Total edges: ~16.3M

---

## CS3/CS4 features not yet supported

The current decoder was built from Flash 8's `flash.exe`. Flash CS3 (2007)
and CS4 (2008) added major features to the binary FLA format before the
switch to XFL/zip in CS5 (2010):

### Flash CS3 (v9)
- **ActionScript 3.0 + AVM2** — AS3 bytecode in symbol streams
- Files targeting Flash Player 9+ may have different serialization

### Flash CS4 (v10) — biggest gap
- **Inverse Kinematics / Bone Tool** — IK armatures with bone structures.
  Likely new CPic classes: `CPicBone`, `CIKBone`, `CIKJoint`, etc.
- **Object-based motion tweening** — replaces classic frame-by-frame tweens
  with interpolated motion paths. New tween data format in the timeline.
- **3D transforms** — z-axis rotation/translation of 2D objects.
  Adds perspective, vanishing point, z-position fields to placement data.
- **Motion Editor curves** — bezier easing data for tween properties

### What's needed
1. **CS4 `Flash.exe`** from archive.org for Ghidra disassembly:
   - https://archive.org/details/adobe-flash-professional-cs-4.7z
   - https://archive.org/details/adobe-flash-cs-4-install-americas
2. **CS4-era FLA files** that use bones/3D/object tweens as test fixtures
3. New class decoders for the IK/3D/tween serialization
4. Compare CRuntimeClass tables between Flash 8 and CS4 to find new classes

### The test corpus
All 9 FLAs in the current test corpus target **Flash Player 7** with
**ActionScript 2** — they're pure Flash 8 era and don't use any CS3/CS4
features. The decoder handles them at 100% shape coverage.

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
