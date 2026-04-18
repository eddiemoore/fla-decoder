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
        if clsname == 'CPicMorphShape':
            return {'class': clsname, **read_cpicmorphshape(r, ar)}
        if clsname in ('CMorphSegment', 'CMorphCurve', 'CMorphHintItem'):
            return {'class': clsname, **read_morph_subobject(r, ar)}
        if clsname == 'CPicBitmap':
            return {'class': clsname, **read_cpicbitmap(r, ar)}
        if clsname == 'CPicShapeObj':
            return {'class': clsname, **read_cpicobj_fallback(clsname, r, ar)}
        return {'class': clsname, 'bytes_from_here': r.remaining(),
                'note': 'class not implemented - stopping'}
    except EOFReader as e:
        return {'class': clsname, 'eof_at_pos': r.pos, 'truncated': str(e)}

def _read_flash_cstring(r: Reader) -> str:
    """Read an MFC/Flash CString from the archive.
       Handles: u8 len (ASCII), FF + FFFE (Unicode), FF + FFFF (long ASCII)."""
    b = r.u8()
    if b == 0:
        return ''
    if b < 0xFF:
        return r.bytes(b).decode('latin1', 'replace')
    ext = r.u16()
    if ext == 0xFFFE:
        count = r.u8()
        if count == 0xFF:
            count = r.u16()
        return r.bytes(count * 2).decode('utf-16le', 'replace') if count > 0 else ''
    elif ext == 0xFFFF:
        count = r.u32()
        return r.bytes(count).decode('latin1', 'replace')
    else:
        return r.bytes(ext).decode('latin1', 'replace')

def read_cpicpage(r: Reader, ar: ArchiveReader) -> dict:
    """CPicPage : CPicObj. Reads CPicObj base, then page-specific fields.

       Decompiled from CPicPage::Serialize at primary vtable slot 2
       (VA 0x00905cc0, loading path at 0x905dde):
         CPicObj::Serialize(archive)
         u8  page_schema
         if page_schema != 4:  u16 → field_78
         if page_schema >= 5:  u16 → field_7c
    """
    out = read_cpicobj_fields(r, ar)
    try:
        out['page_schema'] = r.u8()
        ps = out['page_schema']
        if ps != 4:
            out['page_field_78'] = r.u16()
        if ps >= 5:
            out['page_field_7c'] = r.u16()
        if ps >= 7:
            out['page_field_b4'] = r.u32()
        if ps >= 3:
            cnt = r.u32()
            out['page_field_84_count'] = cnt
            # Each entry is read by FUN_a47720; skip for now
    except EOFReader as e:
        out['_page_truncated'] = str(e)
    return out

def read_cpiclayer(r: Reader, ar: ArchiveReader) -> dict:
    """CPicLayer : CPicObj. Reads CPicObj base, then layer-specific fields.

       Decompiled from CPicLayer::Serialize at primary vtable slot 2
       (VA 0x00f3e520, loading path at 0xf3e8cf):
         CPicObj::Serialize(archive)
         u8  layer_schema
         FUN_f34c30 → CString layer_name (threshold=11)
         if layer_schema <= 3: u8 → call 0xf3e420(this, value)
         if layer_schema >= 4: more fields...
    """
    out = read_cpicobj_fields(r, ar)
    try:
        out['layer_schema'] = r.u8()
        ls = out['layer_schema']
        if ls >= 11:
            out['layer_name'] = _read_flash_cstring(r)
        if ls <= 3:
            out['layer_field_type'] = r.u8()
        if 4 <= ls <= 30:  # guard against misread schemas
            out['layer_type'] = r.u8()      # 0=normal, 1=guide, 3=mask, 4=masked, 5=folder
            out['layer_locked'] = r.u8()     # 0/1
            out['layer_visible'] = r.u8()    # 0=visible, 1=hidden (outline)
        if 5 <= ls <= 30:
            out['layer_color'] = r.u32()     # outline color (ARGB)
        if 6 <= ls <= 30:
            out['layer_field_8c'] = r.u32()
            out['layer_field_90'] = r.u32()
        if 8 <= ls <= 30:
            out['layer_field_98'] = r.u32()
        # Remaining fields (layer type enum, parent ref via ReadObject,
        # conditional flags) are complex and schema-interleaved.
        # Scan forward for the parent CPicPage's end-marker.
        end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
        search = r.pos
        while search < len(r.buf) - 14:
            idx = r.buf.find(end_marker, search)
            if idx < 0 or idx >= len(r.buf) - 14:
                break
            after = idx + 10
            schema_byte = r.buf[after]
            if schema_byte <= 10:
                r.pos = idx
                break
            search = idx + 1
    except EOFReader as e:
        out['_layer_truncated'] = str(e)
    return out

def read_cpicbitmap(r: Reader, ar: ArchiveReader) -> dict:
    """CPicBitmap : CPicObj. Reads CPicObj base, then bitmap-specific fields.

       Decompiled from CPicBitmap::Serialize at primary vtable slot 2
       (VA 0x008e8710, loading path at 0x8e8810):
         CPicObj::Serialize(archive)
         u8  bitmap_schema
         24B matrix at this+0x78
         u16 media_id (or ReadObject via sound manager)
         if bitmap_schema >= 2:  u8 filter_flag
           if filter_flag != 0:  filter Serialize (complex)
    """
    out = read_cpicobj_fields(r, ar)
    try:
        out['bitmap_schema'] = r.u8()
        out['bitmap_matrix'] = read_matrix_6(r)
        out['bitmap_media_id'] = r.u16()
        if out['bitmap_schema'] >= 2:
            out['bitmap_filter_flag'] = r.u8()
    except EOFReader as e:
        out['_bitmap_truncated'] = str(e)
    # End-marker scan for any remaining data (filter objects etc.)
    end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
    idx = r.buf.find(end_marker, r.pos)
    if idx >= 0 and idx < len(r.buf) - 12:
        r.pos = idx
    return out


def read_cpicobj_fallback(clsname: str, r: Reader, ar: ArchiveReader) -> dict:
    """Fallback for CPicObj-derived classes we don't fully decode. Reads the
       CPicObj base (including the children loop) so the parent's parsing
       isn't corrupted, then scans for the end-marker to skip unknown tail."""
    out = read_cpicobj_fields(r, ar)
    end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
    idx = r.buf.find(end_marker, r.pos)
    if idx >= 0 and idx < len(r.buf) - 12:
        r.pos = idx
    return out

def read_morph_subobject(r: Reader, ar: ArchiveReader) -> dict:
    """Read CMorphSegment, CMorphCurve, or CMorphHintItem.
       These are sub-objects within CPicMorphShape containing curve/point data
       as sequences of s32 coordinate pairs in twips (1/20 pixel)."""
    out = {}
    start = r.pos
    # Morph sub-objects have a small header (typically 16 bytes: 2 u32 fields
    # + 2 s32 sentinels) before the coordinate data.
    # Skip header bytes that are 0x00 or 0xFF runs.
    try:
        while r.remaining() >= 4:
            peek = struct.unpack_from('<I', r.buf, r.pos)[0]
            if peek == 0 or peek == 0xFFFFFFFF:
                r.pos += 4
            else:
                break
    except EOFReader:
        pass
    # Extract coordinate pairs (s32 x, s32 y) in twips (1/20 pixel).
    # Morph curves store groups of control points separated by u32=0 + u8 count.
    all_coords = []
    try:
        while r.remaining() >= 8:
            # Check for class tags (NEWCLASS or backref) that end this object
            if r.remaining() >= 2:
                peek = struct.unpack_from('<H', r.buf, r.pos)[0]
                if peek == 0xFFFF:
                    break
                if peek & 0x8000 and (peek & 0x7FFF) <= len(ar.classes):
                    break
            # Check for separator: u32=0 followed by non-coordinate data
            if r.remaining() >= 6:
                peek4 = struct.unpack_from('<I', r.buf, r.pos)[0]
                if peek4 == 0:
                    r.pos += 4
                    if r.remaining() >= 1:
                        r.u8()
                    continue
            # Peek ahead: if a valid NEWCLASS declaration (ff ff 01 00 NN 00 + ASCII)
            # appears within next 8 bytes, stop to let the parent read it
            if r.remaining() >= 12:
                stop = False
                for off in range(0, 6, 2):
                    p = r.pos + off
                    if (r.buf[p] == 0xFF and r.buf[p+1] == 0xFF
                            and r.buf[p+2] == 0x01 and r.buf[p+3] == 0x00
                            and r.buf[p+5] == 0x00
                            and 0x41 <= r.buf[p+6] <= 0x5A):  # ASCII uppercase
                        stop = True
                        break
                if stop:
                    break
            x = r.s32()
            y = r.s32()
            if abs(x) < 10000000 and abs(y) < 10000000:
                all_coords.append({'x': x, 'y': y, 'px_x': x / 20.0, 'px_y': y / 20.0})
            else:
                r.pos -= 8
                break
    except EOFReader:
        pass
    if all_coords:
        out['coords'] = all_coords
    out['_bytes_consumed'] = r.pos - start
    return out


def read_cpicmorphshape(r: Reader, ar: ArchiveReader) -> dict:
    """CPicMorphShape — shape tween (morph) with start/end shape pairs.
       Contains CMorphSegment, CMorphCurve, and CMorphHintItem sub-objects.
       Coordinates are in twips (1/20 pixel), not ultra-twips."""
    out = {}
    start = r.pos
    try:
        out['morph_schema'] = r.u8()
        out['morph_flags'] = r.u8()
        # Two 6-element matrices (start + end morph positions, 8.8 fixed-point)
        start_matrix = [r.u32() for _ in range(6)]
        end_matrix = [r.u32() for _ in range(6)]
        out['start_matrix'] = {
            'a': start_matrix[0] / 256.0, 'b': start_matrix[1] / 256.0,
            'c': start_matrix[2] / 256.0, 'd': start_matrix[3] / 256.0,
            'tx': start_matrix[4] / 20.0, 'ty': start_matrix[5] / 20.0,
        }
        out['end_matrix'] = {
            'a': end_matrix[0] / 256.0, 'b': end_matrix[1] / 256.0,
            'c': end_matrix[2] / 256.0, 'd': end_matrix[3] / 256.0,
            'tx': end_matrix[4] / 20.0, 'ty': end_matrix[5] / 20.0,
        }
        # Extra fields (7 bytes padding) + u8 segment count
        r.bytes(7)  # skip padding/extra fields
        out['morph_segment_count'] = r.u8()
        r.u8()  # skip padding byte before class tags
        # Read embedded morph sub-objects via class tags.
        # Between sub-objects there can be 1-2 byte spacers; skip non-tag bytes.
        children = []
        while r.remaining() >= 2:
            # Skip spacer bytes until we find a valid class tag
            while r.remaining() >= 2:
                peek = struct.unpack_from('<H', r.buf, r.pos)[0]
                if peek == 0xFFFF or peek == 0x0000:
                    break
                if (peek & 0x8000) and (peek & 0x7FFF) <= len(ar.classes):
                    break
                r.pos += 1
            try:
                tag = ar.read_class_tag()
            except (ValueError, EOFReader):
                break
            if tag[0] == 'null':
                break
            if tag[0] == 'new_class':
                child = deserialize_known(tag[1]['name'], r, ar)
                children.append(child)
            elif tag[0] == 'backref':
                idx = tag[1]['idx']
                if 0 < idx <= len(ar.classes):
                    clsname = ar.classes[idx - 1][1]
                    child = deserialize_known(clsname, r, ar)
                    children.append(child)
                else:
                    break
        out['morph_children'] = children
    except EOFReader as e:
        out['_morph_truncated'] = str(e)
    # End-marker scan for parent alignment
    end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
    idx = r.buf.find(end_marker, r.pos)
    if idx >= 0 and idx < len(r.buf) - 12:
        r.pos = idx
    return out


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

def _read_ccolordef(r: Reader, schema: int) -> str:
    """Read a CColorDef (font name / color string) from archive.
       Format depends on schema:
         schema >= 10: u8 count + count×2 bytes UTF-16LE
         schema <  10: u8 count + count bytes ASCII
       Decompiled from FUN_91d230 (VA 0x91d230)."""
    count = r.u8()
    if count == 0:
        return ''
    if schema >= 10:
        return r.bytes(count * 2).decode('utf-16le', 'replace')
    else:
        return r.bytes(count).decode('latin1', 'replace')


def _read_text_run(r: Reader) -> dict:
    """Read a single text run's formatting data.
       Decompiled from FUN_91d310 (VA 0x91d310, loading path at 0x91d830).
       Returns formatting fields for one text run."""
    run = {}
    run['run_schema'] = r.u8()
    rs = run['run_schema']
    if rs >= 2:
        run['char_count'] = r.u16()
        run['font_name'] = _read_ccolordef(r, rs)
        run['font_color'] = r.u32()
        run['bold'] = r.u8()
        run['italic'] = r.u8()
        if rs >= 3:
            run['align'] = r.u8()
            run['field_4b'] = r.u8()
            run['field_4c'] = r.u8()
            run['field_4d'] = r.u8()
        else:
            packed = r.u8()
            run['align'] = packed & 0x03
        run['field_8d4'] = r.u8()
        run['indent'] = r.u16()
        run['line_spacing'] = r.u16()
        run['left_margin'] = r.u16()
        run['right_margin'] = r.u16()
        run['field_8de'] = r.u16()
        if rs >= 5:
            run['highlight'] = _read_ccolordef(r, rs)
        if rs >= 6:
            run['field_8e0'] = r.u8()
            run['field_8e1'] = r.u8()
            run['field_8e2'] = r.u8()
        if rs >= 8:
            run['field_8e3'] = r.u8()
        if rs >= 7:
            run['url_color'] = _read_ccolordef(r, rs)
    return run


def _read_text_body(r: Reader, unicode_mode: bool) -> dict:
    """Read text body data (FUN_9295c0, VA 0x9295c0).
       Reads: u16 text_length + text_run formatting + text data."""
    body = {}
    text_length = r.u16()
    body['text_length'] = text_length
    if text_length == 0:
        return body
    try:
        body['text_run'] = _read_text_run(r)
    except EOFReader:
        body['_run_truncated'] = True
        return body
    # Read actual text characters
    if unicode_mode:
        raw = r.bytes(text_length * 2)
        body['text'] = raw.decode('utf-16le', 'replace')
    else:
        raw = r.bytes(text_length)
        body['text'] = raw.decode('latin1', 'replace')
    return body


def read_cpictext(r: Reader, ar: ArchiveReader) -> dict:
    """CPicText : CPicObj. Reads the CPicObj base, then text-specific fields.

       Decompiled from CPicText::Serialize at primary vtable slot 2
       (VA 0x00929800 in flash.exe, loading path at 0x929cf4).

       CColorDef format (FUN_91d230): u8 count + count×2 bytes UTF-16LE
       (for schema >= 10) or count bytes ASCII (for schema < 10).

       Text body (FUN_9295c0): u16 text_length + text_run + text data.
       Text run (FUN_91d310): u8 run_schema + u16 char_count + CColorDef
       font_name + u32 color + u8 bold + u8 italic + conditional fields.
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
            r.u8()  # discarded byte
        if ts >= 5:
            out['text_field_120'] = r.u32()
        elif ts >= 4:
            out['text_field_120'] = r.u16()
        if ts >= 4:
            out['text_field_124'] = r.u16()
            # field_128 via FUN_920900: conditional MFC CString (threshold=10)
            if ts >= 10:
                out['text_color_128'] = _read_flash_cstring(r)
            # Conditional second color (only if byte 1 bit 5 of field_120)
            field_121 = (out.get('text_field_120', 0) >> 8) & 0xFF
            if field_121 & 0x20:
                if ts >= 10:
                    out['text_color_12c'] = _read_flash_cstring(r)
        # Multiline check: byte 1 bit 6 of field_120
        field_121 = (out.get('text_field_120', 0) >> 8) & 0xFF
        is_multiline = bool(field_121 & 0x40)
        if is_multiline:
            # Multiline: read master text run directly
            try:
                out['text_master_run'] = _read_text_run(r)
            except EOFReader:
                out['_master_run_truncated'] = True
        # Text body: always called (reads text_length + run + text)
        unicode_mode = ts >= 10
        try:
            out['text_body'] = _read_text_body(r, unicode_mode)
            if out['text_body'].get('text'):
                out['text_content'] = out['text_body']['text']
            run = out['text_body'].get('text_run', {})
            if run.get('font_name'):
                out['text_font_name'] = run['font_name']
            if run.get('char_count'):
                # Font size is in text_field_124 (twips), not in the run
                pass
            if out.get('text_field_124'):
                out['text_font_size_twips'] = out['text_field_124']
        except EOFReader:
            out['_text_body_truncated'] = True
        # Post-text-body fields
        scan_start = r.pos
        try:
            if ts >= 6 and ts >= 10:
                out['text_color_134'] = _read_flash_cstring(r)
            # schema >= 9: FUN_937590 sub-object (complex, skip)
            # schema >= 8: u32 field_10c
            # schema >= 11-13: more CColorDefs + filter
            # These are complex; use end-marker scan for the rest
        except EOFReader:
            pass
        # End-marker scan for remaining fields
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
         u8  sprite_schema
         if sprite_schema >= 2: FUN_008facd0(archive, &field_f4) — timeline
         FUN_00913bc0: conditional CString field_160 (threshold=7)
         if sprite_schema >= 3: FUN_005c5b00(archive, &field_164) — complex
         if sprite_schema >= 5: u32 → field_190
         if sprite_schema >= 8: FUN_005d4790(archive) — u8 + u32
    """
    out = read_cpicsymbol_fields(r, ar)
    try:
        out['sprite_schema'] = r.u8()
        ss = out['sprite_schema']
        sprite_data_start = r.pos

        # Timeline data (same FUN_8facd0 as CPicFrame)
        if ss >= 2:
            try:
                out['sprite_timeline'] = _read_fun_8facd0(r, ar)
            except EOFReader:
                out['_sprite_timeline_truncated'] = True

        # Conditional CString field_160 (threshold at [0x12b95a0]=7)
        if ss >= 7:
            try:
                out['sprite_field_160'] = _read_flash_cstring(r)
            except EOFReader:
                pass

        # Extract frame labels from remaining sprite body via string scan
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

        # Skip to end-marker for complex remaining fields
        end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
        idx = r.buf.find(end_marker, r.pos)
        if idx >= 0 and idx < len(r.buf) - 12:
            r.pos = idx
    except EOFReader as e:
        out['_sprite_truncated'] = str(e)
    return out

def read_cpicbutton(r: Reader, ar: ArchiveReader) -> dict:
    """CPicButton : CPicSymbol : CPicObj. Same base as CPicSprite."""
    out = read_cpicsymbol_fields(r, ar)
    end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
    idx = r.buf.find(end_marker, r.pos)
    if idx >= 0 and idx < len(r.buf) - 12:
        r.pos = idx
    return out
def _read_fun_8facd0(r: Reader, ar: ArchiveReader) -> dict:
    """FUN_8facd0: timeline sub-object reader (schema >= 19).
       Reads type_id, format_type, optional entries, and a CString."""
    result = {}
    result['type_id'] = r.u32()
    result['format_type'] = r.u32()
    if result['type_id'] >= 1:
        result['tl_init'] = r.u32()
        count = r.u32()
        result['tl_count'] = count
        if count > 0 and count < 10000:
            result['tl_char_ids'] = [r.u32() for _ in range(count)]
    # format_type dispatch: 0=FUN_498020, 1=FUN_8f9290, 2=CString
    if result['format_type'] == 1 and result['type_id'] >= 4:
        result['tl_label'] = _read_flash_cstring(r)
    elif result['format_type'] == 0:
        # FUN_498020: per-frame sub-structure (u32 schema + u32 count + entries)
        result['tl_pf_schema'] = r.u32()
        pf_count = r.u32()
        result['tl_pf_count'] = pf_count
        if pf_count > 0 and pf_count < 10000:
            result['tl_pf_char_ids'] = [r.u32() for _ in range(pf_count)]
    return result


def read_cpicframe(r: Reader, ar: ArchiveReader) -> dict:
    """CPicFrame : CPicShape : CPicObj. Reads the inherited CPicShape body
       first (which itself reads CPicObj's), then CPicFrame's own
       schema-dependent tail fields.

       Full layout (decompiled from loading path at 0x8fe3fa + 0x8fe9b9):
         u8  frame_schema
         u16 field_18c
         if frame_schema > 2:   u16 → field_188
         else:                   u8  → field_188
         if frame_schema > 1:   s16 → field_190
         if frame_schema > 4:   u16 → sound ref (CMediaSound)
         if frame_schema > 5:   u16 count + count × (u32 + u16 + u16)
         if frame_schema > 6:   u16 + u8 + u32 + s32
         if frame_schema > 7:   u16 → field_248
         if frame_schema > 8:   CString field_250 (threshold=23)
         Branch on schema:
           >= 19: FUN_008facd0 (timeline sub-object, variable)
           10-18: FUN_008fd980 (u32 + variable data)
           4-9:   FUN_008faad0 + FUN_008f9570 (jump table, complex)
         if frame_schema > 10:  u32 field_258 + u32 field_25c
         if frame_schema > 11:  u32 field_254 (clamped ≤ 1)
         if frame_schema > 12:  ReadObject CPicMorphShape
         if frame_schema > 13:  u32 field_1e4
         if frame_schema > 14:  ReadObject CObList
         if frame_schema > 15:  CString field_298 (threshold=23)
         if frame_schema > 19:  u32 field_294
         if frame_schema > 20:  u32 field_24c
         if frame_schema >= 22: u32 field_264
         if frame_schema >= 24: u32 field_194 + u32 field_198 (bool)"""
    out = read_cpicshape(r, ar)
    try:
        out['frame_schema'] = r.u8()
        out['frame_18c'] = r.u16()
        fs = out['frame_schema']
        if fs > 2:
            out['frame_188'] = r.u16()
        else:
            out['frame_188'] = r.u8()
        if fs > 1:
            out['frame_190'] = r.s16()
        if fs > 4:
            out['frame_sound_id'] = r.u16()
        if fs > 5:
            cnt = r.u16()
            out['frame_entries_count'] = cnt
            entries = []
            for _ in range(cnt):
                a = r.u32(); b = r.u16(); c = r.u16()
                entries.append((a, b, c))
            out['frame_entries'] = entries
        if fs > 6:
            out['frame_238'] = r.u16()
            out['frame_23c'] = r.u8()
            out['frame_240'] = r.u32()
            out['frame_244'] = r.s32()
        if fs > 7:
            out['frame_248'] = r.u16()
        if fs > 8:
            if fs >= 23:
                try:
                    out['frame_250'] = _read_flash_cstring(r)
                except EOFReader:
                    pass
            # Timeline sub-object: schema-dependent dispatch
            if fs >= 19:
                try:
                    out['timeline'] = _read_fun_8facd0(r, ar)
                except EOFReader:
                    out['_timeline_truncated'] = True
            elif fs >= 10:
                # FUN_8fd980: simpler timeline data
                out['_frame_tail_unparsed'] = True
                # Fall through to end-marker scan below
            else:
                out['_frame_tail_unparsed'] = True
            # Post-timeline fields (schema > 10 onwards)
            if fs > 10 and not out.get('_frame_tail_unparsed'):
                try:
                    out['frame_258'] = r.u32()
                    out['frame_25c'] = r.u32()
                    if fs > 11:
                        out['frame_254'] = r.u32()
                    if fs > 12:
                        morph_tag = r.u16()
                        if morph_tag == 0:
                            out['frame_morph'] = None
                        else:
                            # Non-null: back up and let the archive reader handle it
                            r.pos -= 2
                            out['_frame_morph_tag'] = morph_tag
                    if fs > 13:
                        out['frame_1e4'] = r.u32()
                    if fs > 14:
                        oblist_tag = r.u16()
                        if oblist_tag == 0:
                            out['frame_oblist'] = None
                        else:
                            r.pos -= 2
                            out['_frame_oblist_tag'] = oblist_tag
                    if fs > 15:
                        if fs >= 23:
                            out['frame_298'] = _read_flash_cstring(r)
                    if fs > 19:
                        out['frame_294'] = r.u32()
                    if fs > 20:
                        out['frame_24c'] = r.u32()
                    if fs >= 22:
                        out['frame_264'] = r.u32()
                    if fs >= 24:
                        out['frame_194'] = r.u32()
                        out['frame_198'] = r.u32()
                except EOFReader:
                    out['_frame_post_truncated'] = True
            # For schemas 10-18 or if parsing failed, use end-marker scan
            if out.get('_frame_tail_unparsed'):
                end_marker = b'\x00\x00\x00\x00\x00\x80\x00\x00\x00\x80'
                search = r.pos
                while search < len(r.buf) - 14:
                    idx = r.buf.find(end_marker, search)
                    if idx < 0 or idx >= len(r.buf) - 14:
                        break
                    after = idx + 10
                    schema_byte = r.buf[after]
                    if schema_byte <= 30:
                        rest = r.buf[after+1:after+5]
                        if b'\xff\xfe\xff' in rest or schema_byte == 0:
                            r.pos = idx
                            break
                    search = idx + 1
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
        # Cap taken region based on actual edge count to prevent one shape
        # from blocking neighbors. Real shapes are ~10-50 bytes per edge.
        n_edges = len(shape.get('shape', {}).get('byte_edges', []))
        max_region = max(500, n_edges * 12 + 1000)
        capped_end = min(end_pos, body_start + max_region)
        found.append({
            'class': 'CPicShape',
            '_recovered_at': body_start,
            '_consumed_bytes': end_pos - body_start,
            '_recovered_via': 'class_decl',
            **shape,
        })
        taken_regions.append((body_start, capped_end))

    # ─── 1b) CPicMorphShape class-declaration recovery ────────────────
    CPICMORPH_DECL = b'\xff\xff\x01\x00\x0e\x00CPicMorphShape'
    for m in re.finditer(re.escape(CPICMORPH_DECL), data):
        body_start = m.end()
        if already_covered(body_start): continue
        tmp_classes = list(ar.classes)
        if not any(c[1] == 'CPicMorphShape' for c in tmp_classes):
            tmp_classes.append((1, 'CPicMorphShape'))
        try:
            tmp_r = Reader(data); tmp_r.pos = body_start
            tmp_ar2 = ArchiveReader(tmp_r); tmp_ar2.classes = list(tmp_classes)
            morph = read_cpicmorphshape(tmp_r, tmp_ar2)
            if morph.get('morph_children'):
                found.append({
                    'class': 'CPicMorphShape',
                    '_recovered_at': body_start,
                    '_recovered_via': 'morph_class_decl',
                    **morph,
                })
        except Exception:
            pass

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
            n_edges = len(shape.get('shape', {}).get('byte_edges', []))
            max_region = max(500, n_edges * 12 + 1000)
            capped_end = min(end_pos, body_start + max_region)
            found.append({
                'class': 'CPicShape',
                '_recovered_at': body_start,
                '_consumed_bytes': end_pos - body_start,
                '_recovered_via': f'sig_offset_{offset}',
                **shape,
            })
            taken_regions.append((body_start, capped_end))
            pos = capped_end
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
