r"""
Render decoded FLA shapes (from decoder.py) to SVG.

Edge coordinates in the decoded output are in Flash "ultra-twips"
(1 px = 2560 units, because delta-type-3 uses `s16 << 7` = ×128 scaling
relative to twips, and 1 px = 20 twips, so 1 px = 20 × 128 = 2560 units).
"""
from __future__ import annotations
import sys
import olefile
from . import decoder

UNIT = 2560.0   # ultra-twips per pixel

def find_all_shapes(node, out=None):
    if out is None: out = []
    if isinstance(node, dict):
        if 'shape' in node:
            out.append(node)
        for v in node.values():
            find_all_shapes(v, out)
    elif isinstance(node, list):
        for item in node:
            find_all_shapes(item, out)
    return out

def find_first_shape(node):
    """Return the first shape node with byte_edges (or first of any)."""
    shapes = find_all_shapes(node)
    with_edges = [s for s in shapes if s['shape'].get('byte_edges')]
    return (with_edges or shapes or [None])[0]

def find_nonempty_shapes(node):
    """Return all shapes with byte_edges. Also includes 'recovered_shapes'
       at the top level (added by decoder.scan_for_shapes fallback)."""
    out = [s for s in find_all_shapes(node) if s.get('shape', {}).get('byte_edges')]
    return out

def find_nonempty_shapes_in_result(result: dict):
    """Like find_nonempty_shapes but also includes recovered shapes."""
    out = find_nonempty_shapes(result.get('body', {}))
    for r in result.get('recovered_shapes', []):
        if r.get('shape', {}).get('byte_edges'):
            out.append(r)
    return out

def matrix_to_svg(m: dict) -> str:
    """Build SVG `transform=matrix(a b c d e f)` from decoded matrix dict.
       Flash translation is stored in twips — we scale to pixels so the
       translation lines up with our 1 px = 2560 units shape coords (which we
       divide by 2560 = UNIT at render time). Matrix a/b/c/d are already
       unitless (16.16 fixed-point).
    """
    a = m.get('a', 1.0); b = m.get('b', 0.0)
    c = m.get('c', 0.0); d = m.get('d', 1.0)
    tx = m.get('tx', 0.0); ty = m.get('ty', 0.0)
    return f'matrix({a:.4f} {b:.4f} {c:.4f} {d:.4f} {tx:.2f} {ty:.2f})'

def edge_path_d(edges, transform=None):
    """Build a single SVG `d` string from a list of decoded quad-Bezier edges.
    Uses M for start and Q for curves / L for straight.
    """
    if not edges: return ''
    out = []
    cur = None
    for e in edges:
        fx, fy = e['from']
        cx, cy = e['ctrl']
        tx, ty = e['to']
        fx_px = fx / UNIT; fy_px = fy / UNIT
        cx_px = cx / UNIT; cy_px = cy / UNIT
        tx_px = tx / UNIT; ty_px = ty / UNIT
        if cur is None or cur != (fx_px, fy_px):
            out.append(f'M {fx_px:.2f} {fy_px:.2f}')
        if e['kind'] == 'line':
            out.append(f'L {tx_px:.2f} {ty_px:.2f}')
        else:
            out.append(f'Q {cx_px:.2f} {cy_px:.2f} {tx_px:.2f} {ty_px:.2f}')
        cur = (tx_px, ty_px)
    return ' '.join(out)

def argb_to_css(u32: int) -> str:
    """Interpret u32 as RGBA bytes (LE): byte0=R byte1=G byte2=B byte3=A."""
    r = u32 & 0xff
    g = (u32 >> 8) & 0xff
    b = (u32 >> 16) & 0xff
    a = (u32 >> 24) & 0xff
    if a == 0: return 'none'
    if a == 255: return f'#{r:02x}{g:02x}{b:02x}'
    return f'rgba({r},{g},{b},{a/255:.3f})'

def fill_to_svg(fill, defs_buf, defs_idx_ref):
    """Return a fill attribute string; may append a <linearGradient> or
       <radialGradient> to defs_buf and return url(#id) for gradients."""
    if fill is None:
        return 'none'
    kind = fill.get('kind')
    if kind == 'solid' or kind == 'solid_old':
        return argb_to_css(fill.get('color_u32', 0))
    if kind == 'gradient':
        # Subtype bits distinguish linear vs radial: 0x12 or 0x13 = radial, 0x10 = linear
        subtype = fill.get('subtype_flags', 0)
        is_radial = (subtype & 0x03) != 0
        defs_idx_ref[0] += 1
        gid = f'g{defs_idx_ref[0]}'
        mx = fill.get('matrix', {})
        stops_xml = ''
        for stop in fill.get('stops', []):
            pos = stop['position'] / 255.0
            stops_xml += f'<stop offset="{pos:.3f}" stop-color="{argb_to_css(stop["color_u32"])}"/>'
        tx = mx.get('tx', 0); ty = mx.get('ty', 0)
        a = mx.get('a', 1); b = mx.get('b', 0); c = mx.get('c', 0); d = mx.get('d', 1)
        if is_radial:
            defs_buf.append(f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                            f'cx="0" cy="0" r="819.2" '
                            f'gradientTransform="matrix({a:.4f} {b:.4f} {c:.4f} {d:.4f} {tx:.2f} {ty:.2f})">{stops_xml}</radialGradient>')
        else:
            defs_buf.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                            f'x1="-819.2" y1="0" x2="819.2" y2="0" '
                            f'gradientTransform="matrix({a:.4f} {b:.4f} {c:.4f} {d:.4f} {tx:.2f} {ty:.2f})">{stops_xml}</linearGradient>')
        return f'url(#{gid})'
    if kind == 'bitmap':
        return argb_to_css(fill.get('color_u32', 0))   # fallback to fill color
    return argb_to_css(fill.get('color_u32', 0)) if 'color_u32' in fill else 'none'

def _render_shape_body(shape_node: dict, defs: list, defs_idx: list) -> tuple[str, list[tuple[float,float]]]:
    """Render one shape's paths as an SVG string (without wrapping). Returns
       the SVG fragment and transformed-bounds-in-pixels as a list of (x,y)
       corners for viewBox calculation.
    """
    shape = shape_node['shape']
    edges = shape.get('byte_edges', [])
    if not edges:
        return '', []
    fills = shape.get('fills', [])
    lines = shape.get('lines', [])
    groups = {}
    for e in edges:
        k = (e['fill0'], e['fill1'], e['line_style'])
        groups.setdefault(k, []).append(e)
    body = []
    for (f0, f1, ls), group in groups.items():
        d = edge_path_d(group)
        fidx = f0 if f0 else f1
        fill = fills[fidx-1] if 0 < fidx <= len(fills) else None
        fill_css = fill_to_svg(fill, defs, defs_idx)
        stroke_css = 'none'; sw = 0
        if 0 < ls <= len(lines):
            line = lines[ls-1]
            stroke_css = argb_to_css(line.get('stroke_color_u32', 0))
            sw = max(0.25, line.get('flags16', 0) * 0.05)
        body.append(f'<path d="{d}" fill="{fill_css}" stroke="{stroke_css}" '
                    f'stroke-width="{sw:.2f}" fill-rule="evenodd"/>')
    # Corners in local shape-space pixels (pre-matrix)
    xs = []; ys = []
    for e in edges:
        for (x, y) in (e['from'], e['ctrl'], e['to']):
            xs.append(x/UNIT); ys.append(y/UNIT)
    if xs:
        corners = [(min(xs), min(ys)), (max(xs), min(ys)),
                   (min(xs), max(ys)), (max(xs), max(ys))]
    else:
        corners = []
    return '\n'.join(body), corners

def _apply_matrix(m: dict, pt: tuple[float, float]) -> tuple[float, float]:
    x, y = pt
    a = m.get('a', 1.0); b = m.get('b', 0.0)
    c = m.get('c', 0.0); d = m.get('d', 1.0)
    tx = m.get('tx', 0.0); ty = m.get('ty', 0.0)
    return (a*x + c*y + tx, b*x + d*y + ty)

def shape_to_svg(shape_node: dict, out_path: str, apply_matrix: bool = True,
                 all_shapes: list = None):
    """Render one shape (or a list of shapes) to SVG. If `apply_matrix`, wrap
       each shape's body in a <g transform=matrix(...)> using the shape's own
       matrix. When `all_shapes` is given, compose all those shapes into one SVG.
    """
    shapes = all_shapes if all_shapes is not None else [shape_node]
    # Filter to non-empty
    shapes = [s for s in shapes if s.get('shape', {}).get('byte_edges')]
    if not shapes:
        raise ValueError('no shapes to render')

    defs = []; defs_idx = [0]
    body_parts = []
    all_corners_px = []
    for s in shapes:
        frag, corners = _render_shape_body(s, defs, defs_idx)
        if not frag: continue
        mx = s.get('matrix', {}) or {}
        if apply_matrix:
            tfm = matrix_to_svg(mx)
            body_parts.append(f'<g transform="{tfm}">\n{frag}\n</g>')
            # Transform corners into parent space for the viewBox
            for c in corners:
                all_corners_px.append(_apply_matrix(mx, c))
        else:
            body_parts.append(frag)
            all_corners_px.extend(corners)

    if not all_corners_px:
        raise ValueError('no drawable content')
    xs = [c[0] for c in all_corners_px]
    ys = [c[1] for c in all_corners_px]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    m = max(1, max(maxx-minx, maxy-miny) * 0.05)
    vb = (minx-m, miny-m, (maxx-minx)+2*m, (maxy-miny)+2*m)

    svg = [f'<?xml version="1.0" encoding="UTF-8"?>',
           f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'viewBox="{vb[0]:.2f} {vb[1]:.2f} {vb[2]:.2f} {vb[3]:.2f}">']
    if defs:
        svg.append('<defs>' + ''.join(defs) + '</defs>')
    svg.extend(body_parts)
    svg.append('</svg>')
    open(out_path, 'w').write('\n'.join(svg))
    total_edges = sum(len(s['shape']['byte_edges']) for s in shapes)
    return {'shapes': len(shapes), 'edges': total_edges,
            'viewbox': vb, 'output': out_path, 'gradients': defs_idx[0]}

def main():
    if len(sys.argv) < 3:
        sys.exit('usage: to_svg.py <file.fla> <symbol_id> [out.svg]')
    fla = sys.argv[1]; sid = int(sys.argv[2])
    out = sys.argv[3] if len(sys.argv) > 3 else f'/tmp/sym_{sid}.svg'
    ole = olefile.OleFileIO(fla); data = ole.openstream(f'Symbol {sid}').read(); ole.close()
    result = decoder.decode_symbol_stream(data)
    shapes = find_nonempty_shapes(result['body'])
    if not shapes: sys.exit('no shapes found')
    info = shape_to_svg(shapes[0], out, apply_matrix=True, all_shapes=shapes)
    print(f'✓ {info}')

if __name__ == '__main__':
    main()
