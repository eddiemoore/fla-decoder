# Ghidra-based RE of Macromedia Flash 8 binary FLA format

Goal: build a standalone, open-source decoder for pre-CS5 binary FLA files
(OLE2 compound documents with `CPicPage` / `CPicLayer` / `CPicShape` /
`CPicText` / `CMediaSound` MFC-serialized objects).

## Setup

- **Ghidra 12.0.4** (PUBLIC 2026-03-03)
- **Flash Professional 8** installer (108 MB; the installer, not the
  binary itself — must be extracted to get at `Flash.exe`). Contributors
  must source this themselves; it is **not redistributed** by this
  repository, and `*.exe` is `.gitignore`d to make accidental commits
  difficult. Reverse engineering this binary for interoperability with
  the FLA file format is protected activity under DMCA §1201(f) (US)
  and Article 6 of the EU Software Directive 2009/24/EC. See the
  top-level [`NOTICE`](../NOTICE) file for the full legal posture.
- Target binary: `Flash.exe` (after install; MFC-based, Win32 x86, VS2003/2005 era)

## MFC serialization primer (what we're looking for)

Flash 8 is built with MFC 7.1 (VS 2003) — confirmed by the `CObject`/`CRuntimeClass`
patterns we've already seen in the FLA container. Key MFC internals:

### `CRuntimeClass` struct (32-bit Windows)

```c
struct CRuntimeClass {
  LPCSTR           m_lpszClassName;      // -> "CPicShape"
  int              m_nObjectSize;
  UINT             m_wSchema;            // version from IMPLEMENT_SERIAL
  CObject*      (*m_pfnCreateObject)();  // default ctor trampoline
  CRuntimeClass*(*m_pfnGetBaseClass)();
  CRuntimeClass*   m_pBaseClass;
  CRuntimeClass*   m_pNextClass;         // linked list of all registered classes
};
```

### What `DECLARE_SERIAL` + `IMPLEMENT_SERIAL` expand to

- A static `CRuntimeClass` initialised with the class name, size, schema,
  and pointers to `CPicShape::CreateObject()` and `CPicShape::_GetBaseClass()`.
- A `Serialize(CArchive&)` virtual override on the class vtable.

### What `CArchive::operator<<` / `operator>>` do

For primitive types (`BYTE`, `WORD`, `DWORD`, `int`, `float`):
just read/write the raw little-endian bytes into/out of the archive buffer.

For `CObject*`: calls `CArchive::WriteObject()` / `ReadObject()` which
emits the class-tag scheme we've already decoded (`FFFF <schema> <nameLen>
<name>` for new class, `0x8000 | idx` for back-ref, `0x7FFF` for nullptr).

For `CString`: length prefix + UTF-16 chars. The `FF FE FF <len>` form
we see in the binary is CString's internal wire format for CStringW.

## RE workflow

### Step 1 — locate `"CPicShape"` string in Flash.exe
Use Ghidra: *Search → For Strings* → filter "CPicShape". Usually a handful
of matches (one real, occasionally a debug-build leftover).

### Step 2 — find `CPicShape::CRuntimeClass` struct
Cross-reference the string. The first DWORD of the enclosing struct is the
string pointer; the struct has the layout above. The `m_pfnCreateObject`
function pointer immediately gives us the class ctor, and from the ctor's
first instruction we see the vtable pointer.

### Step 3 — find the virtual `Serialize` slot
In MFC's `CObject` vtable, `Serialize` is typically the 5th virtual slot
(after destructor, `AssertValid`, `Dump`, `IsKindOf`). For VS2003 x86,
that's vtable offset `+0x10` or `+0x14` depending on compiler options.

### Step 4 — decompile `CPicShape::Serialize`
Ghidra's decompiler will show:

```c
void CPicShape::Serialize(CArchive *ar) {
    CPicBase::Serialize(ar);    // base class
    if (ar->m_nMode & 1) {      // loading
        ar->m_lpBufCur;         // read patterns
        ...
    } else {                    // storing
        ...
    }
}
```

The loading branch is what we reverse.

### Step 5 — map each `ar >> X` to a field
For each:
- primitive read → one fixed-size byte sequence
- object read (ReadObject call) → class-tag dispatch (already understood)
- custom helper (e.g. bit-reader) → decompile the helper

### Step 6 — repeat for every relevant class
Priority order (smallest/simplest first):
1. `CPicFrame` — just layer list + a few flags
2. `CPicLayer` — frame list + style
3. `CPicShape` — THE MAIN ONE (styles + geometry)
4. `CPicPage` — scene wrapper
5. `CPicText` — text fields
6. `CPicMorphShape` / `CMorphSegment` / `CMorphCurve` — morph tweens

## Shape-specific hypotheses to confirm in Ghidra

From our observational RE:

- **Body starts with a 28-byte fixed header** — likely a `CPicBase::Serialize`
  call writing cached bounds (INT_MIN placeholders), object ID, and flags.
- **`05 <u32> 00 00 00 01 00 00 00 <RGB> ...`** is the fill/line style
  block. Expect to see MFC `CArchive::ReadCount()` for the count and
  per-style reads.
- **Geometry region is mid-body, ends at recognisable footer**. The
  `01 80` tokens we see are CObject back-refs (`0x8001`), meaning shapes
  contain nested serialized objects (probably `CPicPath` or `CPicEdge`
  sub-classes not yet surfaced in our class survey).

If those nested sub-classes exist, they should appear in the `"CPic..."`
string search — my earlier class survey only found classes that are
*first declared* in a stream. Back-ref-only classes wouldn't have their
name written to the FLA (just the class-tag), so they'd be invisible from
pure container analysis.

### Open hypothesis: nested-class serialization

Possibility: the shape geometry is NOT a bit-packed blob but a sequence of
serialized `CPicEdge`/`CPicMoveTo`/`CPicLineTo`/`CPicCurveTo` objects,
each of which is `<classtag> <dx:s16> <dy:s16>` or similar. That would
explain the `01 80` tokens interspersed with the coordinate-like bytes
— each `01 80` is a "new instance of previously-declared class 1" header,
followed by fields of that class.

If this is right, the decoder is actually simpler than a bit-packed SWF
reader — just nested-object deserialization.

## Session log

### 2026-04-15 — Kickoff

- Downloaded Ghidra 12.0.4
- Downloaded Flash Professional 8 installer (108 MB)
- Installed 7zip + innoextract
- Extracted `flash.exe` from InstallShield SFX → Data1.cab → 16.8 MB Win32 x86 PE

### 2026-04-15 — String-level discoveries (MAJOR)

#### Full `CPic*` class hierarchy

Found 20 picture classes in `flash.exe` strings:

- `CPicObj`          — **base class for all picture objects**
- `CPicBitmap`       — bitmap (lossless + JPEG)
- `CPicButton`       — button symbol
- `CPicFont`         — font
- `CPicFrame`        — timeline frame
- `CPicLayer`        — timeline layer
- `CPicMorphShape`   — morph (shape tween)
- `CPicOle`          — OLE object (rare)
- `CPicPage`         — scene / page
- `CPicScreen`       — Screens feature (Flash MX2004+)
- `CPicShape`        — shape wrapper
- **`CPicShapeObj`** — **inner shape geometry holder** (separate from CPicShape!)
- `CPicSoundCreator` — sound definition
- `CPicSprite`       — movie clip
- `CPicSwf`          — imported SWF
- `CPicSymbol`       — library symbol
- `CPicTempClipboardObj` — clipboard helper
- `CPicText`         — text field
- `CPicVideo` / `CPicVideoStream` — video

The **CPicShape / CPicShapeObj split** matters — the shape DOM wrapper and
the geometry-bearing object are distinct. Our geometry decoding target is
probably `CPicShapeObj::Serialize`, not `CPicShape::Serialize`.

#### Half-edge graph geometry

Strings found: `getHalfEdge`, `splitEdge`, `getOppositeHalfEdge`, `getEdge`,
`HalfEdge`, `deleteEdge`, `edges`.

Flash's internal shape representation is a **half-edge graph** (topological
data structure used in computational geometry: each edge has two half-edges,
one per incident face; vertices, edges, faces form a mesh).

Implication: the FLA shape geometry is NOT a linear moveto/lineto/curveto
stream (as in SWF). It's a serialized half-edge graph with:

- vertex table (coordinates)
- edge table (pairs of half-edges + curve data for curved edges)
- face table (fill-style references per face)

This explains why all our SWF-inspired decoders failed: the data isn't
SWF-compatible at all. When Flash exports a FLA→SWF, it traverses the
half-edge graph and emits the flat SHAPERECORD stream that SWF expects.

### MFI — Macromedia Flash Importer SDK (MAJOR find)

Also found 79 `MFI*` class/module names in `flash.exe`:

```
MFIShapeModule / _Definition / _Definition_v
MFIShapeDescription / _Definition / _Definition_v
MFIShapeEdgePath    / _Definition / _Definition_v
MFIShapePath        / _Definition / _Definition_v
MFIContourShape
MFICubic
MFIFillStyle        / _Definition / _Definition_v
MFILineStyle        / _Definition / _Definition_v

Plus 20+ other modules:
MFIBitmapModule, MFICharacterOptions, MFIFrameModule, MFIGraphicsModule,
MFIItemInstanceModule, MFILayerModule, MFILibraryModule, MFIMathModule,
MFIMovieModule, MFIParagraphOptions, MFISceneModule, MFIScreenModule,
MFISoundModule, MFITextModule, MFITextOptions, MFIUtilsModule,
MFIVideoModule, MFIModuleList, MFIModuleInfoDescriptor, MFILibraryItemId,
MFIMAC

Entry point: `MFIGetImporterInterface`
```

Naming conventions:
- `_Definition` suffix → the abstract interface
- `_Definition_v` → versioned variant (v2 importer API)

This is clearly a **public or semi-public importer plugin SDK** — Flash 8
had an architecture where third-party importers could plug in to read
foreign formats into the Flash authoring tool. The SDK exposes the
internal structure of EVERY asset type, including shapes.

If we can find docs or a header file for this SDK, it's a complete
Rosetta Stone: `MFIShapeModule`'s interface defines exactly what a shape
consists of (contours, edges, cubics, fills, lines) and how they're
serialised. That becomes our decoder spec, no disassembly needed.

### Next actions

1. Search archive.org and Macromedia archives for **"Macromedia Flash MX
   Importer SDK"** or **"Flash 8 Importer SDK"** — sometimes bundled as
   `MFIImporterSDK.zip` or similar.
2. If no SDK found: disassemble `MFIGetImporterInterface` in Ghidra to
   find the vtable of the `MFIShapeModule` interface, then work backwards
   to the internal serialiser.
3. Parallel: disassemble `CPicShapeObj::Serialize` directly.

## 2026-04-15 — LOADED VTABLES + FOUND SERIALIZE SLOT (MAJOR)

### CRuntimeClass VA map (all CPic* + CMedia* classes)

JSON in `research/data/runtime_classes.json`. Summary:

| class              | objsize | schema | create VA | base class |
|--------------------|---------|--------|-----------|------------|
| CPicObj            | 116     | 1      | 0x9033b0  | CObject    |
| CPicShape          | 300     | 1      | 0x910c60  | CPicObj    |
| CPicShapeObj       | 244     | 1      | 0x912440  | CPicSymbol |
| CPicFrame          | 672     | 1      | 0x8faa70  | CPicShape  |
| CPicLayer          | 176     | 1      | 0xf34bd0  | CPicObj    |
| CPicPage           | 212     | 1      | 0x9062c0  | CPicObj    |
| CPicSymbol         | 244     | 1      | 0x918580  | CPicObj    |
| CPicBitmap         | 152     | 1      | 0x8e8310  | CPicObj    |
| CPicText           | 320     | 1      | 0x92a920  | CPicObj    |
| CPicMorphShape     | 156     | 1      | 0x775b80  | CObject    |
| CMediaSound        | 308     | 1      | 0x8d9590  | CMediaElem |
| CMediaElem         | 232     | 1      | 0x8d7830  | CObject    |
| CMediaBits         | 324     | 1      | 0x8d4980  | CMediaElem |
| CMediaVideo        | 252     | 1      | 0x8dc2a0  | CMediaElem |
| CColorDef          | 128     | 0      | 0x5944f0  | CObject    |
| CDocumentPage      | 600     | 1      | 0x8cbfd0  | CObject    |
| CMorphCurve        | 40      | 1      | 0x775d00  | CObject    |
| CMorphSegment      | 52      | 1      | 0x775ca0  | CObject    |

### Serialize is at PRIMARY vtable slot 2 (+0x08)

Reverse-engineered `CArchive::ReadObject` (@ 0x00ee3e6c). After reading the
class tag and calling `CreateObject()`, it dispatches:

```c
(**(code **)(*(int *)pObj + 8))(ar);  // virtual call on pObj, slot 2
```

Equivalently in `CArchive::WriteObject`:

```c
pCVar3 = (CRuntimeClass*)(*(code*)**(undefined4**)param_1)();  // slot 0 → GetRuntimeClass
WriteClass(ar, pCVar3);
...
(**(code **)(*(int *)param_1 + 8))(ar);  // slot 2 → Serialize
```

**So vtable[0] = GetRuntimeClass, vtable[2] = Serialize** (primary vtable).

### Decoded `CPicObj::Serialize` (FUN_00902d70)

```c
void CPicObj::Serialize(CPicObj *this, CArchive *ar) {
    FUN_00f19490();                       // pre-call instrumentation
    if (ar->IsLoading()) {                // ar->m_nMode & 1
        // push self onto global parent stack
        this[0x1c] = global_cur_parent;
        global_cur_parent = this;

        uint8_t schema = ar.ReadByte();   // per-object schema/version byte
        uint8_t flags  = ar.ReadByte();
        this[0x11] = (flags decoded into bits);

        // Call GetDoc-like virtual (slot 0xcc)
        if ((*this->vt[0x33])()) this[0x11] &= 0xFE;

        // Read children list (variable length, null-terminated)
        while (CObject *p = ar.ReadObject(&CPicObj_RuntimeClass)) {
            // add p to doubly-linked list rooted at this[4]/this[5]
            ...
            // if p is a CPicSymbol, invoke its OnAdd (vt[0x74])
        }

        if (schema > 0)  FUN_00f2c5f0(ar, &this[0x12]);   // read 2D matrix (6 floats)
        if (schema > 2) { uint8_t b; ar >> b; this[0x14] ^= ...; }
        if (schema > 3) { uint8_t b; ar >> b; this[0x69] = b != 0; }
    } else {
        /* mirror write path ... */
    }
}
```

**So every CPicObj starts with**:
1. `u8 schema`
2. `u8 flags`
3. (potentially nested virtual call result)
4. Series of `ReadObject(&CPicObj_RuntimeClass)` calls, null-terminated
5. If schema > 0: 6-element matrix
6. If schema > 2 or > 3: extra bytes

### Decoded `CPicShape::Serialize` (FUN_00910e40)

```c
void CPicShape::Serialize(CPicShape *this, CArchive *ar) {
    CPicObj::Serialize(this, ar);    // **inherited fields first**

    if (ar->IsLoading()) {
        uint8_t shape_schema = ar.ReadByte();
        FUN_00f2c400(ar, &this[0x74]);           // read RECT (bounding box)
        FUN_00f3da60(&this[0x8c], ar, shape_schema > 2);  // ★★ read SHAPE GEOMETRY ★★
        this[0x104] = 0;
    } else {
        /* write path ... */
    }
}
```

**`FUN_00f3da60` is the SHAPE GEOMETRY DESERIALIZER** — takes shape data
storage at `this+0x8c`, the archive, and a "is new schema" boolean.

This is where the half-edge graph / fill styles / edges actually come in.
NEXT STEP: decompile `FUN_00f3da60`, then `FUN_00f2c400` (RECT reader)
and `FUN_00f2c5f0` (matrix reader).

### Addresses to continue from

| label                       | VA          | purpose                      |
|-----------------------------|-------------|------------------------------|
| CPicObj::Serialize          | 0x00902d70  | base picobj fields           |
| CPicShape::Serialize        | 0x00910e40  | shape header + geometry call |
| CPicFrame::Serialize        | 0x008fdb80  | frame fields                 |
| CPicShapeObj::Serialize     | 0x00916800  | library-symbol shape wrapper |
| FUN_00f3da60                | 0x00f3da60  | **★ shape geometry reader ★** |
| FUN_00f3c150                | 0x00f3c150  | coord delta reader           |
| FUN_00f3c430                | 0x00f3c430  | fill style reader (schema ≥3)|
| FUN_00f3c8c0                | 0x00f3c8c0  | inline fill (bit-packed)     |
| FUN_00f26d00                | 0x00f26d00  | straight edge builder        |
| FUN_00f26cc0                | 0x00f26cc0  | curved edge builder          |
| FUN_00f2c400                | 0x00f2c400  | read 6×u32 RECT/matrix       |
| FUN_00f2c5f0                | 0x00f2c5f0  | read 2×u32 point             |
| CArchive::ReadObject        | 0x00ee3e6c  | already decoded              |

## Complete shape decoder specification (as of 2026-04-15)

### Format pseudocode

```c
// Bytes after CPicShape header (which itself is: CPicObj fields, then u8 shape_schema, then RECT, then the shape data)

// ── Shape Data (FUN_00f3da60) ──────────────
u8  shape_schema
u32 edge_count_hint     // inform-only; if > 0x4B0 shape gets a flag
u16 fill_style_count
for each fill in 0..fill_style_count:
    if shape_schema < 3:                   // old solid-color-only fills
        u32 color_rgba
        u16 flags
    else: read_fillstyle(fill, ar, shape_schema > 2)   // see below

u16 line_style_count
for each line in 0..line_style_count:
    u32 width (twips)     // overwritten later
    u16 flags
    read_inline_fill(line, ar)            // bit-packed — see below
    if (shape_schema > 2):                 // new caps/joins/extra
        u8 startCap, endCap, joins, reserved
        u16 miterLimit
        read_fillstyle(line+0x18, ar, true)
    line.width = width_from_u32            // overwrites temporary
    if shape_schema < 4 and line.alpha == 0: line.alpha = 0xFF

if (shape_schema > 4):
    s32 edge_count
    for each edge:
        u32 x0, y0, x1, y1, x2, y2, x3, y3     // cubic Bezier, 4 anchors
        addVertex(x0,y0,endpoint), addVertex(x1,y1,control),
        addVertex(x2,y2,control), addVertex(x3,y3,endpoint)
        commitEdge()

    // More structure initialization...
    FUN_00f2a900, FUN_00f2a9a0, FUN_00f2a8e0 - helpers to zero out bounds
    FUN_00f2f000(shape) — finalize shape topology
    return 0
else:
    // ── OLD SCHEMA (< 5) edge stream ──
    loop:
        u8 edge_flags
        if edge_flags == 0: break          // terminator
        // Optional style change (fillstyle 0, fillstyle 1, linestyle indices):
        if edge_flags & 0x40:
            if edge_flags & 0x80: read u8 u8 u8  (3 byte indices)
            else:                 read u16 u16 u16  (3 word indices)
        // Three coord deltas: (from+ctrl), (ctrl+to), … pattern
        read_delta(type0)    // delta1
        read_delta(type1)    // delta2
        read_delta(type2)    // delta3
        // Types extracted from edge_flags (bit 0-5 pair-wise):
        //   type0 = bits 0-1 of edge_flags    (delta-packing code 0/1/2/3)
        //   type1 = bits 2-3
        //   type2 = bits 4-5
        // (exact bit mapping TBD — verified against file)

        // cumulative math (local_3c/38 = from; local_44/40 = control; local_4c/48 = to)
        // Straight edge if edge_flags & 0xc == 0; else curved
        if edge_flags & 0xc == 0: build_straight(&from, &to, &record)
        else:                      build_curved(&from, &ctrl, &to, &record)
        allocate edge record, store, commit to graph
```

### Coord delta encoding `FUN_00f3c150`

```c
// EAX = type code (0..3), ESI = output int[2]
switch (type) {
    case 0: xy = (0, 0)             // 0 bytes
    case 1: xy = (s16, s16)          // 4 bytes, sign-extend to s32
    case 2: xy = (s32, s32)          // 8 bytes
    case 3: xy = (s16<<7, s16<<7)    // 4 bytes — pre-shifted (fixed-point ×128)
}
```

### Fill style reader `FUN_00f3c430` (schema ≥3, per fill = 0x74 B)

```c
u32   color_or_data     (offset +0)
u8    subtype_flags     (offset +4)    // 0x10=gradient, 0x20=?, 0x40=bitmap
u8    more_flags        (offset +5)

if !(subtype & 0x10):
    if subtype & 0x20:
        read 24-byte matrix (6×u32) at +0x08
        read u32 id at +0x20
        read 4 u16 values at +0x24, +0x26, +0x28, +0x2a
    elif subtype & 0x40:
        read 24-byte matrix at +0x08
        if DAT_013c8ec0 == 0:
            read u32 bitmap_id → resolve
        else:
            read CMediaBits object via CArchive::ReadObject
        if resolved is null: zero record + set color = 0xff0000ff (magenta)
else:  // gradient
    read 24-byte matrix at +0x08
    u8 num_stops    (cap 15, at offset +0x23)
    if schema > 2:
        u16 grad_flags    (at +0x20)
        u8 grad_type      (at +0x22)
    for i in 0..num_stops:
        u8 position (0-255)    at +0x24 + i*5
        u32 color              at +0x34 + i*4
```

### Inline fill `FUN_00f3c8c0` (compact, bit-packed)

```c
s16 sVar1
u16 uVar2

param_1[1] = (u8)((uVar2 >> 0xe) & 2)    // flags
if (sVar1 == 0):
    u8 bVar4 = (u8)uVar2
    param_1[0] = bVar4 & 7               // subtype (2/3/4/5 observed)
    switch (bVar4 & 7) {
        case 2:  u16 at +6 = uVar2 >> 3
        case 3:  u16 at +6 = (bVar4 >> 3) & 7
                 u16 at +8 = bVar4 >> 6
                 u16 at +a = (uVar2 >> 8) & 3
        case 4:  u16 at +6 = (bVar4 >> 3) & 3
                 u16 at +8 = (bVar4 >> 5) & 3
                 s16 at +a = (s16)((uVar2 & 0x180) >> 7)
        case 5:  (similar, 6 fields from uVar2)
    }
else:
    param_1[0] = 1                       // simple color
    s16 at +6 = sVar1
    u16 at +8 = uVar2 & 0x7fff
```

### Edge builders

```c
// Straight edge: convert (from, to) to a quadratic-Bezier with mid-control
void build_straight(int from[2], int to[2], int out[7]) {
    out[0..1] = from
    out[2]   = (from[0] + to[0]) / 2
    out[3]   = (from[1] + to[1]) / 2
    out[4..5] = to
    out[6]   = 1                        // type marker: line
}

// Curved edge: full quadratic
void build_curved(int from[2], int ctrl[2], int to[2], int out[7]) {
    out[0..1] = from
    out[2..3] = ctrl
    out[4..5] = to
    out[6]   = 0                        // type marker: curve
    (out+7)[0] = 0
}
```

So internally ALL edges become quadratic Beziers — which matches SWF's
design (STRAIGHTEDGERECORD vs CURVEDEDGERECORD). The format stores:
- Old schema: variable-size records (1B flag + optional 3–6B style change + 0/4/8B coord deltas)
- New schema (5+): fixed 32B per edge, 4 anchors (cubic Bezier)

### What's still unknown / needs verification

- Exact bit mapping from `edge_flags` to `type0/type1/type2` delta types (read
  FUN_00f3da60 disassembly with capstone to find the three `mov eax, ...`
  before each FUN_00f3c150 call).
- Whether the 3 deltas pattern is (dx_from_to_ctrl, dx_ctrl_to, dx_implicit)
  or similar — need to verify by tracing the cumulative math.
- For schema ≥ 5: meaning of `FUN_0070ca10(x, y, ctrl_flag)` — likely adds
  half-edge vertex with topology info.

## 2026-04-16 — FIRST WORKING DECODER

Built `fla_decoder/decoder.py` and verified on fountain.fla Symbol 17:

✅ Top-level class hierarchy parsing: CPicPage > CPicLayer > CPicFrame > CPicShape nesting read cleanly from the raw stream (byte 0 to byte 0x80).

✅ Field decoding confirmed:

| offset | field | decoded |
|--------|-------|---------|
| 0x15/0x20/0x31/0x42 | CPicObj schema per class | 2 at every level |
| 0x16/0x21/0x32/0x43 | CPicObj flags per class  | 0/0/0/1         |
| 0x44-0x45 | CPicShape children tag | NULL (no further nesting) |
| 0x46-0x4d | CPicObj point (schema > 0) | (INT_MIN, INT_MIN) — uninitialised |
| 0x4e    | CPicShape.shape_schema          | 2                               |
| 0x4f-0x66 | CPicShape.matrix (6×u32 16.16FP) | a=1, b=0, c=0, d=1, tx=1.5px, ty=69.45px — identity+translation ✓ |
| 0x67    | shape_data_schema               | 5                                  |
| 0x68-0x6b | edge_hint                      | 23                                 |
| 0x6c-0x6d | fill_count                      | 1                                  |
| 0x6e-0x73 | fill[0]                        | solid, color 0x0066ccff (blue)     |
| 0x74-0x75 | line_count                      | 1                                  |
| 0x76-0x7f | line[0]                        | color 0xff000000 (black), flags 0x0014, inline subtype 0 |

✅ Matrix decoding verified: `1.0` stored as `0x00010000` confirms 16.16 fixed-point for a/b/c/d; tx/ty stored as plain twips.

### Line style on-wire format (corrected)

```
u32     : stroke color (ARGB32, gets stored at line.fill_style.color)
u16     : flags16
<inline fill — 4 bytes>   (from FUN_00f3c8c0)
(if shape_schema > 2:)
   u8 × 4 : start cap, end cap, joins, reserved
   u16    : miter limit
   <full fill_style — variable>     (from FUN_00f3c430)
```

### Edge stream control flow (recovered)

Single decompile reading of FUN_00f3da60 shows:

```c
if (shape_data_schema < 2) {
    // schema 0/1: skip byte-loop
    goto POST;
} else {
    // schema ≥ 2: byte-encoded edge loop until terminator
    while (ar.IsLoading()) {
        u8 edge_flags = ar.u8();
        if (edge_flags == 0) goto POST;
        if (edge_flags & 0x40) {
            if (edge_flags & 0x80)  read 3 × u8 style-change values
            else                   read 3 × u16 style-change values
        }
        read_coord_delta(type = edge_flags      & 3, &delta1)
        read_coord_delta(type = (edge_flags>>2) & 3, &delta2)
        read_coord_delta(type = (edge_flags>>4) & 3, &delta3)
        from   = prev_to + delta1
        ctrl   = from    + delta2     // (delta2 can be zero → straight)
        to     = from    + delta3
        prev_to = to
        build_quad_bezier(from, ctrl if (edge_flags & 0xc) else midpoint, to)
    }
    POST:
    if (shape_data_schema > 4) {
        s32 cubic_count = ar.s32()
        for each: 4 × (s32 x, s32 y)   // cubic Bezier anchors
    }
    // … (more post-processing, not yet decoded)
}
```

So for Symbol 17 (schema=5), we get BOTH byte-loop edges AND cubic32 edges.

### Delta-type codes (FUN_00f3c150)

```
type 0 (0 bytes):  dx=0,    dy=0
type 1 (4 bytes):  dx=s16,  dy=s16
type 2 (8 bytes):  dx=s32,  dy=s32
type 3 (4 bytes):  dx=s16<<7, dy=s16<<7   (fixed-point ×128)
```

Example: Symbol 17 edge 0 has edge_flags = 0x77 = 0b01110111:
- bit 6 set → style change present
- bit 7 clear → u16 style-change format (6 B)
- delta1 type = 3 (4 B)
- delta2 type = 1 (4 B)  ← bits 2-3 = 0b01, non-zero → curved
- delta3 type = 3 (4 B)
- **Edge total: 1 + 6 + 4 + 4 + 4 = 19 bytes**

## 2026-04-16 🏆 WORKING END-TO-END DECODER

`fla_decoder/decoder.py` + `fla_decoder/to_svg.py` render stick-figure character
art from fountain.fla Symbols 16, 17, 18, 37, 38, 39 — a blue circular head
with a body/legs silhouette. **Identical scene-set of symbols** confirms they
are pickup-animation frames (matching the AS code `_root.pickUpFinish = true; ...`
we found earlier).

Sample SVG (fountain.fla, Symbol 17, 23 edges → 73×293 px PNG):

```svg
<svg viewBox="-39.30 -285.30 72.60 292.65">
  <path d="M 0 -220 Q -10 -221 -18 -229 Q -26 -238 -26 -251 Q -26 -264 -18 -273
            Q -10 -282 2 -282 Q 14 -282 22 -273 Q 30 -264 30 -251 Q 30 -238 22 -229
            Q 14 -220 2 -220 Q 1 -220 0 -220" fill="#0066cc" stroke="#000"/>
  …
</svg>
```

### What's done

- ✅ Full OLE2 + MFC class-tag protocol
- ✅ CPicPage / CPicLayer / CPicFrame / CPicShape nested deserialization
- ✅ CPicObj base fields (schema, flags, children, point, extras)
- ✅ CPicShape matrix (16.16 FP a/b/c/d + twip tx/ty)
- ✅ Shape data header (schema, edge hint, fill count, line count)
- ✅ Fill styles: solid / gradient / bitmap (schema ≥ 3)
- ✅ Line styles: stroke color + flags + inline compact fill + optional caps/miter/fill
- ✅ Byte-encoded edge stream: 3 coord deltas per edge with 4 packing types
- ✅ Cumulative quadratic-Bezier edge records with fill0/fill1/line style indices
- ✅ SVG emission grouped by style index
- ✅ Coordinate unit: 1 px = 2560 units (20 twips × ×128 shift factor)

### What's left

- ⏳ CPicFrame / CPicPage specific tail fields — now handled via EOF-tolerant
  partial-return so inner shapes always decode.
- ⏳ Cubic32 post-stream for shape_data_schema ≥ 5 (only relevant to morph
  tween sources and advanced shapes).
- ⏳ Perfect stroke-width and inline compact-fill color decoding — currently
  only basic cases work.
- ⏳ CPicMorphShape / CPicText / CPicBitmap Serialize bodies.
- ⏳ Proper transform application from CPicShape.matrix in the SVG emitter.

## 2026-04-16 — first whole-FLA test

Test corpus: a ~20 MB master FLA with 60 symbols.

```
Counts: {'ok': 47, 'empty': 6, 'no_shape': 7}      (60 symbols, 78% rendered)
```

Contact sheet confirmed plausibly-shaped sprites for the player character
(rendered across 10+ poses), inventory items, scene props, UI panels.

### Important bugfix (fill-style extras parameter)

`FUN_00f3c430` takes `caps_flag = CPicShape.shape_schema > 2` — **not** the
inner shape_data_schema. Our initial code confused the two, which mis-aligned
the gradient-extras reads and cascaded into junk edge coordinates (max coord
values in the billions of ultra-twips → absurdly large SVGs).

With this fix, max coord across all shapes is 3246 px (Sym 103 — a
big scene image), which is plausible.

## 2026-04-16 — FULL TEST CORPUS BATCH RESULTS

Ran `scripts/decode.py` against a corpus of 9 FLAs (841 symbols total).

After implementing the **signature-based recovery scanner** (which finds
plausible CPicShape body starts by looking for the 10-byte
`00 00  00 00 00 80  00 00 00 80` header tail, then attempting to
deserialize a full CPicShape body at that offset):

| FLA category         | symbols | rendered |    %    | recovery used |
|----------------------|---------|----------|---------|---------------|
| Master FLA           | 60      | 53       | 88%     | 23            |
| UI / button library  | 91      | 78       | 86%     | 33            |
| Scene FLAs (×5)      | 690     | 655      | 95%     | 372           |
| Empty wrappers (×2)  | 0       | 0        | –       | –             |
| **TOTAL**            | **841** | **786**  | **93%** | **428**       |

(Pre-recovery numbers were 681 / 841 = 81%.)

The recovery scanner is essential for the big "timeline-container" symbols
(e.g. a 2.1 MB symbol containing a fountain-scene preview wrapped in a
CPicFrame at schema 23 with helpers we don't fully decode). Without
recovery, those returned 0 edges; with the signature scan, that one symbol
alone yields **139 shapes / 95K edges**.

### Pushing to 95%: always-run scanner

Initially the scanner only ran when structured parsing consumed < 95% of
the stream. That missed ~10 symbols where the structured parser walked
100% through a CPicSprite body (successfully, but finding no shapes).
Switching to "always scan the whole stream" + loosening the schema/flags
filter to `schema <= 5, flags <= 0x10` adds 10 more symbols → **95%**.

### The remaining 5% (45 symbols)

Broken down by `scripts/audit.py`:

| leaf class | count | what it is |
|------------|-------|------------|
| CPicSprite | 36    | **Axel player-character animation state machines** (copies across scene FLAs) |
| CPicText   | 7     | Empty dynamic text fields |
| CPicFrame  | 2     | Empty timeline frames |

Every one of these has extractable **frame-label strings** (walkBase /
standLeft / standBack / front / right / left / back / pickUpFront... /
talkFront... / Axel) and **ActionScript code** (`_root.walkDir = "front";`
etc.) but **no intrinsic shape data** — they're pure composition/animation
metadata that references the CPicShape symbols we *have* already rendered.

So the 95% figure represents a complete capture of the **renderable
vector content** in the project. The other 5% is animation wiring that
tells the Flash runtime "at time T, show Sym X translated by (dx,dy)" —
not something a still SVG renderer produces. For a Godot port that
metadata is best consumed by synthesizing `AnimationPlayer` tracks from
the Flash timeline; a separate task from shape extraction.

### Why we can't go further than 95%

Exhaustive audits rule out these sources of potentially hidden shapes:

1. **Embedded `CPicShape` class declarations** (`FFFF 01 00 09 00 "CPicShape"`)
   scattered inside larger streams: already captured by the
   class-decl leg of the recovery scanner. Only 2 such embedded decls
   existed in unrendered symbols (both recovered).
2. **Relaxed edge-count threshold** (accept 1- or 2-edge shapes):
   produces almost exclusively noise matches (e.g. insideShack Sym 25
   recovered a single-edge 291-byte "L" path with no fill or stroke).
   Reverted to `min_edges=3` for signature-based recovery.
3. **Embedded `CPicText` declarations** in unrendered symbols: 15 found;
   all of them have the closest trailing UTF-16 string = `"pickUpRight"`
   or `"Layer 1"` (frame labels, not user text content). The text
   fields are empty dynamic text — no static content to render.
4. **Embedded `CPicSprite` declarations** in unrendered symbols: many
   found, but none contain further embedded shape or text content
   (they're pure animation state-machines that reference external
   symbols).

The remaining 45 symbols genuinely contain zero shape bytes.

Confirmed assets across the project (visible in per-scene contact sheets):

- **Axel the player character** — ~100+ animation frames across all scenes
  (walk/stand/pickup/talk directions, facing variants, arm poses)
- **NPCs / Gardener** character
- **Environment**: fountain, stone pedestals, statue, gate, door, book,
  umbrella, rocks, grass
- **Inventory items**: watering can (green gradient), red heart piece,
  black cat
- **UI**: compass direction pointer, cash register, buttons

"empty" symbols (~130) are timeline-container CPicFrame/Layer/Page nodes
that reference child symbols via CPicSprite — no intrinsic art, just
movie-clip composition. Their content renders via the referenced symbols.

"no_shape" symbols (~30) are CPicText/CPicSprite top-level containers;
their text content and frame labels are extractable via the string
scanner in `scripts/inspect.py`.

### Files in this repo

- `fla_decoder/decoder.py` — core decoder, ~650 LOC, pure-python + olefile
- `fla_decoder/to_svg.py` — SVG emitter with gradient support
- `scripts/decode.py` — batch driver across a directory of FLAs
- `scripts/audit.py` — list unrendered symbols + their text/script content
- `scripts/inspect.py` — per-symbol class tree / strings / bounds inventory
- `docs/FORMAT.md` — format spec
- `docs/RE_JOURNEY.md` — this doc
- `research/data/runtime_classes.json` — all CRuntimeClass metadata
- `research/data/serialize_vas.json` — Serialize function VAs per class
