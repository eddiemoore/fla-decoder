# Open problems

This decoder currently lands ~95% of *shapes* across a representative
test corpus. The remaining gaps are mostly in **timeline / composition**
data — the structures Flash uses to assemble shapes into frames, layers,
sprites, and movie-clip animation. None of that is rendered yet; the
recovery scanner papers over the missing decoding by extracting raw
shapes from anywhere in the byte stream.

This document lists what's still unknown, where in `flash.exe` to look,
and what a fresh contributor (or a fresh AI session) needs to make
progress.

---

## What you need to do timeline RE

1. **A copy of `flash.exe`** from a Macromedia Flash Professional 8
   install. The installer is on archive.org (~108 MB; extract the SFX
   then the Data1.cab). The PE we analyzed was 16.8 MB, Win32 x86,
   MFC 7.1, VS2003-era.
2. **Ghidra 12.x** with the binary loaded and auto-analyzed.
3. **Sample `.fla` files** to iterate against. Drop them into a local
   `tests/fixtures/` directory (gitignored). You'll want a mix:
   - A small file with one or two symbols (debug speed)
   - A scene-style FLA with multiple layers + sprites
   - The biggest "master" FLA you have access to (stress test)
4. **Patience.** Each unknown field tends to require one Ghidra session
   to nail down.

---

## Workflow that worked previously

For each undecoded class:

1. Look up the Serialize VA in `research/data/serialize_vas.json`.
2. In Ghidra: navigate to that VA, run the decompiler.
3. Read the decompilation. The `CArchive&` argument is normally `param_2`
   or `param_3`; calls like `(*(code *)(*(int *)param_2 + 8))(param_2, ...)`
   are virtual-dispatch reads/writes on the archive.
4. Match each `archive >> field` (or `archive << field` on the save side)
   to a `Reader.uN()` call in `fla_decoder/decoder.py`.
5. Add the field to the relevant `read_cpic*` function. Use `try/except
   EOFReader` if it's schema-gated and might not be present.
6. Test on a fixture; check that adjacent fields still parse.

---

## Specific gaps

### 1. CPicFrame schema-23 tail (highest value)

`fla_decoder/decoder.py:read_cpicframe` decodes schemas 1..7 reliably. Beyond
that we bail out and set `_frame_tail_unparsed: True`. Decompiled comment
lists the known fields:

```
if frame_schema > 8:  helper FUN_008f9120 (variable)
if frame_schema > 9:  helper FUN_008fd980 (variable)
if frame_schema >= 4: helper FUN_008faad0 etc. (mid-block)
if frame_schema > 10: u32 + u32 → field_600/0x25c
if frame_schema > 11: u32 → field_254
if frame_schema > 12: helper FUN_00771700
if frame_schema > 13: u32 → field_1e4
if frame_schema > 14: operator>> (1 read)
if frame_schema > 15: helper FUN_008f9120 + string ops
```

**Next step:** decompile each `FUN_xxx` helper in Ghidra (Serialize VA
for CPicFrame is `0x008ceea0` shared with CPicObj — the actual class-
specific work is in `FUN_008fdb80`). The big "timeline-container"
symbols (e.g. one observed at 2.1 MB containing a fountain-scene
preview) live here; getting CPicFrame schema 23 right would let us
remove the recovery-scanner workaround.

### 2. CPicSprite timeline sub-objects (partially decoded)

CPicSprite::Serialize at primary vtable slot 2 (VA `0x00913d80`) has
been disassembled. The decoder now reads: CPicObj base, CPicSymbol fields
(symbol_schema, matrix, field_b0, field_cc), sprite_schema, and extracts
frame labels + AS2 scripts by string scanning.

What's still missing is the structured frame/layer data inside the
sub-objects. The key function is `FUN_008facd0` (timeline at `this+0xf4`)
which reads: `u32 count`, `u32 type`, then a loop of `u32 frame_entry_id`
values dispatched by type (0=array, 1=tree insertion, 2=CString).

Empirically, the per-frame blocks are 130 bytes each with: a near-
identity matrix, a u32 reference ID (incrementing), and a frame index.
These look like placement records ("put symbol X at transform Y on
frame N").

**Next step:** finish disassembling `0x008facd0`'s loading path and
`0x00913bc0` (frame layout helper) to map the 130-byte frame blocks
precisely. Then implement structured frame extraction in the decoder.

### 3. CPicLayer / CPicPage tail fields

Both currently fall back to `read_cpicobj_fields` only — the
class-specific tails are unimplemented. CPicLayer almost certainly has:
- Layer name (we can pull it as a UTF-16 string but not associate it)
- Layer type (normal / mask / masked / guide / folder)
- Outline color
- Visibility / lock flags
- Parent-layer index (for nesting)

CPicPage is the scene; likely has a name, frame count, scene order.

These are small/cheap to decode and would massively improve the audit
output (you'd know which symbols are masks vs guides).

### 4. CPicText body (partially decoded)

CPicText::Serialize at primary vtable slot 2 is VA `0x0091e960`.
The decoder now reads: CPicObj base, text_schema, matrix, bounding rect
(4 × s32 twips), and extracts font name + font size by pattern scanning.

Still unknown: the exact field layout between the bounds and the font
name (there are some u16/u32 fields and an empty string), the text
content encoding (inline UTF-16 without a standard length prefix), and
text alignment / color / embedding flags. Disassembling `0x0091e960`
would nail all of these.

### 5. CPicMorphShape

`Serialize VA = 0x011525e8`. Shape tweens (the start+end shape pair
for a morph). Probably encoded as two CPicShape bodies plus the
interpolation hints. Untouched.

### 6. CPicBitmap metadata

The pixel data is already extracted by `fla_decoder/lossless.py` and
`fla_decoder/bitmaps.py`. What's missing is the *symbol-level* metadata:
the linkage name, smoothing flag, compression quality. This couples to
CPicBitmap's Serialize body (shared CPicObj VA at `0x008ceea0` plus the
class-specific writer).

---

## Things we know NOT to chase

- **A from-scratch parser of the OLE2 container.** `olefile` is fine and
  battle-tested.
- **The XFL/zip format.** That's the post-CS5 format and is already
  handled by other tools (e.g. `lifeart/fla-viewer`). This project is
  specifically the pre-CS5 binary.
- **Re-encoding / writing .fla files.** Read-only is hard enough.
- **AS3 bytecode.** Pre-CS5 FLAs use AS1/AS2 stored as readable strings.
  ABC bytecode appears in CS5+ only.

---

## Useful Ghidra anchors (from prior RE)

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
| `CPicFrame` | `0x01085d4c` | `0x008fdb80` | CPicShape base + frame tail (documented) |
| `CPicSymbol` | `0x01086d84` | `0x00916800` | CPicObj base + symbol_schema + matrix + u16s + CString name + frame data |
| `CPicSprite` | `0x01086c1c` | `0x00913d80` | CPicSymbol base + sprite_schema + timeline sub-objects |
| `CPicText` | `0x01087358` | `0x0091e960` | CPicObj base + text fields (partially decoded) |

### CPicSymbol::Serialize call map (VA 0x00916800)

```
CPicObj::Serialize(archive)          → 0x00902d70
u8  symbol_schema                    (via schema lookup table at 0x12b9570)
24B matrix at this+0x78              → 0x00f2c2b0 (read_matrix_6)
u16 field_b0
u16 field_cc
FUN_009024f0(archive, &field_90)     — struct: u8 marker + series of u16 fields
FUN_00916540(archive, schema, &field_f0) — CString (symbol name) via 0x4710e0
conditional: u32 field_d0 or field_74 (media/sound ref)
if symbol_schema < 10: extended handling via FUN_00916540 again
```

### CPicSprite::Serialize call map (VA 0x00913d80)

```
CPicSymbol::Serialize(archive)       → 0x00916800
u8  sprite_schema                    (read directly from stream)
if sprite_schema >= 2:
    FUN_008facd0(archive, &field_f4)  — TIMELINE DATA: reads u32 count + u32 type,
                                        then loops reading u32 frame IDs with
                                        type-dependent payloads (tree/list insertion)
FUN_00913bc0(archive, sprite_schema, &field_160)  — frame layout helper
if sprite_schema >= 3:
    FUN_005c5b00(archive, &field_164)
if sprite_schema >= schema_level_6:
    FUN_00937590(&field_150, archive, mode)
if sprite_schema >= 5:
    u32 → field_190
if sprite_schema >= 8:
    FUN_005d4790(&field_15c, archive)
```

### Sub-function VAs still needing disassembly

| VA | Called from | Purpose |
|---|---|---|
| `0x008facd0` | CPicSprite slot 2 | Timeline sub-object at this+0xf4 (partially decoded) |
| `0x00913bc0` | CPicSprite slot 2 | Frame layout at this+0x160 |
| `0x009024f0` | CPicSymbol slot 2 | field_90 struct (u8 + u16 array) |
| `0x005c5b00` | CPicSprite slot 2 | Sub-object at this+0x164 |
| `0x00937590` | CPicSprite slot 2 | Sub-object at this+0x150 |
| `0x005d4790` | CPicSprite slot 2 | Sub-object at this+0x15c |
| `0x0091e960` | CPicText slot 2 | Text body (font, size, content, alignment) |

### Other anchors

| What | VA | Notes |
|---|---|---|
| `CArchive::ReadObject` | `0x00ee3e6c` | The Rosetta Stone — shows the class-tag protocol |
| `FUN_008fdb80` | — | CPicFrame's actual tail-reading helper (= CPicFrame slot 2) |
| `FUN_00f3c430` | — | Fill-style reader; takes `caps_flag = shape_schema > 2` |
| `0x008f3f70` | — | Read u8 from archive (used in saving path) |
| `0x0040a820` | — | Read u32 from archive |
| `0x004710e0` | — | Read CString from archive (used for field_f0 symbol name) |

`research/data/runtime_classes.json` has all 46 CRuntimeClass entries
with sizes/schemas/base-class pointers. `research/data/serialize_vas.json`
has Serialize VAs per class (**note: these are from the SECONDARY vtable
for CPicSprite, not the primary — see above**).

---

## How to verify progress

After adding any field, the smoke check is:

```bash
python3 scripts/decode.py path/to/test.fla /tmp/out
python3 scripts/audit.py path/to/fla_dir/
```

Look for:
- `rendered/total` percentage stays the same or goes up (no regressions)
- `audit.py` shows fewer `CPicSprite` / `CPicFrame` / `CPicText` entries
  in the unrendered list as you successfully decode those tails

The recovery scanner currently masks failures — if you finish CPicFrame
schema 23 properly, the `recovered` count in `_summary.json` should
drop substantially while `ok` stays flat.

---

## Timeline composition: what we know now

### Per-frame data structure (FUN_498020 at 0x498020)

The per-frame reader is called from FUN_008facd0 (type=0 path) for
each frame in the timeline. It reads:

```
u32  schema/version
u32  entry_count
per entry:
    u32  char_id  (index into global table at 0x13c2b68)
    [object.Serialize() — variable-length, depends on object type]
```

After FUN_498020 returns, FUN_008facd0 reads the frame's strings:
- CString frame_label (via FUN_8f9290)
- CString instance_name
- CString action_script

### char_id resolution

The `char_id` is a direct array index into a global table at
`0x13c2b68`. The lookup: `table[char_id]` → CPic* object pointer.
The table is populated during symbol deserialization through virtual
dispatch (FUN_494310 creates wrapper objects, vtable+0x38 returns
the char_id, vtable+0x58=Clone, vtable+0x68=Serialize).

The char_id values are internal Flash editor IDs — they don't appear
as explicit fields in the binary. The mapping is runtime-computed.
For practical purposes, frame labels map to library names by naming
convention (this was verified to work across the test corpus).

### Extraction results

3,493 keyframes extracted across 7 FLAs (9 total, 2 empty).
Each keyframe has: label, char_id, depth, instance_name, script.

### VAs for further work

| VA | Function | Purpose |
|---|---|---|
| `0x00498020` | FUN_498020 | Per-frame data reader |
| `0x00482c10` | FUN_482c10 | Per-frame data writer (saving only) |
| `0x00494310` | FUN_494310 | Global table init + char_id registration |
| `0x13c2b68` | — | Global char_id → CPic* lookup table |
