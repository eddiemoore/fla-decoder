#!/usr/bin/env python3
r"""
Extract embedded media (audio + bitmaps) from a binary .fla.

Usage:
    python scripts/extract_media.py <file.fla> <out_dir>

Writes:
    <out_dir>/audio/wav/*.wav        named PCM sounds wrapped as WAV
    <out_dir>/audio/mp3/*.mp3        natively-mp3 sounds, raw frames
    <out_dir>/audio/inventory.tsv    audio manifest
    <out_dir>/bitmaps/*.png|*.jpg    raw or DefineBitsLossless bitmaps
    <out_dir>/lossless/*.png         pre-CS5 chunked-lossless bitmaps
"""
from __future__ import annotations
import os, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fla_decoder import audio, bitmaps, lossless


def main():
    if len(sys.argv) != 3:
        sys.exit('usage: extract_media.py <file.fla> <out_dir>')
    fla = Path(sys.argv[1]); out = Path(sys.argv[2])
    print(f'\n# audio')
    audio.extract(fla, out / 'audio')
    print(f'\n# bitmaps (raw + DefineBitsLossless)')
    bitmaps.extract(fla, out / 'bitmaps')
    print(f'\n# lossless (chunked pre-CS5 lossless bitmaps)')
    lossless.extract(fla, out / 'lossless')


if __name__ == '__main__':
    main()
