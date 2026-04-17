#!/usr/bin/env python3
r"""
Extract everything possible from a binary FLA file into a single JSON.

Outputs: shapes, audio, text, scripts, timeline, library, settings, layers.

Usage:
    python scripts/extract_all.py <file.fla> <out.json>
"""
from __future__ import annotations
import olefile, struct, re, json, sys, os
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fla_decoder import decoder, to_svg


def extract_all(fla_path: str) -> dict:
    ole = olefile.OleFileIO(fla_path)
    result = {
        'file': os.path.basename(fla_path),
        'file_size': os.path.getsize(fla_path),
    }

    # ── Contents stream ──────────────────────────────────────────────
    if ole.exists('Contents'):
        contents = ole.openstream('Contents').read()
        result['contents_size'] = len(contents)

        # Library table with types and timestamps
        library = {}
        type_names = {0: 'graphic', 1: 'button', 2: 'movieclip'}
        symbol_prefix = b'S\x00y\x00m\x00b\x00o\x00l\x00 \x00'
        pos = 0
        while pos < len(contents):
            idx = contents.find(symbol_prefix, pos)
            if idx < 0:
                break
            str_len = contents[idx - 1] if idx > 0 else 0
            str_end = idx + str_len * 2
            s = contents[idx:str_end].decode('utf-16le', 'replace') if str_len > 0 else ''
            m = re.match(r'Symbol (\d+)', s)
            if m:
                sym_num = int(m.group(1))
                search = str_end
                while search < min(len(contents) - 4, str_end + 100):
                    if contents[search:search + 3] == b'\xff\xfe\xff':
                        ln = contents[search + 3]
                        if ln > 0 and search + 4 + ln * 2 <= len(contents):
                            name = contents[search + 4:search + 4 + ln * 2].decode('utf-16le', 'replace')
                            if '/' not in name and not name.startswith('.\\'):
                                name_end = search + 4 + ln * 2
                                entry = {'name': name}
                                if name_end + 5 <= len(contents):
                                    entry['type'] = type_names.get(contents[name_end + 4], 'unknown')
                                # Find timestamp
                                p = name_end + 5
                                if p + 3 < len(contents) and contents[p:p + 3] == b'\xff\xfe\xff':
                                    lln = contents[p + 3]
                                    p += 4 + lln * 2
                                for i in range(8):
                                    if p + 4 <= len(contents):
                                        v = struct.unpack_from('<I', contents, p)[0]
                                        if 1100000000 < v < 1500000000:
                                            entry['modified'] = datetime.fromtimestamp(
                                                v, tz=timezone.utc).isoformat()
                                            break
                                        p += 4
                                library[sym_num] = entry
                                break
                    search += 1
            pos = idx + 1
        result['library'] = library

        # Folders
        folders = set()
        fpos = 0
        while fpos < len(contents) - 4:
            if contents[fpos:fpos + 3] == b'\xff\xfe\xff':
                fln = contents[fpos + 3]
                fend = fpos + 4 + fln * 2
                if fln > 0 and fend <= len(contents):
                    fs = contents[fpos + 4:fend].decode('utf-16le', 'replace')
                    if fs.startswith('Folder '):
                        folders.add(fs[7:])
                fpos = fend
            else:
                fpos += 1
        result['folders'] = sorted(folders)

        # Publish settings
        strings = []
        spos = 0
        while spos < len(contents) - 4:
            if contents[spos:spos + 3] == b'\xff\xfe\xff':
                sln = contents[spos + 3]
                send = spos + 4 + sln * 2
                if sln > 0 and send <= len(contents):
                    strings.append(contents[spos + 4:send].decode('utf-16le', 'replace'))
                spos = send
            else:
                spos += 1
        settings = {}
        for i in range(len(strings) - 1):
            key, val = strings[i], strings[i + 1]
            if '::' in key and '::' not in val and len(val) < 200:
                settings[key] = val
            if key.endswith('::Width') and 'Html' in key and val.isdigit():
                settings['stage_width'] = int(val)
            if key.endswith('::Height') and 'Html' in key and val.isdigit():
                settings['stage_height'] = int(val)
        result['publish_settings'] = settings

    # ── Symbol streams ───────────────────────────────────────────────
    streams = sorted(int(s[0].split()[1])
                     for s in ole.listdir(streams=True)
                     if s[0].startswith('Symbol '))
    symbols = {}
    total_shapes = 0
    total_edges = 0
    total_recovered = 0

    def find(n, t):
        if isinstance(n, dict):
            if n.get('class') == t:
                return n
            for v in n.values():
                r = find(v, t)
                if r:
                    return r
        elif isinstance(n, list):
            for x in n:
                r = find(x, t)
                if r:
                    return r

    for sid in streams:
        data = ole.openstream(f'Symbol {sid}').read()
        decoded = decoder.decode_symbol_stream(data)
        shapes = to_svg.find_nonempty_shapes_in_result(decoded)

        sym = {
            'stream_bytes': len(data),
            'consumed_bytes': decoded['consumed_bytes'],
            'shape_count': len(shapes),
            'edge_count': sum(len(s.get('shape', {}).get('byte_edges', [])) for s in shapes),
        }

        if sid in result.get('library', {}):
            sym['library_name'] = result['library'][sid]['name']
            sym['symbol_type'] = result['library'][sid].get('type')
            if 'modified' in result['library'][sid]:
                sym['modified'] = result['library'][sid]['modified']

        total_shapes += sym['shape_count']
        total_edges += sym['edge_count']
        total_recovered += len(decoded.get('recovered_shapes', []))

        # Layer metadata
        layer = find(decoded['body'], 'CPicLayer')
        if layer:
            if layer.get('layer_name'):
                sym['layer_name'] = layer['layer_name']
            if layer.get('layer_type') is not None:
                sym['layer_type'] = {0: 'normal', 1: 'guide', 3: 'mask',
                                     4: 'masked', 5: 'folder'}.get(
                    layer['layer_type'], f'type_{layer["layer_type"]}')
            if layer.get('layer_locked'):
                sym['layer_locked'] = True
            if layer.get('layer_visible'):
                sym['layer_outline'] = True

        # Text
        txt = find(decoded['body'], 'CPicText')
        if txt:
            sym['text'] = {}
            if txt.get('text_font_name'):
                sym['text']['font'] = txt['text_font_name']
            if txt.get('text_font_size_twips'):
                sym['text']['size_pt'] = txt['text_font_size_twips'] / 20.0
            if txt.get('text_content'):
                sym['text']['content'] = txt['text_content']
            if txt.get('text_bounds'):
                b = txt['text_bounds']
                sym['text']['bounds_px'] = {
                    'width': (b.get('right', 0) - b.get('left', 0)) / 20.0,
                    'height': (b.get('bottom', 0) - b.get('top', 0)) / 20.0,
                }

        # Sprite
        sp = find(decoded['body'], 'CPicSprite')
        if sp:
            sym['sprite'] = {}
            labels = sp.get('sprite_labels', [])
            frame_labels = [l for l in labels if len(l) < 30
                           and not any(k in l for k in ['{', ';', '(', '='])]
            scripts = [l for l in labels if any(
                k in l for k in ['_root', '_level', 'gotoAnd', 'stop(', 'play(',
                                 'onClipEvent', 'function', 'var ', 'if (', 'objName'])]
            if frame_labels:
                sym['sprite']['frame_labels'] = frame_labels
            if scripts:
                sym['sprite']['script_count'] = len(scripts)
                sym['sprite']['scripts'] = scripts

        # Morph shapes
        morphs = [r for r in decoded.get('recovered_shapes', [])
                  if r.get('class') == 'CPicMorphShape']
        if morphs:
            sym['morph_shapes'] = len(morphs)
            total_coords = sum(
                len(c.get('coords', []))
                for m in morphs
                for c in m.get('morph_children', []))
            sym['morph_coords'] = total_coords

        symbols[sid] = sym

    result['symbols'] = symbols
    result['summary'] = {
        'total_symbols': len(streams),
        'total_shapes': total_shapes,
        'total_edges': total_edges,
        'total_recovered': total_recovered,
    }

    # ── Media streams ────────────────────────────────────────────────
    media = {}
    for s in ole.listdir(streams=True):
        if not s[0].startswith('Media '):
            continue
        mid = int(s[0].split()[1])
        data = ole.openstream(s[0]).read()
        fmt = 'unknown'
        if data[:3] == b'\xff\xd8\xff':
            fmt = 'jpeg'
        elif data[:8] == b'\x89PNG\r\n\x1a\n':
            fmt = 'png'
        elif data[:2] in (b'\xff\xfb', b'\xff\xfa', b'\xff\xf3'):
            fmt = 'mp3'
        elif data[:3] == b'ID3':
            fmt = 'mp3'
        elif data[0:2] == b'\x03\x05':
            fmt = 'lossless'
        else:
            fmt = 'raw-pcm'
        media[mid] = {'format': fmt, 'size': len(data)}
    if media:
        result['media'] = media

    # ── Page streams ─────────────────────────────────────────────────
    pages = {}
    for s in ole.listdir(streams=True):
        if not s[0].startswith('Page '):
            continue
        page_data = ole.openstream(s[0]).read()
        page_decoded = decoder.decode_symbol_stream(page_data)
        page_shapes = to_svg.find_nonempty_shapes_in_result(page_decoded)
        page_info = {
            'stream_bytes': len(page_data),
            'shapes': len(page_shapes),
        }
        layer = find(page_decoded['body'], 'CPicLayer')
        if layer and layer.get('layer_name'):
            page_info['layer'] = layer['layer_name']
        sp = find(page_decoded['body'], 'CPicSprite')
        if sp:
            labels = sp.get('sprite_labels', [])
            frame_labels = [l for l in labels if len(l) < 30
                           and not any(k in l for k in ['{', ';', '(', '='])]
            if frame_labels:
                page_info['frame_labels'] = frame_labels
        pages[s[0]] = page_info
    if pages:
        result['pages'] = pages

    ole.close()
    return result


def main():
    if len(sys.argv) < 3:
        sys.exit('usage: extract_all.py <file.fla> <out.json>')
    fla_path = sys.argv[1]
    out_path = sys.argv[2]
    print(f'Extracting from {os.path.basename(fla_path)}...')
    data = extract_all(fla_path)
    with open(out_path, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    s = data.get('summary', {})
    print(f'  {s.get("total_symbols", 0)} symbols, '
          f'{s.get("total_shapes", 0)} shapes, '
          f'{s.get("total_edges", 0):,} edges')
    print(f'  Library: {len(data.get("library", {}))} items')
    print(f'  Media: {len(data.get("media", {}))} streams')
    print(f'  Settings: {len(data.get("publish_settings", {}))} entries')
    print(f'Saved to {out_path}')


if __name__ == '__main__':
    main()
