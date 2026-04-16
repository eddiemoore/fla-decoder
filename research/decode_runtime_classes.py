#!/usr/bin/env python3
r"""
Decode every CRuntimeClass in flash.exe and dump the inheritance chain.

MFC 7.1 CRuntimeClass layout (VS 2003, 32-bit):
  +0x00  LPCSTR          m_lpszClassName       (ASCII name pointer in .rdata)
  +0x04  int             m_nObjectSize         (sizeof)
  +0x08  UINT            m_wSchema             (DECLARE_SERIAL schema number)
  +0x0c  CreateObject function ptr             (0 if not DECLARE_DYNCREATE)
  +0x10  CRuntimeClass*  m_pBaseClass          (direct pointer in MFC >= 7)
  +0x14  void*           reserved              (observed 0)
  +0x18  CRuntimeClass*  m_pNextClass          (linked-list next)
  +0x1c  AFX_CLASSINIT*  or similar            (observed non-zero)

Usage:
    python research/decode_runtime_classes.py <flash.exe> [out.json]
"""
import pefile, struct, sys, json


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: decode_runtime_classes.py <flash.exe> [out.json]')
    pe_path = sys.argv[1]
    out_json = sys.argv[2] if len(sys.argv) > 2 else None

    pe = pefile.PE(pe_path, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    full = open(pe_path, 'rb').read()

    def va_to_off(va):
        rva = va - base
        for s in pe.sections:
            if s.VirtualAddress <= rva < s.VirtualAddress + s.Misc_VirtualSize:
                return s.PointerToRawData + (rva - s.VirtualAddress)
        return None

    def read_cstring(va, max_len=128):
        off = va_to_off(va)
        if off is None: return None
        end = full.find(b'\x00', off, off + max_len)
        return full[off:end].decode('latin1', 'ignore') if end > off else None

    rtcs = {}
    data_sec = next(s for s in pe.sections if s.Name.startswith(b'.data'))
    data_start_off = data_sec.PointerToRawData
    data_end_off   = data_start_off + data_sec.SizeOfRawData
    for off in range(data_start_off, data_end_off - 28, 4):
        name_ptr, obj_size, schema, create_fn, base_cls, reserved, next_cls, init_fn = \
            struct.unpack_from('<IIIIIIII', full, off)
        name = read_cstring(name_ptr, 80) if name_ptr else None
        if not name: continue
        if not (name.startswith('C') or name.startswith('MFI')): continue
        if not all(c.isalnum() or c == '_' for c in name): continue
        if not (0x20 <= obj_size <= 0x20000): continue
        if schema > 0x1000: continue
        struct_va = base + (off - data_start_off) + data_sec.VirtualAddress
        rtcs[struct_va] = {
            'name': name,
            'name_ptr': name_ptr,
            'obj_size': obj_size,
            'schema': schema,
            'create_fn': create_fn,
            'base_cls': base_cls,
            'reserved': reserved,
            'next_cls': next_cls,
            'init_fn': init_fn,
        }

    print(f'Found {len(rtcs)} plausible CRuntimeClass structs')

    by_va = {va: r for va, r in rtcs.items()}
    print(f'\n{"name":<25} {"size":>6} {"sch":>3}  {"create":>8}  {"base":<25}')
    print('-' * 80)
    for va, r in sorted(rtcs.items(), key=lambda kv: kv[1]['name']):
        base_name = '-'
        if r['base_cls'] in by_va:
            base_name = by_va[r['base_cls']]['name']
        elif r['base_cls']:
            base_name = f'0x{r["base_cls"]:x}'
        if r['name'].startswith(('CPic', 'CMedia', 'CDocument', 'CColor', 'CMorph', 'MFI')):
            print(f'{r["name"]:<25} {r["obj_size"]:>6} {r["schema"]:>3}  '
                  f'0x{r["create_fn"]:08x}  {base_name:<25}')

    if out_json:
        with open(out_json, 'w') as f:
            json.dump({hex(va): r for va, r in rtcs.items()}, f, indent=2, default=str)
        print(f'\nSaved full list to {out_json}')


if __name__ == '__main__':
    main()
