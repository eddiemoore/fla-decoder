r"""
Inspect every Symbol stream in a binary FLA: class tree, embedded strings,
ActionScript, text content, numeric fields that look like shape bounds.

The binary shape geometry inside CPicShape is undocumented — this tool
extracts only what's unambiguously readable.

Outputs per symbol a report and a combined JSON inventory.
"""
from __future__ import annotations
import olefile, re, struct, sys, json
from pathlib import Path

CLASS_DEF = re.compile(rb'\xff\xff\x01\x00(.)\x00([A-Z][A-Za-z]+)')
U16STR    = re.compile(rb'\xff\xfe\xff(.)((?:.\x00)*)')  # FLA length-prefixed UTF-16LE

def extract_class_tree(data: bytes) -> list[dict]:
    """Find all class definitions in order, with the raw body offset."""
    classes = []
    for m in CLASS_DEF.finditer(data):
        ln = m.group(1)[0]
        nm = m.group(2)
        if len(nm) == ln and nm.startswith(b'C'):
            classes.append({'name': nm.decode(), 'offset': m.start(), 'body_start': m.end()})
    return classes

def extract_utf16_strings(data: bytes, min_chars: int = 2) -> list[str]:
    """Pull length-prefixed UTF-16LE strings with reasonable content."""
    out = []
    i = 0
    while i < len(data) - 4:
        if data[i:i+3] == b'\xff\xfe\xff':
            ln = data[i+3]
            start = i + 4
            end   = start + ln * 2
            if end <= len(data) and ln >= min_chars:
                try:
                    s = data[start:end].decode('utf-16le')
                    if s and all(0x20 <= ord(c) < 0xff or c in '\r\n\t' for c in s):
                        out.append(s)
                except UnicodeDecodeError:
                    pass
            i = end if ln > 0 else i + 4
        else:
            i += 1
    return out

def extract_ascii_runs(data: bytes, min_len: int = 8) -> list[str]:
    """Find ASCII runs (e.g., ActionScript source)."""
    runs = []
    for m in re.finditer(rb'[\x20-\x7e\t\r\n]{%d,}' % min_len, data):
        s = m.group().decode('ascii', 'replace').strip()
        # Filter out pure-hex/gibberish
        if len(s) >= min_len and any(c.isalpha() for c in s):
            runs.append(s)
    return runs

def find_plausible_bounds(data: bytes) -> list[dict]:
    """
    Scan the stream for 4-element signed-int32 sequences that look like RECT
    bounds in twips (1/20 pixel). Reasonable ranges: [-100k, +100k] twips
    => [-5000, +5000] pixels. Require right>left and bottom>top.
    """
    found = []
    for i in range(0, len(data) - 16, 2):
        try:
            a, b, c, d = struct.unpack_from('<iiii', data, i)
        except struct.error:
            continue
        # Reject common sentinel values (0x10000, 0x10080, 0x8000, etc.)
        bad = (0x10000, 0x10080, 0xffff, -1, 0x8000, -0x8000)
        if any(v in bad for v in (a, b, c, d)):
            continue
        if all(-40000 <= v <= 40000 for v in (a, b, c, d)):
            # Assume (left, right, top, bottom) layout as in XFL
            if b > a and d > c and (b-a) * (d-c) > 1000 and (b-a) < 40000 and (d-c) < 40000:
                found.append({
                    'offset': i,
                    'left_tw': a, 'right_tw': b, 'top_tw': c, 'bottom_tw': d,
                    'width_px': (b-a)/20, 'height_px': (d-c)/20,
                })
    # De-dup overlapping matches (same rect appearing twice due to scan step)
    seen = set(); uniq = []
    for r in found:
        key = (r['left_tw'], r['right_tw'], r['top_tw'], r['bottom_tw'])
        if key not in seen:
            seen.add(key); uniq.append(r)
    return uniq

def inspect_fla(fla_path: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    ole = olefile.OleFileIO(str(fla_path))
    inventory = []
    for s in sorted(ole.listdir(streams=True), key=lambda x: int(x[0].split()[1]) if len(x)==1 and x[0].startswith('Symbol ') else 0):
        name = '/'.join(s)
        if not name.startswith('Symbol '): continue
        sid = int(name.split()[1])
        data = ole.openstream(name).read()
        classes = extract_class_tree(data)
        # Count back-references too (`XX 80` where XX = class index).
        # JPEXS's convention: class index starts at 1 for first class declared,
        # so back-ref `03 80` -> class index 3 -> 3rd declared class in this stream.
        class_order = [c['name'] for c in classes]
        class_instances = {nm: 1 for nm in class_order}
        for m in re.finditer(rb'([\x01-\x40])\x80', data):
            idx = m.group(1)[0]
            if 1 <= idx <= len(class_order):
                class_instances[class_order[idx-1]] = class_instances.get(class_order[idx-1], 0) + 1
        u16 = extract_utf16_strings(data)
        ascii_long = extract_ascii_runs(data, min_len=12)
        # Script detection: ASCII runs containing common AS tokens
        scripts = [r for r in ascii_long
                   if re.search(r'(_root|_parent|stop\(|play\(|gotoAndStop|gotoAndPlay|onClipEvent|function |var |trace\()', r)]
        bounds = find_plausible_bounds(data[:2000])  # only scan near the start
        # Pick the largest one as the shape bounds guess
        main_rect = max(bounds, key=lambda r: r['width_px']*r['height_px']) if bounds else None
        entry = {
            'symbol_id':  sid,
            'stream_bytes': len(data),
            'class_tree': [c['name'] for c in classes],
            'class_counts': class_instances,
            'text_strings': u16,
            'scripts': scripts[:10],
            'guessed_bounds': main_rect,
            'all_bound_candidates': bounds[:5],
        }
        inventory.append(entry)
    ole.close()
    (out_dir / 'symbols.json').write_text(json.dumps(inventory, indent=2))
    # Write a human-readable text report
    lines = []
    lines.append(f'Symbol inventory for {fla_path.name}')
    lines.append('=' * 72)
    for e in inventory:
        lines.append(f'\nSymbol {e["symbol_id"]}  ({e["stream_bytes"]:,} B)  classes: '
                     + '> '.join(e['class_tree']))
        if e['class_counts']:
            cc = ', '.join(f'{n}×{k}' for k, n in e['class_counts'].items())
            lines.append(f'  counts: {cc}')
        if e['guessed_bounds']:
            g = e['guessed_bounds']
            lines.append(f'  guessed bounds: {g["width_px"]:.0f}x{g["height_px"]:.0f} px '
                         f'({g["left_tw"]}..{g["right_tw"]} tw, {g["top_tw"]}..{g["bottom_tw"]} tw)')
        if e['text_strings']:
            for ts in e['text_strings'][:5]:
                lines.append(f'  text: {ts!r}')
        if e['scripts']:
            for sc in e['scripts'][:5]:
                lines.append(f'  script: {sc}')
    (out_dir / 'symbols.txt').write_text('\n'.join(lines) + '\n')
    print(f'Wrote {out_dir / "symbols.json"} and {out_dir / "symbols.txt"}')
    print(f'Inventory: {len(inventory)} symbols')

def main():
    if len(sys.argv) != 3:
        sys.exit('usage: fla_inspect_symbols.py <file.fla> <out_dir>')
    inspect_fla(Path(sys.argv[1]), Path(sys.argv[2]))

if __name__ == '__main__':
    main()
