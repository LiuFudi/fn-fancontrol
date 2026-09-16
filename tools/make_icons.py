#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 LiuFudi
"""Generate the fn-fancontrol icon set.

No imaging library is available on this box, so the PNGs are written directly
(RGBA8, zlib) and anti-aliased by supersampling a small analytic scene: a
rounded-square gradient badge with a five-blade fan on top.
"""

import math
import os
import struct
import zlib

BLADES = 5
BLADE_DISTANCE = 0.232
BLADE_A = 0.225          # semi-axis along the blade
BLADE_B = 0.108          # semi-axis across the blade
BLADE_SWEEP = math.radians(38.0)
HUB_RADIUS = 0.115
RING_INNER = 0.415
RING_OUTER = 0.468

TOP = (0x3B, 0x82, 0xF6)
BOTTOM = (0x18, 0x3F, 0xB0)


def write_png(path, width, height, rows):
    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", header))
        handle.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        handle.write(chunk(b"IEND", b""))


def rounded_alpha(x, y, radius):
    """Coverage of a rounded square centred at the origin, half-size 1."""
    dx = abs(x)
    dy = abs(y)
    if dx <= 1.0 - radius or dy <= 1.0 - radius:
        return 1.0 if (dx <= 1.0 and dy <= 1.0) else 0.0
    corner_x = 1.0 - radius
    corner_y = 1.0 - radius
    dist = math.hypot(dx - corner_x, dy - corner_y)
    return 1.0 if dist <= radius else 0.0


def in_ellipse(px, py, cx, cy, a, b, angle):
    dx = px - cx
    dy = py - cy
    ca = math.cos(-angle)
    sa = math.sin(-angle)
    u = dx * ca - dy * sa
    v = dx * sa + dy * ca
    return (u / a) ** 2 + (v / b) ** 2 <= 1.0


def inside_fan(px, py):
    """Analytic test for the white part of the glyph, in unit coordinates."""
    radius = math.hypot(px, py)
    if RING_INNER <= radius <= RING_OUTER:
        # leave four gaps in the ring so it reads as a fan housing
        angle = math.atan2(py, px) % (2 * math.pi)
        for k in range(4):
            gap = k * (math.pi / 2) + math.pi / 4
            if abs((angle - gap + math.pi) % (2 * math.pi) - math.pi) < 0.16:
                return False
        return True

    if radius <= HUB_RADIUS:
        return True

    for i in range(BLADES):
        theta = i * (2 * math.pi / BLADES)
        cx = BLADE_DISTANCE * math.cos(theta)
        cy = BLADE_DISTANCE * math.sin(theta)
        if in_ellipse(px, py, cx, cy, BLADE_A, BLADE_B, theta + BLADE_SWEEP):
            return True
    return False


def render(size, supersample=4):
    total = size * supersample
    accum = [[0.0, 0.0, 0.0, 0.0] for _ in range(size * size)]
    step = 2.0 / total
    inv = 1.0 / (supersample * supersample)

    # Precompute the sub-sample offsets once.
    offsets = [(-1.0 + (i + 0.5) * step) for i in range(total)]

    for row in range(total):
        y = offsets[row]
        glyph_row = row // supersample
        for col in range(total):
            x = offsets[col]
            alpha = rounded_alpha(x, y, 0.34)
            if alpha <= 0.0:
                continue

            # background gradient (top lighter)
            mix = (y + 1.0) / 2.0
            bg = (
                TOP[0] + (BOTTOM[0] - TOP[0]) * mix,
                TOP[1] + (BOTTOM[1] - TOP[1]) * mix,
                TOP[2] + (BOTTOM[2] - TOP[2]) * mix,
            )

            ux = x / 0.86
            uy = y / 0.86
            if inside_fan(ux, uy):
                colour = (255.0, 255.0, 255.0)
            else:
                colour = bg

            cell = glyph_row * size + (col // supersample)
            slot = accum[cell]
            slot[0] += colour[0]
            slot[1] += colour[1]
            slot[2] += colour[2]
            slot[3] += 255.0 * alpha

    rows = []
    for row in range(size):
        buf = bytearray()
        for col in range(size):
            r, g, b, a = accum[row * size + col]
            if a <= 0.0:
                buf += b"\x00\x00\x00\x00"
                continue
            # colours were only accumulated where alpha > 0, so divide by the
            # number of covered samples rather than by the total.
            covered = a / 255.0
            if covered <= 0.0:
                buf += b"\x00\x00\x00\x00"
                continue
            buf += bytes(
                (
                    int(min(255.0, r / covered) + 0.5),
                    int(min(255.0, g / covered) + 0.5),
                    int(min(255.0, b / covered) + 0.5),
                    int(min(255.0, a * inv) + 0.5),
                )
            )
        rows.append(bytes(buf))
    return rows


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    project = os.path.join(os.path.dirname(here), "package")
    ui_images = os.path.join(project, "app", "ui", "images")
    os.makedirs(ui_images, exist_ok=True)

    targets = [
        (os.path.join(project, "ICON.PNG"), 64, 8),
        (os.path.join(project, "ICON_256.PNG"), 256, 4),
        (os.path.join(ui_images, "icon_64.png"), 64, 8),
        (os.path.join(ui_images, "icon_256.png"), 256, 4),
    ]
    for path, size, ss in targets:
        rows = render(size, ss)
        write_png(path, size, size, rows)
        print("wrote %-58s %dx%d  %d bytes" % (path, size, size, os.path.getsize(path)))


if __name__ == "__main__":
    main()
