#!/usr/bin/env python3
r"""
Extract uncompressed audio from a pre-CS5 binary Flash .fla file.

The FLA is an OLE2 compound document. Each imported sound is stored as a
`Media N` stream (raw sample bytes) with a `CMediaSound` metadata record
inside the `Contents` stream that names it and records sample count +
a one-byte sample-rate tag.

Discovered layout (by observation of FLAs from a 2005-era Flash 8 project):
  00 0b 00  "CMediaSound"               (length-prefixed ASCII classname)
  06 08
  <utf16_string>  display filename (e.g. "pickUp0.wav")
  <utf16_string>  import path     (e.g. ".\Sound\pickUp0.wav")
  <u32 tstamp> 06 00 00 00 01 00 00 00
  <u32 tstamp> <u32 ?> 01 00 00 00
  <u32 tstamp> <u32 data-size-ish> 00 00 00 00
  07 <hasLinkage:u8> 00 00 00
  <utf16_string>  AS linkage id   (present iff hasLinkage==1)
  ff fe ff 00 ff fe ff 00 05 02 00 00 00
  ff fe ff 00 ff fe ff 00
  <zero fill>
  01 00 00 00 00 00 00 00 ff ff ff ff 00 ff fe ff 00
  00 01 00 00 00 0a <rateTag:u8> 00 <sampleCount:u24_le> 03 00 0e 00

  rateTag observed: 0x0e -> 22050 Hz, 0x0f -> 44100 Hz.
  All streams observed satisfy  stream_size == 2 * sampleCount  => 16-bit mono PCM.
  MP3-imported sounds (filename ends ".mp3") have raw MP3 frames in the stream
  and are written without a WAV wrapper.

The parser walks Contents left-to-right looking for the "CMediaSound" marker
and consumes one record per occurrence.

Outputs:
  audio/wav/<name>.wav            named PCM sounds wrapped as WAV
  audio/mp3/<name>.mp3            natively-mp3 sounds, raw frames
  audio/unknown/Media_<N>.bin     streams with no CMediaSound metadata
  audio/inventory.tsv             tab-separated manifest
"""
from __future__ import annotations
import olefile, struct, os, sys, re, wave
from pathlib import Path

# ---- UTF-16 length-prefixed string helper ---------------------------------
# The FLA encoding is: FF FE FF <len:u8> <len utf16le code units>.
# (Some strings use a longer length; they all start with FF FE FF though.)

def read_u16str(buf: bytes, off: int) -> tuple[str, int]:
    """Read a `FF FE FF <len> <u16 chars>` string starting at off. Returns (str, new_off)."""
    if buf[off:off+3] != b'\xff\xfe\xff':
        raise ValueError(f'not a u16str at 0x{off:x}: {buf[off:off+4].hex()}')
    ln = buf[off+3]
    start = off + 4
    end = start + ln * 2
    return buf[start:end].decode('utf-16le', 'replace'), end

def next_u16str(buf: bytes, off: int, max_skip: int = 8) -> tuple[str, int]:
    """Skip at most max_skip bytes forward to the next u16str marker, then read it."""
    for i in range(max_skip + 1):
        if buf[off+i:off+i+3] == b'\xff\xfe\xff':
            return read_u16str(buf, off+i)
    raise ValueError(f'no u16str within {max_skip} bytes of 0x{off:x}: {buf[off:off+max_skip].hex()}')

def sanitize(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._\- ]+', '_', name).strip() or 'unnamed'

# ---- Sound-record parser ---------------------------------------------------

# The first sound record declares the class as `00 0b 00 "CMediaSound"`; every
# subsequent record uses the compact back-reference `03 80`. Both are followed
# by `06 08` and then a bare-UTF-16 `Media N` label. We anchor on the label.
MEDIA_LABEL_RE = re.compile(rb'(?:\x00\x0b\x00CMediaSound|\x03\x80)\x06\x08'
                            rb'(M\x00e\x00d\x00i\x00a\x00 \x00(?:\d\x00){1,3})')

def parse_sound_records(contents: bytes):
    for m in MEDIA_LABEL_RE.finditer(contents):
        idx = m.start()
        media_label = m.group(1).decode('utf-16le')
        p = m.end()
        try:
            filename,   p = next_u16str(contents, p)
            path,       p = next_u16str(contents, p)
            # three (timestamp, value, tag) triples:  u32 u32 u32   u32 u32 u32   u32 u32 u32
            t1, v1a, v1b = struct.unpack_from('<III', contents, p); p += 12
            t2, v2a, v2b = struct.unpack_from('<III', contents, p); p += 12
            t3, v3a, v3b = struct.unpack_from('<III', contents, p); p += 12
            # 07 <hasLinkage:u8> 00 00 00
            tag_07 = contents[p]; has_linkage = contents[p+1]; p += 5
            linkage = ''
            if has_linkage == 1:
                linkage, p = next_u16str(contents, p)
            # The record has a `0a <rate_tag:u8> 00 <samples:u32_le>` block.
            # In some records it's followed by `03 00 0e 00` (when linkage is
            # set); in others by `ff ff ff ff` (no linkage). Don't match on the
            # trailer — collect every `0a XX 00 <u32>` candidate in the next
            # 256 bytes and let extract() pick the one whose sample count
            # cleanly divides the Media stream size.
            window = contents[p:p+256]
            candidates = [
                (mm.group(1)[0], struct.unpack('<I', mm.group(2))[0])
                for mm in re.finditer(rb'\x0a(.)\x00(....)', window, re.DOTALL)
            ]
            rate_tag = sample_count = None
            if candidates:
                rate_tag, sample_count = candidates[0]
            yield {
                'media_label': media_label,
                'filename':    filename,
                'path':        path,
                'linkage':     linkage,
                'sample_count': sample_count,
                'rate_tag':    rate_tag,
                'candidates':  candidates,
                'record_off':  idx,
            }
        except Exception as e:
            sys.stderr.write(f'parse error at record 0x{idx:x} ({media_label}): {e}\n')

# ---- Rate-tag mapping ------------------------------------------------------
# The `0a <rate_tag> 00` byte pair's second byte observed in this FLA:
#   0x0e -> 44100 Hz (all voice/SFX; verified by ear: 22050 plays at half-pitch)
#   0x0f -> 44100 Hz stereo music (SmallPiano)
# Both tags seem to mean 44.1 kHz here; the difference may be channel-related
# or a Flash-internal setting. Other values reserved as guesses.
RATE_TABLE = {
    0x0a: 22050,
    0x0e: 44100,
    0x0f: 44100,
}

# ---- Main ------------------------------------------------------------------

def extract(fla_path: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'wav').mkdir(exist_ok=True)
    (out_dir / 'mp3').mkdir(exist_ok=True)
    (out_dir / 'unknown').mkdir(exist_ok=True)

    ole = olefile.OleFileIO(str(fla_path))
    contents = ole.openstream('Contents').read()

    # Gather all Media streams
    media_streams = {}
    for s in ole.listdir(streams=True):
        name = '/'.join(s)
        m = re.fullmatch(r'Media (\d+)', name)
        if m:
            media_streams[int(m.group(1))] = ole.openstream(name).read()

    manifest_lines = ['media_id\tfilename\tlinkage\tbytes\tsamples\trate\tchannels\tformat\toutput']
    claimed = set()

    for rec in parse_sound_records(contents):
        label = rec['media_label']
        if not label:
            sys.stderr.write(f'  (no Media label for record at 0x{rec["record_off"]:x})\n')
            continue
        mid = int(label.split()[1])
        if mid not in media_streams:
            sys.stderr.write(f'  {label}: referenced but stream missing\n')
            continue
        data = media_streams[mid]
        claimed.add(mid)
        fn = rec['filename']
        link = rec['linkage']
        base = sanitize(link or Path(fn).stem)
        is_mp3 = fn.lower().endswith('.mp3')
        if is_mp3:
            out = out_dir / 'mp3' / f'{base}.mp3'
            out.write_bytes(data)
            manifest_lines.append(
                f'{mid}\t{fn}\t{link}\t{len(data)}\t-\t-\t-\tmp3\t{out.relative_to(out_dir)}')
            print(f'  Media {mid:>3} -> {out.name}  (MP3 {len(data):,} B)')
            continue
        # PCM 16-bit. sample_count in the record is samples-per-channel.
        # Channel count = len(data) / (2 * samples_per_channel).
        # Pick the (rate_tag, samples) candidate whose size ratio is exactly 2 or 4.
        cnt = rec['sample_count']
        rate_tag = rec['rate_tag']
        channels = 1
        chosen = None
        for rt, c in rec['candidates']:
            if c <= 0:
                continue
            ratio = len(data) / (2 * c)
            if abs(ratio - 1) < 0.01:
                chosen, channels = (rt, c), 1; break
            if abs(ratio - 2) < 0.01:
                chosen, channels = (rt, c), 2; break
        if chosen:
            rate_tag, cnt = chosen
        elif cnt:
            sys.stderr.write(
                f'  Media {mid} {fn}: no candidate matches size={len(data)}; '
                f'candidates={rec["candidates"][:4]}; defaulting to mono\n')
        rate = RATE_TABLE.get(rate_tag, 44100)
        out = out_dir / 'wav' / f'{base}.wav'
        with wave.open(str(out), 'wb') as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframesraw(data)
        manifest_lines.append(
            f'{mid}\t{fn}\t{link}\t{len(data)}\t{cnt}\t{rate}\t{channels}\tpcm_s16le\t{out.relative_to(out_dir)}')
        dur = (cnt / rate) if cnt else len(data) / (2 * channels * rate)
        print(f'  Media {mid:>3} -> {out.name}  '
              f'(PCM {"stereo" if channels==2 else "mono"} 16-bit @ {rate} Hz, {dur:.2f}s)')

    # Dump un-claimed Media streams
    for mid, data in sorted(media_streams.items()):
        if mid in claimed:
            continue
        out = out_dir / 'unknown' / f'Media_{mid:02d}.bin'
        out.write_bytes(data)
        head = data[:8].hex(' ')
        # Sniff a few common magics
        fmt = 'unknown'
        if data[:2] in (b'\xff\xfb', b'\xff\xfa', b'\xff\xf3'): fmt = 'mp3-like'
        elif data[:3] == b'\xff\xd8\xff':                     fmt = 'jpeg'
        elif data[:8] == b'\x89PNG\r\n\x1a\n':                fmt = 'png'
        elif data[:4] == b'FLV\x01':                          fmt = 'flv'
        elif data[:4] == b'RIFF':                             fmt = 'riff/wav'
        manifest_lines.append(f'{mid}\t-\t-\t{len(data)}\t-\t-\t-\t{fmt}\t{out.relative_to(out_dir)}')
        print(f'  Media {mid:>3} -> unknown/{out.name}  ({fmt}, {len(data):,} B, head={head})')

    (out_dir / 'inventory.tsv').write_text('\n'.join(manifest_lines) + '\n')
    ole.close()
    print(f'\nInventory: {out_dir}/inventory.tsv')

def main():
    if len(sys.argv) != 3:
        sys.exit('usage: fla_extract_audio.py <file.fla> <out_dir>')
    extract(Path(sys.argv[1]), Path(sys.argv[2]))

if __name__ == '__main__':
    main()
