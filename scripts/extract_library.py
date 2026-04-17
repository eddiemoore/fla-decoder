#!/usr/bin/env python3
r"""
Extract the symbol library table and timeline data from binary FLA files.

For each FLA, outputs:
  - Symbol stream number → library name mapping (from Contents stream)
  - Per-symbol timeline frames with labels, scripts, and char_id references
  - Embedded objects within frames (CPicText, etc.)

Usage:
    python scripts/extract_library.py <file.fla_or_dir> [out.json]
"""
from __future__ import annotations
import olefile, struct, re, json, sys, os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fla_decoder import decoder, to_svg


def extract_library_table(contents: bytes) -> dict[int, str]:
    """Extract symbol number → library name from the Contents stream."""
    symbol_prefix = b'S\x00y\x00m\x00b\x00o\x00l\x00 \x00'
    library = {}
    pos = 0
    while pos < len(contents):
        idx = contents.find(symbol_prefix, pos)
        if idx < 0:
            break
        str_len = contents[idx - 1] if idx > 0 else 0
        s = contents[idx:idx + str_len * 2].decode('utf-16le', 'replace') if str_len > 0 else ''
        m = re.match(r'Symbol (\d+)', s)
        if m:
            sym_num = int(m.group(1))
            str_end = idx + str_len * 2
            search = str_end
            while search < min(len(contents) - 4, str_end + 100):
                if contents[search:search + 3] == b'\xff\xfe\xff':
                    ln = contents[search + 3]
                    if ln > 0 and search + 4 + ln * 2 <= len(contents):
                        name = contents[search + 4:search + 4 + ln * 2].decode('utf-16le', 'replace')
                        if '/' not in name and not name.startswith('.\\'):
                            library[sym_num] = name
                            break
                search += 1
        pos = idx + 1
    return library


def extract_publish_settings(contents: bytes) -> dict:
    """Extract publish settings (key=value pairs) from the Contents stream."""
    strings = []
    pos = 0
    while pos < len(contents) - 4:
        if contents[pos:pos + 3] == b'\xff\xfe\xff':
            ln = contents[pos + 3]
            end = pos + 4 + ln * 2
            if ln > 0 and end <= len(contents):
                s = contents[pos + 4:end].decode('utf-16le', 'replace')
                strings.append(s)
            pos = end
        else:
            pos += 1

    settings = {}
    for i in range(len(strings) - 1):
        key, val = strings[i], strings[i + 1]
        if '::' in key and not ('::' in val):
            section, prop = key.rsplit('::', 1)
            section = section.replace('Properties', '').replace('Publish', '').strip()
            if val and len(val) < 200:
                settings[f'{section}.{prop}'] = val

    # Extract stage dimensions specifically
    for i in range(len(strings) - 1):
        key, val = strings[i], strings[i + 1]
        if key.endswith('::Width') and 'Html' in key and val.isdigit():
            settings['stage_width'] = int(val)
        if key.endswith('::Height') and 'Html' in key and val.isdigit():
            settings['stage_height'] = int(val)
        if key == 'PublishFormatProperties::flashFileName':
            settings['swf_filename'] = val

    return settings


def extract_timeline_frames(data: bytes, start_pos: int = 0) -> list[dict]:
    """Extract per-frame timeline data from a symbol's frame tail."""
    def read_str(data, pos):
        if pos + 4 > len(data) or data[pos:pos + 3] != b'\xff\xfe\xff':
            return None, pos
        ln = data[pos + 3]
        end = pos + 4 + ln * 2
        s = data[pos + 4:end].decode('utf-16le', 'replace') if ln > 0 and end <= len(data) else ''
        return s, end

    frames = []
    pos = start_pos
    frame_num = 0

    while pos < len(data) - 30:
        if data[pos:pos + 3] != b'\xff\xfe\xff':
            pos += 1
            continue

        label, pos = read_str(data, pos)
        if label is None:
            break
        if pos + 16 > len(data):
            break
        f0, f1, f2, f3 = struct.unpack_from('<IIII', data, pos)
        pos += 16

        inst, pos = read_str(data, pos)
        if inst is None:
            break
        if pos + 20 > len(data):
            break
        pos += 20

        script, pos = read_str(data, pos)
        if script is None:
            break

        frame_num += 1
        is_script = any(k in label for k in ['{', ';', '(', '=', 'gotoAnd', 'Math'])

        frame = {'frame': frame_num}
        if is_script:
            frame['type'] = 'script'
            frame['code'] = label
        elif f0 == 4:
            frame['type'] = 'keyframe'
            if label:
                frame['label'] = label
            frame['char_id'] = f2
            frame['depth'] = f1
        elif f0 == 0:
            frame['type'] = 'empty'
        else:
            frame['type'] = 'special'
            if label:
                frame['label'] = label

        if inst:
            frame['instance_name'] = inst
        if script:
            frame['action_script'] = script

        frames.append(frame)

        while pos < len(data) - 4 and data[pos:pos + 3] != b'\xff\xfe\xff':
            pos += 1

    return frames


def process_fla(fla_path: str) -> dict:
    ole = olefile.OleFileIO(fla_path)

    result = {'file': os.path.basename(fla_path), 'symbols': {}}

    if ole.exists('Contents'):
        contents = ole.openstream('Contents').read()
        result['library'] = extract_library_table(contents)
        result['publish_settings'] = extract_publish_settings(contents)
    else:
        result['library'] = {}
        result['publish_settings'] = {}

    streams = sorted(int(s[0].split()[1])
                     for s in ole.listdir(streams=True)
                     if s[0].startswith('Symbol '))

    for sid in streams:
        data = ole.openstream(f'Symbol {sid}').read()
        decoded = decoder.decode_symbol_stream(data)
        shapes = to_svg.find_nonempty_shapes_in_result(decoded)

        sym_info = {
            'stream_bytes': len(data),
            'has_shapes': len(shapes) > 0,
            'shape_count': len(shapes),
        }

        if sid in result['library']:
            sym_info['library_name'] = result['library'][sid]

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

        sp = find(decoded['body'], 'CPicSprite')
        if sp:
            sym_info['type'] = 'sprite'
            sym_info['sprite_labels'] = sp.get('sprite_labels', [])
            sym_info['symbol_schema'] = sp.get('symbol_schema')
            sym_info['sprite_schema'] = sp.get('sprite_schema')

        txt = find(decoded['body'], 'CPicText')
        if txt:
            sym_info['type'] = 'text'
            if txt.get('text_font_name'):
                sym_info['font_name'] = txt['text_font_name']
            if txt.get('text_font_size_twips'):
                sym_info['font_size_twips'] = txt['text_font_size_twips']

        layer = find(decoded['body'], 'CPicLayer')
        if layer and layer.get('layer_name'):
            sym_info['layer_name'] = layer['layer_name']

        result['symbols'][sid] = sym_info

    ole.close()
    return result


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: extract_library.py <file.fla_or_dir> [out.json]')

    src = sys.argv[1]
    out_json = sys.argv[2] if len(sys.argv) > 2 else None

    if os.path.isfile(src):
        paths = [src]
    else:
        paths = sorted(str(p) for p in Path(src).glob('*.fla'))

    results = {}
    for path in paths:
        fname = os.path.basename(path)
        print(f'Processing {fname}...')
        try:
            result = process_fla(path)
            lib = result.get('library', {})
            syms = result.get('symbols', {})
            shapes = sum(1 for s in syms.values() if s.get('has_shapes'))
            print(f'  {len(syms)} symbols, {len(lib)} library names, {shapes} with shapes')
            results[fname] = result
        except Exception as e:
            print(f'  ERROR: {e}')

    if out_json:
        with open(out_json, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f'\nSaved to {out_json}')


if __name__ == '__main__':
    main()
