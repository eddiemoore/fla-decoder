r"""
Extract binary-FLA lossless bitmaps from Media streams.

Format (reverse-engineered from JPEXS's LosslessImageBinDataReader.java):

  u8  = 0x03   signature byte 1
  u8  = 0x05   signature byte 2
  u16 LE       rowSize
  u16 LE       width
  u16 LE       height
  u32 LE       frameLeft
  u32 LE       frameRight
  u32 LE       frameTop
  u32 LE       frameBottom
  u8           flags (bit0 = hasAlpha)
  u8           variant (1 = compressed in chunks)
  [u16 chunkLen, bytes] ...   (chunked zlib stream, terminated by chunkLen=0)

After zlib inflate:
  for each y:
    for each x:
      u8: alpha (premultiplied; 0 or 255 are literal, else store+1 semantics)
      u8: blue
      u8: green
      u8: red

We undo the premultiplication (divide channels by alpha when 0 < alpha < 255)
and emit a standard PNG.
"""
from __future__ import annotations
import olefile, zlib, struct, sys, os
from pathlib import Path
from io import BytesIO

def read_u8(f):  return f.read(1)[0]
def read_u16(f): return struct.unpack('<H', f.read(2))[0]
def read_u32(f): return struct.unpack('<I', f.read(4))[0]

def decode_lossless(data: bytes) -> tuple[int, int, bytes] | None:
    if len(data) < 21 or data[0] != 0x03 or data[1] != 0x05:
        return None
    f = BytesIO(data)
    f.read(2)  # signature
    _rowSize = read_u16(f)
    width    = read_u16(f)
    height   = read_u16(f)
    _fl = read_u32(f); _fr = read_u32(f); _ft = read_u32(f); _fb = read_u32(f)
    flags   = read_u8(f)
    _hasAlpha = (flags & 1) == 1
    variant = read_u8(f)
    # Read chunked data
    raw = bytearray()
    if variant == 1:
        while True:
            cl = read_u16(f)
            if cl == 0: break
            raw += f.read(cl)
    else:
        raw = data[f.tell():]
    # Inflate
    try:
        inflated = zlib.decompress(bytes(raw))
    except Exception as e:
        return None
    if len(inflated) < width * height * 4:
        return None
    # Reorder ABGR (premultiplied alpha) -> straight RGBA, undoing premult.
    out = bytearray(width * height * 4)
    p = 0; o = 0
    for _ in range(width * height):
        a = inflated[p];   b = inflated[p+1]; g = inflated[p+2]; r = inflated[p+3]
        p += 4
        if 0 < a < 255:
            a1 = a - 1
            if a1 > 0:
                r = min(255, (r * 256) // a1)
                g = min(255, (g * 256) // a1)
                b = min(255, (b * 256) // a1)
            a = a1
        out[o] = r; out[o+1] = g; out[o+2] = b; out[o+3] = a
        o += 4
    return (width, height, bytes(out))

def png_from_rgba(w: int, h: int, rgba: bytes) -> bytes:
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    raw = b''.join(b'\x00' + rgba[y*w*4:(y+1)*w*4] for y in range(h))
    idat = zlib.compress(raw, 9)
    return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

def extract(fla_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    ole = olefile.OleFileIO(str(fla_path))
    n = 0
    for s in sorted(ole.listdir(streams=True)):
        name = '/'.join(s)
        if not name.startswith('Media '): continue
        data = ole.openstream(name).read()
        res = decode_lossless(data)
        if res is None: continue
        w, h, rgba = res
        mid = name.split()[1]
        png = png_from_rgba(w, h, rgba)
        (out_dir / f'Media_{int(mid):02d}_{w}x{h}.png').write_bytes(png)
        print(f'  {fla_path.name} Media {mid}: {w}x{h} -> PNG ({len(png):,} B)')
        n += 1
    ole.close()
    return n

def main():
    if len(sys.argv) < 3:
        sys.exit('usage: fla_extract_lossless.py <dir_or_fla> <out_dir>')
    src = Path(sys.argv[1]); out = Path(sys.argv[2])
    paths = [src] if src.is_file() else sorted(p for p in src.iterdir() if p.suffix == '.fla')
    total = 0
    for p in paths:
        sub = out / p.stem
        total += extract(p, sub)
    print(f'\nTotal images extracted: {total}')

if __name__ == '__main__':
    main()
