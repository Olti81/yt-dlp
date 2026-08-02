#!/usr/bin/env python3
"""
Draw the application icon and write assets/icon.ico + assets/icon.png.

There is no image library in this project, so the skull is defined with
superellipses and rasterised here by supersampling. Run it after changing any
of the shape constants:

    python tools/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4

OUTLINE = (0x15, 0x11, 0x1D)
BONE_TOP = (0xFA, 0xF7, 0xEF)
BONE_BOTTOM = (0xC3, 0xB9, 0xA4)
SOCKET = (0x12, 0x0F, 0x19)
GLOW = (0x38, 0xBD, 0xF8)


def superellipse(x, y, cx, cy, rx, ry, n, shrink=0.0):
    rx, ry = rx - shrink, ry - shrink
    if rx <= 0 or ry <= 0:
        return False
    return (abs((x - cx) / rx) ** n + abs((y - cy) / ry) ** n) <= 1.0


def rotate(x, y, cx, cy, degrees):
    import math
    a = math.radians(degrees)
    dx, dy = x - cx, y - cy
    return (cx + dx * math.cos(a) - dy * math.sin(a),
            cy + dx * math.sin(a) + dy * math.cos(a))


def in_skull(x, y, shrink=0.0):
    """A wide rounded cranium over a narrower jaw, so the join reads as a cheekbone."""
    cranium = superellipse(x, y, 0.500, 0.400, 0.345, 0.330, 2.15, shrink)
    jaw = superellipse(x, y, 0.500, 0.700, 0.190, 0.200, 2.45, shrink)
    return cranium or jaw


def in_socket(x, y, shrink=0.0):
    for cx, tilt in ((0.352, 14), (0.648, -14)):
        rx, ry = rotate(x, y, cx, 0.425, tilt)
        if superellipse(rx, ry, cx, 0.425, 0.118, 0.128, 2.3, shrink):
            return True
    return False


def in_nose(x, y, shrink=0.0):
    """An upside-down heart: narrow at the top, flaring at the bottom."""
    if not (0.498 + shrink <= y <= 0.626 - shrink):
        return False
    t = (y - 0.498) / 0.128                      # 0 at the top, 1 at the base
    half = (0.010 + 0.060 * t * t) - shrink
    return abs(x - 0.500) <= half


def in_mouth(x, y, shrink=0.0):
    """A dark slot across the jaw; the teeth are cut out of it."""
    return superellipse(x, y, 0.500, 0.738, 0.146, 0.066, 2.9, shrink)


def is_tooth(x, y, gap):
    pitch = 0.0486
    offset = (x - 0.500 + pitch / 2) % pitch
    return gap <= offset <= pitch - gap


def shade(y):
    t = max(0.0, min(1.0, (y - 0.10) / 0.80))
    return tuple(int(a + (b - a) * t) for a, b in zip(BONE_TOP, BONE_BOTTOM))


def sample(x, y, outline_w, tooth_w, detail):
    """Return (r, g, b, a) for one sub-sample."""
    if not in_skull(x, y):
        return (0, 0, 0, 0)
    if not in_skull(x, y, outline_w):
        return (*OUTLINE, 255)

    if in_socket(x, y):
        if not detail:
            return (*SOCKET, 255)   # tiny sizes need contrast, not decoration
        if not in_socket(x, y, outline_w * 0.9):
            return (*OUTLINE, 255)
        depth = max(0.0, min(1.0, (y - 0.330) / 0.230))
        mix = 0.55 * depth * depth
        return (*[int(a + (b - a) * mix) for a, b in zip(SOCKET, GLOW)], 255)

    if in_nose(x, y):
        return (*SOCKET, 255)

    if in_mouth(x, y):
        if detail and is_tooth(x, y, tooth_w):
            return (*shade(y), 255)
        return (*SOCKET, 255)

    return (*shade(y), 255)


def render(size: int) -> bytes:
    outline_w = max(0.016, 0.048 - size * 0.00012)
    tooth_w = 0.008 if size >= 64 else 0.011
    detail = size >= 24
    step = 1.0 / (size * SUPERSAMPLE)
    out = bytearray()

    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SUPERSAMPLE):
                y = (py + (sy + 0.5) / SUPERSAMPLE) / size
                for sx in range(SUPERSAMPLE):
                    x = (px + (sx + 0.5) / SUPERSAMPLE) / size
                    sr, sg, sb, sa = sample(x, y, outline_w, tooth_w, detail)
                    r += sr * sa; g += sg * sa; b += sb * sa; a += sa
            if a:
                out += bytes((r // a, g // a, b // a, a // (SUPERSAMPLE ** 2)))
            else:
                out += b"\x00\x00\x00\x00"
    del step
    return bytes(out)


def write_png(path: Path, size: int, rgba: bytes):
    raw = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b""))


def ico_image(size: int, rgba: bytes) -> bytes:
    """One BMP-encoded ICO entry: 32bpp BGRA, bottom-up, plus an AND mask."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4:(y + 1) * size * 4]
        for i in range(0, len(row), 4):
            r, g, b, a = row[i:i + 4]
            pixels += bytes((b, g, r, a))

    mask_stride = ((size + 31) // 32) * 4        # 1bpp, padded to 4 bytes
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(size):
            if rgba[(y * size + x) * 4 + 3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)  # 1 = transparent
        mask += bits
    return header + bytes(pixels) + bytes(mask)


def main():
    ASSETS.mkdir(exist_ok=True)
    images = []
    for size in SIZES:
        rgba = render(size)
        images.append((size, ico_image(size, rgba)))
        if size == 256:
            write_png(ASSETS / "icon.png", size, rgba)
        print(f"  rendered {size}x{size}")

    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs = b"", b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                               len(blob), offset)
        blobs += blob
        offset += len(blob)

    (ASSETS / "icon.ico").write_bytes(directory + entries + blobs)
    print(f"wrote {ASSETS / 'icon.ico'} and {ASSETS / 'icon.png'}")


if __name__ == "__main__":
    main()
