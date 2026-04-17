r"""
Standalone binary-FLA shape decoder (WIP, ~50% of format).

Decodes Symbol streams containing CPicPage > CPicLayer > CPicFrame > CPicShape
nested structures, reading the MFC-style class-tagged serialization with a
lookup table we've reversed from Flash 8's flash.exe.

WORKING so far:
- OLE2 container (via olefile)
- MFC class tag protocol (new-class FFFF/back-ref XX80/null 0000)
- Length-prefixed UTF-16 strings (FF FE FF)
- CPicObj fields: schema, flags, children recursion, 2-u32 point (schema>0)
- CPicShape fields: schema, 24-byte matrix (16.16 fixed-point a/b/c/d + twip tx/ty)
- Shape data header: schema, edge hint, fill count, line count
- Fill styles (schema ≥ 3): solid / gradient / bitmap (partial)
- New-schema (≥5) edges: 4-anchor cubic Beziers, 32 B each

NOT YET IMPLEMENTED:
- Old-schema edge stream (< 5)    — decoded on paper, not coded here
- Inline line-style compact color  — partial
- CPicFrame / CPicPage / CPicLayer specific fields past CPicObj base
- CPicText, CPicMorphShape, CPicBitmap
"""
from __future__ import annotations
import olefile, struct, sys, json
from dataclasses import dataclass, field
from pathlib import Path

# ── bit-level reader over CArchive stream contents ─────────────────────────

class EOFReader(Exception):
    pass

class Reader:
    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0
    def _need(self, n):
        if self.pos + n > len(self.buf):
            raise EOFReader(f'need {n} bytes at pos 0x{self.pos:x}, only {self.remaining()} left')
    def u8(self):
        self._need(1); v = self.buf[self.pos]; self.pos += 1; return v
    def s8(self):
        v = self.u8()
        return v - 256 if v >= 128 else v
    def u16(self):
        self._need(2); v = struct.unpack_from('<H', self.buf, self.pos)[0]; self.pos += 2; return v
    def s16(self):
        self._need(2); v = struct.unpack_from('<h', self.buf, self.pos)[0]; self.pos += 2; return v
    def u32(self):
        self._need(4); v = struct.unpack_from('<I', self.buf, self.pos)[0]; self.pos += 4; return v
    def s32(self):
        self._need(4); v = struct.unpack_from('<i', self.buf, self.pos)[0]; self.pos += 4; return v
    def bytes(self, n):
        self._need(n); v = self.buf[self.pos:self.pos+n]; self.pos += n; return v
    def eof(self):
        return self.pos >= len(self.buf)
    def remaining(self):
        return len(self.buf) - self.pos

# ── MFC class-tag reader ────────────────────────────────────────────────────

class ArchiveReader:
    """Mimics CArchive::ReadObject semantics. Maintains class-table + object-table."""
    NULL  = 0x0000
    NEWCLASS = 0xFFFF
    def __init__(self, r: Reader):
        self.r = r
        self.classes: list[tuple[int, str]] = []   # [(schema, name), ...], 1-indexed
        self.objects: list = []                    # for back-refs

    def read_u16str(self) -> str:
        """Read an FF FE FF <len> <len utf-16le chars> string."""
        if self.r.bytes(3) != b'\xff\xfe\xff':
            raise ValueError('expected u16str BOM at pos %d' % (self.r.pos-3))
        ln = self.r.u8()
        return self.r.bytes(ln * 2).decode('utf-16le', 'replace')

    def read_class_tag(self) -> tuple[str, dict] | None:
        """Read an object-header tag.

        Returns one of:
          ('new_class', {'name': str, 'schema': int, 'idx': int})
          ('backref',   {'idx': int})
          ('null',      {})
        """
        tag = self.r.u16()
        if tag == self.NULL:
            return ('null', {})
        if tag == self.NEWCLASS:
            schema = self.r.u16()
            name_len = self.r.u16()
            name = self.r.bytes(name_len).decode('ascii', 'replace')
            self.classes.append((schema, name))
            return ('new_class', {'name': name, 'schema': schema, 'idx': len(self.classes)})
        if tag & 0x8000:
            return ('backref', {'idx': tag & 0x7FFF})
        raise ValueError(f'bad class tag 0x{tag:04x} @ 0x{self.r.pos-2:x}')

# ── decoders per MFC class ─────────────────────────────────────────────────

def fp_16_16(raw: int) -> float:
    """Interpret an s32 as 16.16 fixed-point."""
    if raw >= 0x80000000:
        raw -= 0x100000000
    return raw / 65536.0

def read_matrix_6(r: Reader) -> dict:
    """Read a 6-u32 2D affine matrix (a, b, c, d, tx_twips, ty_twips)."""
    a, b, c, d, tx, ty = struct.unpack('<iiiiii', r.bytes(24))
    return {
        'a':  fp_16_16(a), 'b': fp_16_16(b),
        'c':  fp_16_16(c), 'd': fp_16_16(d),
        'tx': tx / 20.0, 'ty': ty / 20.0,     # twips → px
        'raw': (a, b, c, d, tx, ty),
    }

def read_point_2u32(r: Reader) -> tuple[int, int]:
    return struct.unpack('<ii', r.bytes(8))

def read_fill_style(r: Reader, caps_flag: bool) -> dict:
    """Read one fill style. The `caps_flag` is CPicShape.shape_schema > 2 — it
       gates the new-schema extras inside gradients. Wire format:
         u32 color, u8 subtype_flags, u8 more_flags, then variable per subtype.
    """
    color_u32 = r.u32()
    subtype   = r.u8()
    more      = r.u8()
    fill = {
        'color_u32': color_u32,
        'color_bytes': color_u32.to_bytes(4, 'little').hex(),
        'subtype_flags': subtype,
        'more_flags': more,
    }
    if not (subtype & 0x10):
        if subtype & 0x20:
            fill['kind'] = 'type_0x20'
            fill['matrix'] = read_matrix_6(r)
            fill['id']  = r.u32()
            fill['x1']  = r.u16()
            fill['x2']  = r.u16()
            fill['x3']  = r.u16()
            fill['x4']  = r.u16()
        elif subtype & 0x40:
            fill['kind'] = 'bitmap'
            fill['matrix'] = read_matrix_6(r)
            fill['bitmap_id'] = r.u32()
        else:
            fill['kind'] = 'solid'
    else:
        fill['kind'] = 'gradient'
        fill['matrix'] = read_matrix_6(r)
        num_stops = r.u8()
        fill['num_stops'] = min(num_stops, 15)
        if caps_flag:
            fill['gradient_u16'] = r.u16()
            fill['gradient_u8']  = r.u8()
        stops = []
        for _ in range(num_stops):
            stops.append({
                'position': r.u8(),
                'color_u32': r.u32(),
            })
        fill['stops'] = stops
    return fill

def read_inline_fill(r: Reader) -> dict:
    """Bit-packed inline fill (for line styles). Returns raw fields."""
    sv = r.s16()
    uv = r.u16()
    flags = (uv >> 14) & 2
    fill = {'flags_bit': flags, 's_word': sv, 'u_word': uv}
    if sv == 0:
        b = uv & 0xff
        subtype = b & 7
        fill['subtype'] = subtype
        if subtype == 2:
            fill['a'] = uv >> 3
        elif subtype == 3:
            fill['a'] = (b >> 3) & 7
            fill['b'] = (b >> 6) & 3
            fill['c'] = (uv >> 8) & 3
        elif subtype == 4:
            fill['a'] = (b >> 3) & 3
            fill['b'] = (b >> 5) & 3
            fill['c'] = (uv & 0x180) >> 7
        elif subtype == 5:
            fill['a'] = (b >> 3) & 3
            fill['b'] = (b >> 5) & 3
            fill['c'] = (uv >> 7) & 3
            fill['d'] = (uv >> 9) & 3
            fill['e'] = (uv >> 11) & 3
            fill['f'] = (uv & 0x6000) >> 13
    else:
        fill['subtype'] = 1
        fill['x'] = sv
        fill['y'] = uv & 0x7fff
    return fill

def read_line_style(r: Reader, caps_flag: bool) -> dict:
    """One line style on-wire = u32 stroke_color + u16 flags +
       inline compact fill (4B) + (new schema only) caps/joins + full fill.
    """
    stroke_color_u32 = r.u32()
    flags16          = r.u16()
    inline           = read_inline_fill(r)
    line = {
        'stroke_color_u32': stroke_color_u32,
        'stroke_color_bytes': stroke_color_u32.to_bytes(4, 'little').hex(),
        'flags16': flags16,
        'inline':  inline,
    }
    if caps_flag:
        line['start_cap']    = r.u8()
        line['end_cap']      = r.u8()
        line['joins']        = r.u8()
        line['reserved']     = r.u8()
        line['miter_limit']  = r.u16()
        line['fill_style']   = read_fill_style(r, caps_flag)
    return line

# ── coord delta reader (FUN_00f3c150) ──────────────────────────────────────
# Internally Flash uses a "ultra-twip" fixed-point unit of 1 px = 2560 units
# (= 20 twips × 128). Types 1 and 2 read plain twips (with shift implied by
# Flash internal representation = twips << 7 for ALL accumulated coords),
# while type 3 reads pre-shifted values. We normalise to ultra-twips always.

def read_coord_delta(r: Reader, type_code: int) -> tuple[int, int]:
    """Read a coord-delta pair. Values accumulate in Flash's internal
       "ultra-twip" unit (1 px = 2560 units). Encoding:
         type 1 : raw s16 = ultra-twips directly  (fine precision, ±12.8 px)
         type 2 : raw s32 = ultra-twips directly  (full range)
         type 3 : raw s16 << 7 = ultra-twips      (coarse precision, wider range)
    """
    if type_code == 0:
        return (0, 0)
    if type_code == 1:
        return (r.s16(), r.s16())
    if type_code == 2:
        return (r.s32(), r.s32())
    if type_code == 3:
        return (r.s16() << 7, r.s16() << 7)
    raise ValueError(f'bad coord delta type {type_code}')

# ── byte-encoded edge loop ─────────────────────────────────────────────────

def read_byte_edges(r: Reader, shape_data_schema: int) -> list[dict]:
    """Read the variable-length edge stream until a 0 terminator byte."""
    edges = []
    # Cumulative position ("prev_to")
    cur_x, cur_y = 0, 0
    cur_fill0, cur_fill1, cur_line = 0, 0, 0
    while True:
        if r.eof():
            edges.append({'error': 'unexpected EOF in edge loop'})
            break
        flags = r.u8()
        if flags == 0:
            break    # terminator
        style_change = None
        if flags & 0x40:
            if flags & 0x80:
                v1 = r.u8(); v2 = r.u8(); v3 = r.u8()
            else:
                v1 = r.u16(); v2 = r.u16(); v3 = r.u16()
            style_change = {'v1': v1, 'v2': v2, 'v3': v3}
            # Heuristic: (fill0_idx, fill1_idx, line_idx) with high-bit meaning ?
            cur_fill0 = v1 & 0x7FFF
            cur_fill1 = v2 & 0x7FFF
            cur_line  = v3 & 0x7FFF
        t1 = flags & 3
        t2 = (flags >> 2) & 3
        t3 = (flags >> 4) & 3
        dx1, dy1 = read_coord_delta(r, t1)
        dx2, dy2 = read_coord_delta(r, t2)
        dx3, dy3 = read_coord_delta(r, t3)
        from_x, from_y = cur_x + dx1, cur_y + dy1
        ctrl_x, ctrl_y = from_x + dx2, from_y + dy2
        to_x,   to_y   = from_x + dx3, from_y + dy3
        if t2 == 0:
            # straight: midpoint-control stored for uniform quad-bezier rep
            ctrl_x, ctrl_y = (from_x + to_x) // 2, (from_y + to_y) // 2
            kind = 'line'
        else:
            kind = 'curve'
        edges.append({
            'flags':  flags,
            'style_change': style_change,
            'fill0':  cur_fill0, 'fill1': cur_fill1, 'line_style': cur_line,
            'from':  (from_x, from_y),
            'ctrl':  (ctrl_x, ctrl_y),
            'to':    (to_x,   to_y),
            'kind':  kind,
            'delta_types': (t1, t2, t3),
        })
        cur_x, cur_y = to_x, to_y
    return edges

def read_shape_data(r: Reader, caps_flag: bool):
    """FUN_00f3da60 — the core shape geometry reader."""
    shape_data_schema = r.u8()
    edge_hint  = r.u32()
    fill_count = r.u16()
    fills = []
    for _ in range(fill_count):
        if shape_data_schema < 3:
            fills.append({
                'kind': 'solid_old',
                'color_u32': r.u32(),
                'flags16': r.u16(),
            })
        else:
            fills.append(read_fill_style(r, caps_flag))
    line_count = r.u16()
    lines = []
    for _ in range(line_count):
        lines.append(read_line_style(r, caps_flag))
    # Edge stream — byte-encoded loop (schema ≥ 2)
    byte_edges = []
    if shape_data_schema >= 2:
        byte_edges = read_byte_edges(r, shape_data_schema)
    # Cubic32 post-stream (schema > 4, always additional)
    cubic_edges = []
    if shape_data_schema > 4:
        if r.remaining() >= 4:
            edge_count = r.s32()
            try:
                for i in range(edge_count):
                    if r.remaining() < 32:
                        cubic_edges.append({'truncated_at_edge': i, 'remaining_bytes': r.remaining()})
                        break
                    pts = [struct.unpack('<ii', r.bytes(8)) for _ in range(4)]
                    cubic_edges.append({'points': pts})
            except Exception as e:
                cubic_edges.append({'error': str(e)})
    return {
        'shape_data_schema': shape_data_schema,
        'edge_hint': edge_hint,
        'fills': fills,
        'lines': lines,
        'byte_edges': byte_edges,
        'cubic_edges': cubic_edges,
        'reader_pos_after_edges': r.pos,
    }

def read_cpicobj_fields(r: Reader, ar: ArchiveReader) -> dict:
    schema = r.u8()
    flags  = r.u8()
    # Children via ReadObject-ish loop — but we recurse into deserialize
    children = []
    while True:
        try:
            tag = ar.read_class_tag()
        except (ValueError, EOFReader) as e:
            children.append({'error': str(e), 'pos': r.pos})
            break
        if tag[0] == 'null':
            break
        if tag[0] == 'new_class':
            clsname = tag[1]['name']
            children.append(deserialize_known(clsname, r, ar))
        elif tag[0] == 'backref':
            if 0 < tag[1]['idx'] <= len(ar.classes):
                clsname = ar.classes[tag[1]['idx']-1][1]
                children.append({'class': clsname, 'backref': True,
                                 'child': deserialize_known(clsname, r, ar)})
            else:
                children.append({'backref_err': tag[1]})
                break
    base = {
        'schema': schema,
        'flags':  flags,
        'children': children,
    }
    try:
        if schema > 0: base['point']  = read_point_2u32(r)
        if schema > 2: base['extra1'] = r.u8()
        if schema > 3: base['extra2'] = r.u8()
    except EOFReader:
        base['_cpicobj_truncated'] = True
    return base

def deserialize_known(clsname: str, r: Reader, ar: ArchiveReader) -> dict:
    """Dispatch to the right per-class deserializer. Tolerates EOF gracefully."""
    try:
        if clsname == 'CPicPage':   return {'class': clsname, **read_cpicpage(r, ar)}
        if clsname == 'CPicLayer':  return {'class': clsname, **read_cpiclayer(r, ar)}
        if clsname == 'CPicFrame':  return {'class': clsname, **read_cpicframe(r, ar)}
        if clsname == 'CPicShape':  return {'class': clsname, **read_cpicshape(r, ar)}
        if clsname == 'CPicText':   return {'class': clsname, **read_cpictext(r, ar)}
        if clsname == 'CPicSprite': return {'class': clsname, **read_cpicsprite(r, ar)}
        if clsname == 'CPicButton': return {'class': clsname, **read_cpicbutton(r, ar)}
        if clsname in ('CPicBitmap', 'CPicMorphShape', 'CPicShapeObj'):
            return {'class': clsname, **read_cpicobj_fallback(clsname, r, ar)}
        return {'class': clsname, 'bytes_from_here': r.remaining(),
                'note': 'class not implemented - stopping'}
    except EOFReader as e:
        return {'class': clsname, 'eof_at_pos': r.pos, 'truncated': str(e)}

def read_cpicpage(r: Reader, ar: ArchiveReader) -> dict:
    return read_cpicobj_fields(r, ar)       # TODO: page-specific tail

def read_cpiclayer(r: Reader, ar: ArchiveReader) -> dict:
    return read_cpicobj_fields(r, ar)       # TODO: layer-specific tail

def read_cpicobj_fallback(clsname: str, r: Reader, ar: ArchiveReader) -> dict:
    """Fallback for CPicObj-derived classes we don't fully decode. Reads the
       CPicObj base (including the children loop) so the parent's parsing
       isn't corrupted, then skips the class-specific tail."""
    return read_cpicobj_fields(r, ar)

def _read_u16str_safe(r: Reader) -> str | None:
    """Try to read an FF FE FF <len> <utf16le> string. Returns None on failure."""
    if r.remaining() < 4:
        return None
    if r.buf[r.pos:r.pos+3] != b'\xff\xfe\xff':
        return None
    r.pos += 3
    ln = r.u8()
    if r.remaining() < ln * 2:
        return None
    return r.bytes(ln * 2).decode('utf-16le', 'replace')

def _read_mfc_cstring(r: Reader) -> str:
    """Read an MFC-style length-prefixed UTF-16 string (u8 charlen then chars)."""
    ln = r.u8()
    if ln == 0xff:
        ln = r.u16()
    return r.bytes(ln * 2).decode('utf-16le', 'replace')

def read_cpictext(r: Reader, ar: ArchiveReader) -> dict:
    """CPicText : CPicObj. Reads the CPicObj base, then text-specific fields.

       Decompiled from CPicText::Serialize at primary vtable slot 2
       (VA 0x00929800 in flash.exe, loading path at 0x929cf4):
         CPicObj::Serialize(archive)
         u8  text_schema
         24B matrix at this+0x80                    (read_matrix_6 via 0xf2c400)
         16B bounds at this+0x98                    (4 × s32 via 0xf2c760)
         u8  field_c8
         if text_schema >= 3:  u8 extra (local, not stored)
         if text_schema >= 5:  u32 field_120 (else u16 if >= 4)
         if text_schema >= 4:  u16 field_124
         if text_schema >= 4:  FUN_920900 → CString field_128 (font, if schema >= 10)
         FUN_9295c0 (internal state only, no archive reads)
         if text_schema >= 6:  FUN_920900 → CString field_134 (font, if schema >= 10)
         if text_schema >= 9:  FUN_937590 sub-object at field_74
         if text_schema >= 8:  u32 field_10c
       Font name is extracted by pattern scanning since the exact CString
       byte alignment between field_128 and field_134 is ambiguous.
    """
    out = read_cpicobj_fields(r, ar)
    try:
        out['text_schema'] = r.u8()
        out['matrix'] = read_matrix_6(r)
        out['text_bounds'] = {
            'left': r.s32(), 'right': r.s32(),
            'top': r.s32(), 'bottom': r.s32(),
        }
        out['text_field_c8'] = r.u8()
        ts = out['text_schema']
        if ts >= 3:
            r.u8()  # extra byte (consumed but not stored)
        if ts >= 5:
            out['text_field_120'] = r.u32()
        elif ts >= 4:
            out['text_field_120'] = r.u16()
        if ts >= 4:
            out['text_field_124'] = r.u16()
        # Font name: scan remaining bytes for u8-length + UTF-16LE pattern
        # since exact CString alignment depends on the font reader chain
        scan_start = r.pos
        scan_buf = r.buf[scan_start:]
        font_name = None
        font_size = None
        i = 0
        while i < len(scan_buf) - 4:
            ln = scan_buf[i]
            if 4 <= ln <= 40 and i + 1 + ln * 2 <= len(scan_buf):
                candidate = scan_buf[i+1:i+1+ln*2]
                try:
                    s = candidate.decode('utf-16le')
                    if all(0x20 <= ord(c) < 0x7F for c in s) and any(c.isalpha() for c in s):
                        if font_name is None:
                            font_name = s
                            if i >= 2:
                                font_size = struct.unpack_from('<H', scan_buf, i - 2)[0]
                        i += 1 + ln * 2
                        continue
                except (UnicodeDecodeError, ValueError):
                    pass
            i += 1
        if font_name:
            out['text_font_name'] = font_name
        if font_size:
            out['text_font_size_twips'] = font_size
        # Skip forward to the end of CPicText data. The parent CPicFrame's
        # children loop needs to find a null tag (00 00) after CPicText
        # returns, followed by the INT_MIN point sentinel. Scan for the
        # pattern: 00 00  00 00 00 80  00 00 00 80 (null tag + INT_MIN point).
        end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
        idx = r.buf.find(end_marker, scan_start)
        if idx >= 0 and idx < len(r.buf) - 12:
            r.pos = idx
    except EOFReader as e:
        out['_text_truncated'] = str(e)
    return out

def read_cpicsymbol_fields(r: Reader, ar: ArchiveReader) -> dict:
    """CPicSymbol : CPicObj. Reads CPicObj base, then CPicSymbol-specific fields.

       Decompiled from CPicSymbol::Serialize at primary vtable slot 2
       (VA 0x00916800 in flash.exe):
         CPicObj::Serialize(archive)               → 0x00902d70
         u8  symbol_schema                         (schema versioning)
         24B matrix at this+0x78                   → 0x00f2c2b0
         u16 field_b0
         u16 field_cc
         FUN_009024f0: u8 skip + 4 × u16          (field_90 struct)
         FUN_00916540 → 0x4710e0: u8-len CString  (symbol name at field_f0)
         u32 media_ref                             (field_d0/field_74)
         if symbol_schema < 10: extended handling  (skipped for schema 14)
    """
    out = read_cpicobj_fields(r, ar)
    try:
        out['symbol_schema'] = r.u8()
        out['symbol_matrix'] = read_matrix_6(r)
        out['symbol_field_b0'] = r.u16()
        out['symbol_field_cc'] = r.u16()
        r.u8()  # field_90 marker (always 1, skipped on load)
        out['field_90'] = [r.u16() for _ in range(4)]
        cstr_len = r.u8()
        out['symbol_name'] = r.bytes(cstr_len).decode('ascii', 'replace') if cstr_len > 0 else ''
        out['media_ref'] = r.u32()
    except EOFReader as e:
        out['_symbol_truncated'] = str(e)
    return out

def read_cpicsprite(r: Reader, ar: ArchiveReader) -> dict:
    """CPicSprite : CPicSymbol : CPicObj. Reads CPicSymbol base,
       then CPicSprite-specific fields.

       Decompiled from CPicSprite::Serialize at primary vtable slot 2
       (VA 0x00913d80 in flash.exe):
         CPicSymbol::Serialize(archive)
         u8  sprite_schema          (read directly from stream)
         if sprite_schema >= 2: FUN_008facd0(archive, &this->field_f4)
         FUN_00913bc0(archive, sprite_schema, &this->field_160)  — frame data
         if sprite_schema >= 3: FUN_005c5b00(archive, &this->field_164)
         if sprite_schema >= schema_level_6: FUN_00937590(&this->field_150, archive, mode)
         if sprite_schema >= 5: u32 → this->field_190
         if sprite_schema >= 8: FUN_005d4790(&this->field_15c, archive)
    """
    out = read_cpicsymbol_fields(r, ar)
    try:
        out['sprite_schema'] = r.u8()
        sprite_data_start = r.pos
        # Extract frame labels and strings from remaining sprite body
        # by scanning for ff-fe-ff string markers.
        sprite_body = r.buf[r.pos:]
        labels = []
        i = 0
        while i < len(sprite_body) - 4:
            if sprite_body[i:i+3] == b'\xff\xfe\xff':
                ln = sprite_body[i+3]
                end = i + 4 + ln * 2
                if end <= len(sprite_body) and ln > 0:
                    s = sprite_body[i+4:end].decode('utf-16le', 'replace')
                    if s.strip():
                        labels.append(s)
                i = max(i + 1, end) if end <= len(sprite_body) else i + 1
            else:
                i += 1
        if labels:
            out['sprite_labels'] = labels
        out['_sprite_body_bytes'] = len(sprite_body)
    except EOFReader as e:
        out['_sprite_truncated'] = str(e)
    return out

def read_cpicbutton(r: Reader, ar: ArchiveReader) -> dict:
    """CPicButton : CPicSymbol : CPicObj. Same base as CPicSprite."""
    out = read_cpicsymbol_fields(r, ar)
    out['_button_tail_remaining'] = r.remaining()
    return out
def read_cpicframe(r: Reader, ar: ArchiveReader) -> dict:
    """CPicFrame : CPicShape : CPicObj. Reads the inherited CPicShape body
       first (which itself reads CPicObj's), then CPicFrame's own
       schema-dependent tail fields.

       Tail layout (decompiled from FUN_008fdb80):
         u8  frame_schema
         u16 field_18c
         if frame_schema < 3:  u32 → field_188 (legacy)
         else:                  u16 → field_188
         if frame_schema > 1:   s16 → field_400
         if frame_schema > 4:
            if DAT_013c8ec0 == 0:  u32 sound_id (legacy)
            else:                   read CMediaSound back-ref
         if frame_schema > 5:
            u16 entry_count → field_500
            per entry: u32 + u16 + u16 (8 B each) → field_1fc + i*8
         if frame_schema > 6:
            u16 + u8 + u32 + s32 → field_238/0x23c/0x240/0x244
         if frame_schema > 7:  u16 → field_248
         if frame_schema > 8:  helper FUN_008f9120 (variable)
         if frame_schema > 9:  helper FUN_008fd980 (variable)
         if frame_schema >= 4: helper FUN_008faad0 etc. (mid-block)
         if frame_schema > 10: u32 + u32 → field_600/0x25c
         if frame_schema > 11: u32 → field_254
         if frame_schema > 12: helper FUN_00771700
         if frame_schema > 13: u32 → field_1e4
         if frame_schema > 14: operator>> (1 read)
         if frame_schema > 15: helper FUN_008f9120 + string ops

       Many helpers we don't decode in detail — they're variable-length; we
       use EOF tolerance + best-effort to consume them. For schemas we've
       observed in this project (typically 2..8) the simple field reads above
       are enough."""
    out = read_cpicshape(r, ar)
    try:
        out['frame_schema'] = r.u8()
        out['frame_18c'] = r.u16()
        if out['frame_schema'] < 3:
            out['frame_188'] = r.u32()
        else:
            out['frame_188'] = r.u16()
        if out['frame_schema'] > 1:
            out['frame_400'] = r.s16()
        if out['frame_schema'] > 4:
            # Sound id (legacy path). The "CMediaSound back-ref" path requires
            # global state we don't track — fall back to u32 read.
            out['frame_sound_id'] = r.u32()
        if out['frame_schema'] > 5:
            cnt = r.u16()
            out['frame_entries_count'] = cnt
            entries = []
            for _ in range(cnt):
                a = r.u32(); b = r.u16(); c = r.u16()
                entries.append((a, b, c))
            out['frame_entries'] = entries
        if out['frame_schema'] > 6:
            out['frame_238'] = r.u16()
            out['frame_23c'] = r.u8()
            out['frame_240'] = r.u32()
            out['frame_244'] = r.s32()
        if out['frame_schema'] > 7:
            out['frame_248'] = r.u16()
        # schemas >= 8 have variable-length helpers we don't decode; mark
        # as present so the EOF handler kicks in if more data remains
        if out['frame_schema'] > 8:
            out['_frame_tail_unparsed'] = True
    except EOFReader as e:
        out['_frame_tail_truncated'] = str(e)
    return out

def read_cpicshape(r: Reader, ar: ArchiveReader) -> dict:
    obj = read_cpicobj_fields(r, ar)
    out = dict(obj)
    try:
        out['shape_schema'] = r.u8()
        out['matrix']       = read_matrix_6(r)
        out['shape']        = read_shape_data(r, out['shape_schema'] > 2)
    except EOFReader as e:
        out['_shape_truncated'] = str(e)
    return out

# ── top-level: decode a Symbol stream ──────────────────────────────────────

# 10-byte tail of the CPicShape header right after schema(u8)+flags(u8):
#   <NULL child tag = 00 00>
#   <point.x = 0x80000000 = INT_MIN>
#   <point.y = 0x80000000 = INT_MIN>
# (Both points being INT_MIN is the "uninitialized origin" sentinel that
# Flash uses when the shape hasn't had its origin computed yet — extremely
# common in saved files.)
_SHAPE_HDR_TAIL = bytes.fromhex('00000000000080000000800')[:10] if False else \
                  bytes([0,0, 0,0,0,0x80, 0,0,0,0x80])

def _try_parse_shape_at(data: bytes, body_start: int, ar: ArchiveReader,
                        min_edges: int = 1):
    """Try parsing a CPicShape starting at `body_start`. Returns the shape
       dict (with non-empty geometry) on success, or None.
    """
    if body_start < 0 or body_start >= len(data):
        return None
    try_r = Reader(data); try_r.pos = body_start
    try_ar = ArchiveReader(try_r); try_ar.classes = list(ar.classes)
    try:
        shape = read_cpicshape(try_r, try_ar)
    except Exception:
        return None
    edges = shape.get('shape', {}).get('byte_edges', [])
    if len(edges) < min_edges or try_r.pos <= body_start + 30:
        return None
    sd = shape.get('shape', {})
    # Reject implausibly-high schema (probably noise matched signature)
    if sd.get('shape_data_schema', 99) > 8: return None
    # Reject shapes whose edges run off to absurd coords (likely junk parse)
    for e in edges:
        for (x, y) in (e['from'], e['ctrl'], e['to']):
            if abs(x) > 50_000_000 or abs(y) > 50_000_000:  # > 20k px
                return None
    return shape, try_r.pos

def scan_for_shapes(data: bytes, ar: ArchiveReader, start: int = 0,
                    max_results: int = 5000) -> list[dict]:
    """Recovery scanner: walk the stream looking for plausible CPicShape
       body starts. Two strategies are used together:

       1. **Class-declaration** recovery: any `FFFF 01 00 09 00 "CPicShape"`
          substring is *guaranteed* to be followed by a CPicShape body. No
          signature guesswork.
       2. **Signature** recovery: the 10-byte tail of the standard header
          (NULL child tag + 2 × INT_MIN point) indicates a shape whose
          CPicObj base has no children and an uninitialized origin — the
          common case. For each hit we try parsing at offsets [-2..0].
    """
    found = []
    taken_regions = []   # (start, end) pairs of already-recovered bytes

    def already_covered(p):
        return any(s <= p < e for (s, e) in taken_regions)

    # ─── 1) class-declaration recovery ────────────────────────────────
    import re
    CPICSHAPE_DECL = b'\xff\xff\x01\x00\x09\x00CPicShape'
    for m in re.finditer(re.escape(CPICSHAPE_DECL), data):
        body_start = m.end()
        if already_covered(body_start): continue
        tmp_classes = list(ar.classes)
        if not any(c[1] == 'CPicShape' for c in tmp_classes):
            tmp_classes.append((1, 'CPicShape'))
        # Use a temp ar to pass through
        class _TmpAR:
            pass
        tmp_ar = _TmpAR(); tmp_ar.classes = tmp_classes
        res = _try_parse_shape_at(data, body_start, tmp_ar, min_edges=1)
        if res is None: continue
        shape, end_pos = res
        found.append({
            'class': 'CPicShape',
            '_recovered_at': body_start,
            '_consumed_bytes': end_pos - body_start,
            '_recovered_via': 'class_decl',
            **shape,
        })
        taken_regions.append((body_start, end_pos))

    # ─── 2) signature-based recovery ──────────────────────────────────
    pos = start
    sig = _SHAPE_HDR_TAIL
    while pos < len(data) - len(sig) - 2 and len(found) < max_results:
        idx = data.find(sig, pos)
        if idx < 0: break
        pos = idx + 1
        for offset in (-2, -1, 0, -3):
            body_start = idx + offset
            if body_start < 0: continue
            if already_covered(body_start): break
            # Higher bar for signature-based recovery (avoid noise hits)
            res = _try_parse_shape_at(data, body_start, ar, min_edges=3)
            if res is None: continue
            shape, end_pos = res
            found.append({
                'class': 'CPicShape',
                '_recovered_at': body_start,
                '_consumed_bytes': end_pos - body_start,
                '_recovered_via': f'sig_offset_{offset}',
                **shape,
            })
            taken_regions.append((body_start, end_pos))
            pos = end_pos
            break
    return found

def decode_symbol_stream(data: bytes) -> dict:
    r = Reader(data)
    ar = ArchiveReader(r)
    # Flash prefixes the root object with a 0x01 tag
    root_tag = r.u8()
    tag = ar.read_class_tag()
    if tag[0] != 'new_class':
        raise ValueError(f'expected root new_class, got {tag}')
    cls_name = tag[1]['name']
    body = deserialize_known(cls_name, r, ar)
    consumed = r.pos
    # ALWAYS run the recovery scanner on the WHOLE stream — many sprite
    # containers (CPicSprite at the top level) consume 100% of the stream
    # via their child loop without surfacing any shape, but the bytes ARE
    # there (placed inside child CPicShape instances inside nested sprites).
    # The scanner picks these up via signature match.
    recovered = scan_for_shapes(data, ar, start=0)
    # De-duplicate against shapes already in the body tree by offset
    body_offsets = set()
    def collect_offsets(n):
        if isinstance(n, dict):
            if 'shape' in n and 'reader_pos_after_edges' in n.get('shape', {}):
                # we don't have start offset, but using the read pos as an
                # approximation isn't enough — keep all recovered for now
                pass
            for v in n.values(): collect_offsets(v)
        elif isinstance(n, list):
            for x in n: collect_offsets(x)
    collect_offsets(body)
    return {
        'root_tag': root_tag,
        'root_class': cls_name,
        'stream_bytes': len(data),
        'consumed_bytes': consumed,
        'body': body,
        'recovered_shapes': recovered,
    }

def main():
    if len(sys.argv) != 3:
        sys.exit('usage: decoder.py <file.fla> <symbol_id>')
    fla_path = sys.argv[1]
    sid = int(sys.argv[2])
    ole = olefile.OleFileIO(fla_path)
    stream_name = f'Symbol {sid}'
    data = ole.openstream(stream_name).read()
    try:
        result = decode_symbol_stream(data)
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'\nFailed at stream pos ≈ 0x{getattr(e, "pos", "?"):}: {e}')
    ole.close()

if __name__ == '__main__':
    main()
