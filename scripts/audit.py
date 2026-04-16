#!/usr/bin/env python3
r"""
Audit the unrendered symbols in a directory of binary FLAs. Lists the
CPicSprite/CPicText/CPicFrame composition containers we couldn't decode
into shapes, plus any text labels and ActionScript snippets we could pull
out of them so nothing is lost.

Usage:
    python scripts/audit.py <fla_dir> [out_json]
"""
from __future__ import annotations
import olefile, os, sys, re, json
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fla_decoder import decoder, to_svg


def extract_utf16_strings(data: bytes, min_chars: int = 2, max_chars: int = 80) -> list[str]:
    out = []
    for m in re.finditer(rb'\xff\xfe\xff(.)', data):
        ln = m.group(1)[0]
        if ln < min_chars or ln > max_chars: continue
        start = m.start() + 4
        end = start + ln * 2
        if end > len(data): continue
        try:
            s = data[start:end].decode('utf-16le')
            if all(0x20 <= ord(c) < 0xfffe for c in s):
                if s.strip() and s not in ('Layer 1', 'Layer 2'):
                    out.append(s)
        except UnicodeDecodeError:
            pass
    seen = set(); uniq = []
    for s in out:
        if s not in seen:
            seen.add(s); uniq.append(s)
    return uniq


def extract_ascii_strings(data: bytes, min_len: int = 8) -> list[str]:
    out = []
    for m in re.finditer(rb'[\x20-\x7e\t\r\n]{%d,}' % min_len, data):
        s = m.group().decode('ascii', 'replace').strip()
        if any(c.isalpha() for c in s):
            out.append(s)
    seen = set(); uniq = []
    for s in out:
        if s not in seen:
            seen.add(s); uniq.append(s)
    return uniq


def audit(fla_dir: str) -> list[dict]:
    report = []
    for fla in sorted(os.listdir(fla_dir)):
        if not fla.endswith('.fla'): continue
        ole = olefile.OleFileIO(os.path.join(fla_dir, fla))
        streams = sorted(int(s[0].split()[1]) for s in ole.listdir(streams=True)
                         if s[0].startswith('Symbol '))
        for sid in streams:
            data = ole.openstream(f'Symbol {sid}').read()
            try:
                result = decoder.decode_symbol_stream(data)
                if to_svg.find_nonempty_shapes_in_result(result):
                    continue
            except Exception:
                continue
            classes = Counter()
            def walk(n):
                if isinstance(n, dict):
                    if 'class' in n: classes[n['class']] += 1
                    for v in n.values(): walk(v)
                elif isinstance(n, list):
                    for x in n: walk(x)
            walk(result['body'])
            u16 = extract_utf16_strings(data)
            scripts = [s for s in extract_ascii_strings(data) if
                       any(k in s for k in ('_root', '_parent', 'stop(', 'play(',
                                            'gotoAnd', 'function', 'var ',
                                            'onClipEvent'))]
            leaf_class = next((c for c in ('CPicSprite', 'CPicText', 'CPicFrame')
                              if classes.get(c)), '?')
            report.append({
                'fla': fla, 'sid': sid, 'size_bytes': len(data),
                'leaf_class': leaf_class,
                'text_labels': u16[:20],
                'scripts':    scripts[:5],
            })
        ole.close()
    return report


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: audit.py <fla_dir> [out_json]')
    fla_dir = sys.argv[1]
    out_json = sys.argv[2] if len(sys.argv) > 2 else None

    r = audit(fla_dir)
    print(f'# Unrendered symbols audit ({len(r)} items)\n')
    c = Counter(x['leaf_class'] for x in r)
    print(f'By leaf class: {dict(c)}\n')
    for item in r:
        print(f'## {item["fla"]} Symbol {item["sid"]}  ({item["size_bytes"]:,}B, {item["leaf_class"]})')
        if item['text_labels']:
            print(f'   labels: {item["text_labels"]}')
        if item['scripts']:
            for s in item['scripts'][:3]:
                print(f'   script: {s[:100]}')
        print()
    if out_json:
        with open(out_json, 'w') as f:
            json.dump(r, f, indent=2, default=str)
        print(f'\nSaved to {out_json}')


if __name__ == '__main__':
    main()
