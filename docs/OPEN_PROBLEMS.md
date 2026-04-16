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

### 2. CPicSprite (no implementation at all)

`Serialize VA = 0x00913d30`. CPicSprite is the movie-clip / animation
state machine: it owns layers, frame-by-frame keyframe data, sound sync,
ActionScript hooks. About 36 of the unrendered symbols in our test
corpus are CPicSprite-only.

`fla_decoder/decoder.py` doesn't even attempt this class — the structured
parser walks past it via the generic `read_cpicobj_fields` and finds no
shapes inside. We rely entirely on the recovery scanner to dig shapes
back out of the bytes.

**Next step:** decompile `0x00913d30` and translate. Likely fields:
- Frame array (per-frame layer state)
- Layer array (each layer has its own frame stream)
- Tween descriptors (linear, ease, motion-guide)
- Sound sync mode + start sample
- AS2 frame scripts (already extractable as raw strings via
  `scripts/audit.py`)

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

### 4. CPicText body

`Serialize VA = 0x0091e6f0`. ~7 unrendered symbols are CPicText.
Decoding would let us recover dynamic text fields (font, size,
alignment, default text) — useful for any port that needs to recreate
UI labels.

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

| What | VA | Notes |
|---|---|---|
| `CArchive::ReadObject` | `0x00ee3e6c` | The Rosetta Stone — shows the class-tag protocol |
| `CPicObj::Serialize` | `0x008ceea0` | Shared by CPicShape / CPicLayer / CPicPage / CPicFrame / CPicBitmap |
| `CPicSprite::Serialize` | `0x00913d30` | Class-specific (timeline) |
| `CPicText::Serialize` | `0x0091e6f0` | Class-specific (text fields) |
| `CPicMorphShape::Serialize` | `0x011525e8` | Class-specific (shape tweens) |
| `FUN_008fdb80` | — | CPicFrame's actual tail-reading helper |
| `FUN_00f3c430` | — | Fill-style reader; takes `caps_flag = shape_schema > 2` |

`research/data/runtime_classes.json` has all 46 CRuntimeClass entries
with sizes/schemas/base-class pointers. `research/data/serialize_vas.json`
has the Serialize VA per class, plus the vtable VA in case you need to
walk other slots.

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
