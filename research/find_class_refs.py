#!/usr/bin/env python3
r"""
Parse Flash 8's flash.exe with pefile to find the file/RVA offsets of class
name strings (e.g. "CPicShape") and any DWORD cross-references to them
(candidate CRuntimeClass structures).

This is one of the discovery scripts from the reverse-engineering effort.
You need a copy of `flash.exe` from a Flash Professional 8 install; pass
its path as the first argument.

Strategy:
  1. Scan sections for the literal bytes of each class name (null-terminated).
  2. For each string RVA, scan the entire binary for DWORD references to
     `image_base + string_rva` (absolute pointer).
  3. Each hit is a candidate CRuntimeClass structure; MFC's layout starts
     with the name pointer.
  4. Read the struct: name_ptr, obj_size, schema, createObj_fn,
     getBaseClass_fn, baseClass_ptr, nextClass_ptr.
  5. `createObj_fn` invoked `operator new` then the class ctor;
     disassembling it reveals the vtable address.

Usage:
    python research/find_class_refs.py <path/to/flash.exe>
"""
import pefile, struct, sys

TARGETS = [
    'CPicShape', 'CPicShapeObj', 'CPicObj', 'CPicPage', 'CPicLayer',
    'CPicFrame', 'CPicText', 'CPicMorphShape', 'CPicBitmap', 'CPicSymbol',
    'CMediaSound', 'CDocumentPage', 'CColorDef',
]


def main():
    if len(sys.argv) != 2:
        sys.exit('usage: find_class_refs.py <flash.exe>')
    pe_path = sys.argv[1]

    pe = pefile.PE(pe_path, fast_load=True)
    image_base = pe.OPTIONAL_HEADER.ImageBase
    print(f'Image base: 0x{image_base:x}')
    print(f'Sections: {len(pe.sections)}')
    for s in pe.sections:
        name = s.Name.rstrip(b'\x00').decode('latin1', 'ignore')
        print(f'  {name:<10}  VA=0x{image_base+s.VirtualAddress:08x}  '
              f'VSize=0x{s.Misc_VirtualSize:x}  FileOff=0x{s.PointerToRawData:x}')

    full = open(pe_path, 'rb').read()

    def off_to_va(off):
        for s in pe.sections:
            if s.PointerToRawData <= off < s.PointerToRawData + s.SizeOfRawData:
                return image_base + s.VirtualAddress + (off - s.PointerToRawData)
        return None

    print('\n=== Class string locations ===')
    for name in TARGETS:
        needle = name.encode() + b'\x00'
        offs = []
        p = 0
        while True:
            i = full.find(needle, p)
            if i < 0: break
            offs.append(i)
            p = i + 1
        for off in offs:
            va = off_to_va(off)
            print(f'  {name:<15} @ file 0x{off:x}  '
                  + (f'VA 0x{va:08x}' if va else '(no VA)'))

    print('\n=== Cross-references to each class name ===')
    for name in TARGETS:
        needle = name.encode() + b'\x00'
        i = full.find(needle)
        if i < 0: continue
        va = off_to_va(i)
        if va is None: continue
        pat = struct.pack('<I', va)
        refs = []
        p = 0
        while True:
            j = full.find(pat, p)
            if j < 0: break
            refs.append(j)
            p = j + 4
        vas = [off_to_va(r) for r in refs]
        print(f'  {name:<15}: string VA 0x{va:x}, {len(refs)} dword refs')
        for r, rva in zip(refs[:5], vas[:5]):
            chunk = full[r:r+32]
            dwords = struct.unpack('<8I', chunk)
            print(f'    ref@file 0x{r:x} VA=0x{rva:08x}:  '
                  f'dwords={[hex(d) for d in dwords]}')


if __name__ == '__main__':
    main()
