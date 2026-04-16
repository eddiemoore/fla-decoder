# fla-decoder

An open-source decoder for **pre-CS5 binary `.fla`** files (Flash MX through
Flash CS4). These files are Microsoft OLE2 compound documents containing
MFC-serialized object trees — a format that was never publicly documented
and which existing tools (JPEXS, Ruffle, etc.) can read *out* of (when
exporting from `.swf`) but not *in* to.

This project reverse-engineers that format by disassembling Flash 8's
`flash.exe` and reimplements enough of MFC's `CArchive` protocol to walk
the object tree, decode shape geometry, and recover artwork as SVG.

It also extracts embedded audio (PCM/MP3) and bitmaps (JPEG/PNG and the
custom chunked-zlib lossless format).

## Status

On a 9-FLA test corpus (841 symbols total), about **95% of shapes** render
correctly. The remaining ~5% are CPicSprite animation state machines and
empty CPicText/CPicFrame containers, which contain no inline shape data —
their text labels and ActionScript snippets are still recoverable via
`scripts/audit.py`.

This is **not** a 100% solution. It's enough to recover artwork from FLA
files when the original Flash IDE isn't available, but won't reconstruct
timelines or symbol composition perfectly.

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

### Extract audio + bitmaps

```bash
python scripts/extract_media.py path/to/file.fla output_dir/
```

### Audit unrendered symbols

Most "unrendered" symbols still have useful content (frame labels, AS
scripts) — `audit.py` lists them:

```bash
python scripts/audit.py path/to/fla_dir/ audit.json
```

### Inspect a single FLA's symbol inventory

```bash
python scripts/inspect.py path/to/file.fla output_dir/
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
the format spec we derived is in [`docs/FORMAT.md`](docs/FORMAT.md); the raw
RE journey log is in [`docs/RE_JOURNEY.md`](docs/RE_JOURNEY.md). If you want
to contribute, [`docs/OPEN_PROBLEMS.md`](docs/OPEN_PROBLEMS.md) lists what's
still unfinished (timeline data, CPicSprite, CPicText body, etc.) and how
to attack each one.

The TL;DR:

1. The FLA is an OLE2 compound document with `Contents`, `Page N`, `Symbol N`,
   and `Media N` streams.
2. Inside each stream, the bytes are an MFC `CArchive` serialization of an
   object tree (`CPicPage` → `CPicLayer` → `CPicFrame` → `CPicShape` → ...).
3. MFC encodes class identity using "tags": `0xFFFF`+name for new classes,
   `0x8000|idx` for back-references to previously-seen classes in the same
   stream.
4. Each class's `Serialize` method writes its fields in a fixed order;
   we found those orderings by disassembling Flash 8's `flash.exe` in
   Ghidra (the primary vtable's slot 4 is always `Serialize` for any
   `CObject`-derived class).
5. Shape geometry uses a custom byte-encoded edge loop with four delta
   types (zero, s16, s32, and `s16 << 7`) and a "ultra-twip" coordinate
   unit where 1 pixel = 2560 units.

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
to make pre-CS5 FLA files readable by people who own them. Comparable
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
