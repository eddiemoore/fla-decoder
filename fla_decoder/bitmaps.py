r"""
Scan a binary .fla for embedded bitmap data and write any found images.

Bitmaps in FLA Media streams are stored in one of these forms:

  a) Raw JPEG bytes starting with FFD8 FF
  b) Raw PNG bytes starting with 89 50 4E 47 0D 0A 1A 0A
  c) SWF-style DefineBitsLossless:
       <format:u8> <???> <width:u16_le> <height:u16_le> [<colormap>] <zlib-pixels>
     where format = 3 (8-bit palette), 4 (15-bit RGB), or 5 (32-bit ARGB)
  d) Plain zlib-wrapped JPEG/PNG (rare)

For (c) we locate the zlib stream inside the blob, decompress, and rebuild
a PNG using the declared width/height.

Every Media stream that produces nothing is copied as `Media_<N>.bin` so
you can inspect it. No parsing of Symbol streams here (those hold vector
shape data, not bitmaps).
"""
from __future__ import annotations
import olefile, os, re, struct, sys, zlib
from pathlib import Path

def find_zlib(data: bytes) -> tuple[int, bytes] | None:
    """Find first zlib stream in `data`. Returns (offset, decompressed_bytes) or None."""
    for magic in (b'\x78\x01', b'\x78\x9c', b'\x78\xda'):
        off = 0
        while True:
            i = data.find(magic, off)
            if i < 0: break
            try:
                dec = zlib.decompress(data[i:])
                return (i, dec)
            except zlib.error:
                off = i + 1
                continue
    return None

def try_extract_lossless(data: bytes, stream_name: str) -> bytes | None:
    """Try to interpret `data` as a SWF DefineBitsLossless blob and render PNG."""
    # Scan the first 32 bytes for a plausible (format, width, height) triple
    # immediately followed by a zlib stream.
    import struct as _s
    import io, zlib
    def attempt(fmt_off, w_off, h_off, size_field='u16'):
        fmt = data[fmt_off]
        if fmt not in (3, 4, 5): return None
        unpack = '<H' if size_field == 'u16' else '<I'
        sz = 2 if size_field == 'u16' else 4
        try:
            w = _s.unpack(unpack, data[w_off:w_off+sz])[0]
            h = _s.unpack(unpack, data[h_off:h_off+sz])[0]
        except Exception: return None
        if not (1 <= w <= 8192 and 1 <= h <= 8192): return None
        z = find_zlib(data[h_off+sz:])
        if not z: return None
        off, dec = z
        if fmt == 5:  # 32-bit ARGB
            if len(dec) < w*h*4: return None
            px = dec[:w*h*4]
            # Re-order ARGB -> RGBA for PNG (SWF ARGB is actually ARGB premultiplied,
            # but PNG wants RGBA straight; we do a simple re-order here)
            out = bytearray(len(px))
            for i in range(0, len(px), 4):
                a,r,g,b = px[i],px[i+1],px[i+2],px[i+3]
                out[i:i+4] = bytes((r,g,b,a))
            return png_from_rgba(w, h, bytes(out))
        if fmt == 4:  # 15-bit RGB (1 padding + 5 bits each)
            if len(dec) < w*h*2: return None
            rgba = bytearray()
            for i in range(0, w*h*2, 2):
                v = dec[i] | (dec[i+1] << 8)
                r = (v >> 10) & 0x1f
                g = (v >> 5)  & 0x1f
                b =  v        & 0x1f
                rgba += bytes((r<<3, g<<3, b<<3, 0xff))
            return png_from_rgba(w, h, bytes(rgba))
        if fmt == 3:  # 8-bit palette (TableSize+1 entries of RGBA or RGB)
            # TableSize byte is right before zlib data in SWF. We need to
            # discover it. In our FLA dump, first bytes were:
            #   03 05 d0 07 f4 01 b0 00 ...
            # where 05 may be TableSize. Palette immediately precedes pixels
            # within the zlib stream itself.
            ts = data[fmt_off+1]
            entry = 4  # assume RGBA palette
            pal_bytes = (ts+1) * entry
            if len(dec) < pal_bytes + w*h: return None
            palette = dec[:pal_bytes]
            indices = dec[pal_bytes : pal_bytes + w*h*((ceil4(w))//w)]
            # SWF pads rows to 4-byte boundary
            stride = (w + 3) & ~3
            if len(dec) < pal_bytes + stride*h: return None
            raw = dec[pal_bytes : pal_bytes + stride*h]
            rgba = bytearray()
            for y in range(h):
                row = raw[y*stride:y*stride+w]
                for idx in row:
                    pe = palette[idx*entry:idx*entry+4]
                    if len(pe) == 4:
                        rgba += bytes((pe[1], pe[2], pe[3], pe[0]))  # ARGB->RGBA
                    else:
                        rgba += pe[:3] + b'\xff'
            return png_from_rgba(w, h, bytes(rgba))
        return None
    # Try several plausible header layouts
    for fmt_off, w_off, h_off in [(0, 2, 4), (0, 3, 5), (0, 1, 3), (0, 4, 6)]:
        if w_off + 4 > len(data): continue
        png = attempt(fmt_off, w_off, h_off)
        if png: return png
    return None

def ceil4(n): return (n + 3) & ~3

def png_from_rgba(w: int, h: int, rgba: bytes) -> bytes:
    """Build a minimal PNG from RGBA pixel data (no third-party deps)."""
    import struct, zlib
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    # Each scanline prefixed with filter byte 0
    raw = b''.join(b'\x00' + rgba[y*w*4:(y+1)*w*4] for y in range(h))
    idat = zlib.compress(raw, 9)
    return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

def extract(fla_path: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / 'raw_media'
    raw_dir.mkdir(exist_ok=True)
    ole = olefile.OleFileIO(str(fla_path))
    count_png = count_jpg = count_raw = 0
    for s in sorted(ole.listdir(streams=True)):
        name = '/'.join(s)
        if not name.startswith('Media '): continue
        data = ole.openstream(name).read()
        mid = int(name.split()[1])
        # All-zero stream? Skip the render, but still dump raw.
        if data[:64] == b'\x00' * min(64, len(data)) and all(b == 0 for b in data[:256]):
            (raw_dir / f'Media_{mid:02d}.zeros.bin').write_bytes(data)
            count_raw += 1
            continue
        # Direct JPEG?
        if data[:3] == b'\xff\xd8\xff':
            (out_dir / f'Media_{mid:02d}.jpg').write_bytes(data)
            count_jpg += 1
            print(f'  Media {mid:>3}: raw JPEG ({len(data):,} B)')
            continue
        # Direct PNG?
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            (out_dir / f'Media_{mid:02d}.png').write_bytes(data)
            count_png += 1
            print(f'  Media {mid:>3}: raw PNG  ({len(data):,} B)')
            continue
        # SWF-style DefineBitsLossless?
        png = try_extract_lossless(data, name)
        if png:
            (out_dir / f'Media_{mid:02d}.png').write_bytes(png)
            count_png += 1
            print(f'  Media {mid:>3}: rendered lossless ({len(png):,} B PNG)')
            continue
        # Plain zlib-wrapped JPEG/PNG?
        z = find_zlib(data)
        if z:
            _off, dec = z
            if dec[:3] == b'\xff\xd8\xff':
                (out_dir / f'Media_{mid:02d}.jpg').write_bytes(dec)
                count_jpg += 1
                print(f'  Media {mid:>3}: zlib-wrapped JPEG ({len(dec):,} B)')
                continue
            if dec[:8] == b'\x89PNG\r\n\x1a\n':
                (out_dir / f'Media_{mid:02d}.png').write_bytes(dec)
                count_png += 1
                print(f'  Media {mid:>3}: zlib-wrapped PNG ({len(dec):,} B)')
                continue
        # Unknown -> dump raw for later forensics
        (raw_dir / f'Media_{mid:02d}.bin').write_bytes(data)
        count_raw += 1
        print(f'  Media {mid:>3}: unidentified ({len(data):,} B)  head={data[:16].hex(" ")}')
    ole.close()
    print(f'  -> {count_png} PNG, {count_jpg} JPEG, {count_raw} raw')

def main():
    if len(sys.argv) < 3:
        sys.exit('usage: fla_extract_bitmaps.py <file.fla> <out_dir>')
    extract(Path(sys.argv[1]), Path(sys.argv[2]))

if __name__ == '__main__':
    main()
