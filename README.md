# fla-decoder

An open-source decoder for **binary `.fla`** files (Flash 5 through CS6,
2000-2012). These files are Microsoft OLE2 compound documents containing
MFC-serialized object trees — a format that was never publicly documented
and which existing tools (JPEXS, Ruffle, etc.) can read *out* of (when
exporting from `.swf`) but not *in* to.

This project reverse-engineers that format by disassembling Flash 8's
`flash.exe` with Ghidra, reimplementing MFC's `CArchive` protocol to
walk the object tree, and extracting all recoverable data: vector shapes,
audio, bitmaps, text, scripts, timeline data, and document properties.

## Status

Tested on 17 FLAs spanning Flash 5 through CS6:

- **100% shape coverage** — 31,168 shapes / 16.3M edges, zero missed
- **96% symbol render rate** — 806/841 symbols rendered to SVG
- **99.9% useful data** — 840/841 symbols have extracted content

The remaining 35 unrendered symbols are movie-clip composition
containers that reference shapes from other symbols — they contain
no inline geometry but their frame labels, scripts, and metadata
are fully extracted.

### What's extracted

| Data | Status |
|------|--------|
| Vector shapes (fills, gradients, transforms) | 100% coverage |
| Audio (WAV/MP3) | Complete |
| Bitmaps (JPEG/PNG/lossless) | Complete |
| Background color + frame rate | Complete |
| Stage dimensions | Complete |
| Text content, font names, font sizes | Complete |
| AS2 scripts (frame + clip events) | Complete |
| Timeline keyframes with labels | Complete |
| Symbol library (names, types, timestamps) | Complete |
| Layer metadata (name, type, lock, visible, color) | Complete |
| Publish settings (130+ per FLA) | Complete |
| Library folder hierarchy | Complete |
| Shape tweens (CPicMorphShape) | Complete |
| CS4 IK bones (armature hierarchy + transforms) | Complete |
| CS4 motion tweens (AnimationCore XML) | Complete |

## Install

```bash
git clone https://github.com/<you>/fla-decoder
cd fla-decoder
pip install -e .
```

Runtime dependencies are minimal: just [`olefile`](https://pypi.org/project/olefile/).
The optional research scripts (`research/`) need `pefile` and `capstone`.

## Usage

### Decode shapes to SVG

```bash
# Single FLA, all symbols -> SVG (+ optional PNG contact sheet if
# rsvg-convert and ImageMagick are on PATH).
python scripts/decode.py path/to/file.fla output_dir/

# Whole directory of FLAs.
python scripts/decode.py path/to/fla_dir/ output_dir/
```

### Extract everything to JSON

```bash
python scripts/extract_all.py path/to/file.fla output.json
```

Outputs a single JSON with all extractable data: shapes, library table,
publish settings, text content, scripts, timeline frames, layer metadata,
background color, frame rate, IK bones, and motion tweens.

### Extract audio + bitmaps

```bash
python scripts/extract_media.py path/to/file.fla output_dir/
```

### Extract library table + timeline

```bash
python scripts/extract_library.py path/to/fla_dir/ library.json
```

### Audit unrendered symbols

```bash
python scripts/audit.py path/to/fla_dir/ audit.json
```

### Programmatic API

```python
import olefile
from fla_decoder import decoder, to_svg

ole = olefile.OleFileIO('myfile.fla')
data = ole.openstream('Symbol 17').read()
result = decoder.decode_symbol_stream(data)
shapes = to_svg.find_nonempty_shapes_in_result(result)
to_svg.shape_to_svg(shapes[0], 'out.svg', apply_matrix=True, all_shapes=shapes)
```

## How it works

The full reverse-engineering story is in [`this blog post`](https://eddiemoore.dev/blog/cracking-the-pre-cs5-binary-fla);
the format spec is in [`docs/FORMAT.md`](docs/FORMAT.md); the raw
RE journey log is in [`docs/RE_JOURNEY.md`](docs/RE_JOURNEY.md). For
remaining gaps and Ghidra VAs, see
[`docs/OPEN_PROBLEMS.md`](docs/OPEN_PROBLEMS.md).

The TL;DR:

1. The FLA is an OLE2 compound document with `Contents`, `Page N`, `Symbol N`,
   and `Media N` streams (CS4+ uses `S N`/`P N`/`M N` + timestamp).
2. Inside each stream, the bytes are an MFC `CArchive` serialization of an
   object tree (`CPicPage` → `CPicLayer` → `CPicFrame` → `CPicShape` → ...).
3. MFC encodes class identity using "tags": `0xFFFF`+name for new classes,
   `0x8000|idx` for back-references.
4. Each class's `Serialize` method writes its fields in a fixed order;
   we found those orderings by disassembling Flash 8's `flash.exe` in
   Ghidra (slot 2 of the **primary vtable** is `Serialize` — not slot 4).
5. Shape geometry uses a custom byte-encoded edge loop with four delta
   types (zero, s16, s32, and `s16 << 7`) and a "ultra-twip" coordinate
   unit where 1 pixel = 2560 units.
6. CS4 adds IK bone data and motion tweens as embedded XML strings
   (`<BridgeTree>`, `<AnimationCore>`) within the binary MFC stream.
7. The `Contents` stream holds the document DOM: library table (symbol
   names, types, timestamps), folder hierarchy, publish settings,
   background color, frame rate, and color palette.

## Credits and prior art

- [JPEXS Free Flash Decompiler](https://github.com/jindrapetrik/jpexs-decompiler)
  — invaluable reference for the `LosslessImageBinDataReader` and overall
  Flash format knowledge.
- [Ruffle](https://github.com/ruffle-rs/ruffle) — open-source SWF runtime;
  its shape rendering helped sanity-check the coordinate unit.
- [fla-viewer](https://github.com/lifeart/fla-viewer) — handles the
  post-CS5 XFL/zip format.
- The Ghidra disassembly was driven by Claude Code (Anthropic).

## License

MIT — see [LICENSE](LICENSE).

The decoder was built by reverse-engineering a freely-distributed Macromedia
Flash Professional 8 binary (acquired from archive.org). No proprietary
source code or assets are included in this repository.

## Legal & Disclaimers

This is independently-developed software for **interoperability** with the
binary FLA file format. The decoder code is original Python; no Adobe source
code is included or redistributed.

The reverse engineering relied on disassembly of a publicly-distributed
Macromedia Flash Professional 8 binary (`flash.exe`). That binary is **not
included in this repository** — contributors who want to extend the decoder
must obtain their own copy. See [`research/README.md`](research/README.md)
for guidance.

File formats are not subject to copyright (cf. *Sega Enterprises Ltd. v.
Accolade, Inc.*, 977 F.2d 1510 (9th Cir. 1992); *Google LLC v. Oracle
America, Inc.*, 593 U.S. ___ (2021)). Reverse engineering for
interoperability is protected by **DMCA §1201(f)** in the US and by
**Article 6 of the EU Software Directive 2009/24/EC** in the EU. FLA files
have no DRM or technological protection measure, so DMCA §1201(a)
anti-circumvention provisions don't apply.

Adobe Flash was end-of-life as of December 31, 2020. This project exists
to make binary FLA files (Flash 5 through CS6) readable by people who own them. Comparable
projects ([JPEXS Free Flash Decompiler](https://github.com/jindrapetrik/jpexs-decompiler),
[Ruffle](https://github.com/ruffle-rs/ruffle)) have operated openly in
this space for over a decade.

This README is not legal advice. The MIT license's "as is, no warranty"
terms apply.

## Trademarks

"Adobe", "Flash", "Macromedia", and "Flash Professional" are trademarks
of Adobe Inc. They appear in this repository nominatively to describe
the file format being decoded. This project is not affiliated with,
endorsed by, or sponsored by Adobe Inc.
