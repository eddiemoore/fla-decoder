#!/usr/bin/env python3
r"""
Batch-run the binary-FLA decoder on every .fla in a directory.
For each file, emit a sibling directory with all rendered SVGs (and a
PNG contact sheet if `rsvg-convert` and ImageMagick `magick` are on PATH).

Usage:
    python -m scripts.decode <fla_dir> <out_dir>
    python scripts/decode.py <fla_dir> <out_dir>
"""
from __future__ import annotations
import olefile, os, sys, subprocess, json
from collections import Counter

# Allow `python scripts/decode.py ...` to find the package without `pip install`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fla_decoder import decoder, to_svg


def process_fla(fla_path: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    ole = olefile.OleFileIO(fla_path)
    streams = sorted([int(s[0].split()[1])
                      for s in ole.listdir(streams=True)
                      if s[0].startswith('Symbol ')])
    counts = Counter(); rendered = []
    for sid in streams:
        data = ole.openstream(f'Symbol {sid}').read()
        try:
            result = decoder.decode_symbol_stream(data)
            shapes = to_svg.find_nonempty_shapes_in_result(result)
            if not shapes:
                all_shapes = to_svg.find_all_shapes(result['body'])
                counts['empty' if all_shapes else 'no_shape'] += 1
                continue
            svg_path = f'{out_dir}/Symbol_{sid:03d}.svg'
            to_svg.shape_to_svg(shapes[0], svg_path, apply_matrix=True, all_shapes=shapes)
            rendered.append(sid); counts['ok'] += 1
            if result.get('recovered_shapes'):
                counts['recovered'] += 1
        except Exception as e:
            counts[f'err_{type(e).__name__}'] += 1
    ole.close()
    # Optional: render PNGs + contact sheet if tools are available.
    if _which('rsvg-convert'):
        for svg in [f for f in os.listdir(out_dir) if f.endswith('.svg')]:
            subprocess.run(['rsvg-convert', '-o', svg.replace('.svg', '.png'), svg],
                           cwd=out_dir, stderr=subprocess.DEVNULL)
        pngs = sorted(f for f in os.listdir(out_dir)
                      if f.startswith('Symbol_') and f.endswith('.png'))
        if pngs and _which('magick'):
            subprocess.run(['magick', 'montage', '-geometry', '120x160+3+3',
                            '-tile', '8x', *pngs, '_contact_sheet.png'],
                           cwd=out_dir, stderr=subprocess.DEVNULL)
    return {'fla': os.path.basename(fla_path),
            'total_symbols': len(streams),
            'rendered': len(rendered),
            'counts': dict(counts)}


def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def main():
    if len(sys.argv) != 3:
        sys.exit('usage: decode.py <fla_dir_or_file> <out_dir>')
    src = sys.argv[1]; base_out = sys.argv[2]
    if os.path.isfile(src):
        files = [src]
        src_dir = os.path.dirname(src) or '.'
    else:
        src_dir = src
        files = [os.path.join(src_dir, fn) for fn in sorted(os.listdir(src_dir))
                 if fn.endswith('.fla')]
    reports = []
    for path in files:
        fn = os.path.basename(path)
        stem = os.path.splitext(fn)[0]
        out = os.path.join(base_out, stem)
        print(f'\n=== {fn} ===')
        try:
            r = process_fla(path, out)
            print(f'  {r["rendered"]}/{r["total_symbols"]} rendered  {r["counts"]}')
            reports.append(r)
        except Exception as e:
            print(f'  ERROR: {e}')
            reports.append({'fla': fn, 'error': str(e)})
    os.makedirs(base_out, exist_ok=True)
    with open(os.path.join(base_out, '_summary.json'), 'w') as f:
        json.dump(reports, f, indent=2, default=str)
    total_rendered = sum(r.get('rendered', 0) for r in reports)
    total_symbols = sum(r.get('total_symbols', 0) for r in reports)
    pct = 100 * total_rendered / max(1, total_symbols)
    print(f'\n=== OVERALL: {total_rendered}/{total_symbols} symbols rendered '
          f'({pct:.0f}%) across {len(reports)} FLAs ===')


if __name__ == '__main__':
    main()
