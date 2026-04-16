#!/usr/bin/env python3
r"""
For each CRuntimeClass in flash.exe:

1. Disassemble CreateObject -> find the constructor called.
2. Disassemble the constructor -> find the vtable pointer (mov [eax]/[ecx],
   imm32 at the start).
3. From the vtable, extract the Serialize slot. MFC CObject's primary
   vtable layout (VS 2003, 32-bit x86):

     +0x00  virtual ~CObject()
     +0x04  virtual void AssertValid() const
     +0x08  virtual void Dump(CDumpContext&) const
     +0x0c  virtual BOOL IsKindOf(const CRuntimeClass*) const
     +0x10  virtual void Serialize(CArchive&)              <-- our target

   Note: the destructor is normally stored as `vector deleting destructor`,
   so destructor + Serialize sit at slots 0 and 4 of the *primary* vtable.
   Multi-inheritance classes have additional secondary vtables.

4. Print all (class_name, vtable_va, serialize_va) triples.

Usage:
    python research/find_serialize.py <flash.exe> <runtime_classes.json> [out.json]

Where runtime_classes.json comes from `decode_runtime_classes.py`.
"""
import json, pefile, struct, sys
from capstone import Cs, CS_ARCH_X86, CS_MODE_32


def main():
    if len(sys.argv) < 3:
        sys.exit('usage: find_serialize.py <flash.exe> <runtime_classes.json> [out.json]')
    pe_path = sys.argv[1]
    rtc_json = sys.argv[2]
    out_json = sys.argv[3] if len(sys.argv) > 3 else None

    pe = pefile.PE(pe_path, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    full = open(pe_path, 'rb').read()

    def va_to_off(va):
        rva = va - base
        for s in pe.sections:
            if s.VirtualAddress <= rva < s.VirtualAddress + s.Misc_VirtualSize:
                return s.PointerToRawData + (rva - s.VirtualAddress)
        return None

    def read_dword(va):
        off = va_to_off(va)
        return struct.unpack_from('<I', full, off)[0] if off is not None else None

    cs = Cs(CS_ARCH_X86, CS_MODE_32)
    cs.detail = True

    classes = json.load(open(rtc_json))

    # Compute .rdata range so we can sanity-check vtable VAs.
    rdata = next((s for s in pe.sections if s.Name.startswith(b'.rdata')), None)
    if rdata is None:
        sys.exit('no .rdata section found')
    rdata_lo = base + rdata.VirtualAddress
    rdata_hi = rdata_lo + rdata.Misc_VirtualSize

    def disasm_fn(entry_va, max_insn=60):
        off = va_to_off(entry_va)
        if off is None: return []
        return list(cs.disasm(full[off:off+512], entry_va))[:max_insn]

    def find_ctor_in_create(create_va):
        """CreateObject calls operator new, then the ctor. Last call is the ctor."""
        insns = disasm_fn(create_va, max_insn=40)
        calls = [int(ins.op_str, 16) for ins in insns
                 if ins.mnemonic == 'call' and ins.op_str.startswith('0x')]
        # Skip the very first call (allocator).
        if len(calls) < 2: return None
        return calls[1]

    def find_vtable_in_ctor(ctor_va):
        """Ctor writes the vtable pointer via `mov dword ptr [reg], imm32`."""
        insns = disasm_fn(ctor_va, max_insn=60)
        for ins in insns:
            if ins.mnemonic != 'mov': continue
            op_str = ins.op_str
            if 'dword ptr [' in op_str or '[' in op_str.split(',')[0]:
                parts = [p.strip() for p in op_str.split(',')]
                if len(parts) == 2 and parts[1].startswith('0x'):
                    imm = int(parts[1], 16)
                    if rdata_lo <= imm < rdata_hi:
                        return imm
        return None

    def read_vtable(vtable_va, nslots=8):
        return [read_dword(vtable_va + 4*i) for i in range(nslots)]

    print(f'{"class":<24} {"create":>10} {"ctor":>10} {"vtable":>10} {"serialize":>10}')
    print('-' * 90)
    results = {}
    for va_str, r in sorted(classes.items(), key=lambda kv: kv[1]['name']):
        nm = r['name']
        if not (nm.startswith('CPic') or nm.startswith('CMedia')
                or nm == 'CDocumentPage' or nm.startswith('CMorph')
                or nm == 'CColorDef'):
            continue
        create = r['create_fn']
        ctor = find_ctor_in_create(create)
        vt = find_vtable_in_ctor(ctor) if ctor else None
        slots = read_vtable(vt, 8) if vt else None
        serialize = slots[4] if slots else None  # slot 4 = +0x10 = Serialize
        results[nm] = dict(
            create=create, ctor=ctor, vtable=vt, serialize=serialize,
            slots=[hex(s) if s else None for s in (slots or [])])
        print(f'{nm:<24} '
              f'0x{create:08x} '
              f'{"0x%08x" % ctor if ctor else "        -":>10} '
              f'{"0x%08x" % vt if vt else "        -":>10} '
              f'{"0x%08x" % serialize if serialize else "        -":>10}')

    if out_json:
        with open(out_json, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f'\nSaved to {out_json}')


if __name__ == '__main__':
    main()
