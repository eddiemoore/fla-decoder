"""
fla-decoder — open-source decoder for pre-CS5 binary Adobe Flash .fla files.

The pre-XFL binary FLA format (Flash MX through CS4) is an OLE2 compound
document containing MFC-serialized object trees. This package decodes those
trees into Python dicts, then renders shape geometry to SVG and extracts
embedded audio/bitmap media.

Public API:
    decoder.decode_symbol_stream(bytes)             — parse one Symbol stream
    to_svg.shape_to_svg(shape_node, out_path, ...)  — render a shape to SVG
    audio.extract(fla_path, out_dir)                — pull WAV/MP3 audio
    lossless.extract(fla_path, out_dir)             — pull lossless bitmaps
    bitmaps.extract(fla_path, out_dir)              — pull JPEG/PNG bitmaps
"""
from . import decoder, to_svg
