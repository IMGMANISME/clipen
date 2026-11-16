"""Small, dependency-free visual fingerprints for Clipen-generated PNG previews."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GRID_SIZE = 8


@dataclass(frozen=True, slots=True)
class VisualFingerprint:
    cells: tuple[int, ...]
    mean_luma: int

    def is_similar_to(self, other: "VisualFingerprint") -> bool:
        if len(self.cells) != len(other.cells):
            return False
        mean_delta = abs(self.mean_luma - other.mean_luma) / 255
        cell_delta = sum(abs(a - b) for a, b in zip(self.cells, other.cells)) / (len(self.cells) * 255)
        return mean_delta <= 0.10 and cell_delta <= 0.085


def fingerprint_png(path: Path) -> VisualFingerprint:
    """Fingerprint an RGB8 PNG emitted by Clipen's controlled FFmpeg pipeline."""

    width, height, pixels = _decode_rgb8_png(path.read_bytes())
    cells = []
    for grid_y in range(_GRID_SIZE):
        y0 = grid_y * height // _GRID_SIZE
        y1 = max(y0 + 1, (grid_y + 1) * height // _GRID_SIZE)
        for grid_x in range(_GRID_SIZE):
            x0 = grid_x * width // _GRID_SIZE
            x1 = max(x0 + 1, (grid_x + 1) * width // _GRID_SIZE)
            total = count = 0
            for y in range(y0, y1):
                row = y * width * 3
                for x in range(x0, x1):
                    offset = row + x * 3
                    red, green, blue = pixels[offset : offset + 3]
                    total += (77 * red + 150 * green + 29 * blue) >> 8
                    count += 1
            cells.append(round(total / count))
    return VisualFingerprint(tuple(cells), round(sum(cells) / len(cells)))


def _decode_rgb8_png(data: bytes) -> tuple[int, int, bytes]:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("preview is not a PNG")
    width = height = None
    compressed = bytearray()
    position = len(_PNG_SIGNATURE)
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("PNG chunk is truncated")
        payload = data[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            if len(payload) != 13:
                raise ValueError("preview PNG header is invalid")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if (
                not width
                or not height
                or width > 512
                or height > 512
                or depth != 8
                or color != 2
                or compression
                or filtering
                or interlace
            ):
                raise ValueError("unsupported preview PNG layout")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
        position = end
    if width is None or height is None or not compressed:
        raise ValueError("preview PNG is incomplete")

    stride = width * 3
    expected_size = height * (stride + 1)
    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(bytes(compressed), expected_size + 1)
    except zlib.error as error:
        raise ValueError("preview PNG compression is invalid") from error
    if len(decoded) != expected_size or not decompressor.eof or decompressor.unconsumed_tail:
        raise ValueError("preview PNG data length is invalid")
    rows: list[bytearray] = []
    for row_index in range(height):
        start = row_index * (stride + 1)
        filter_type = decoded[start]
        raw = decoded[start + 1 : start + 1 + stride]
        previous = rows[-1] if rows else bytearray(stride)
        row = _unfilter(raw, previous, filter_type, 3)
        rows.append(row)
    return width, height, b"".join(rows)


def _unfilter(raw: bytes, previous: bytearray, filter_type: int, bpp: int) -> bytearray:
    row = bytearray(len(raw))
    for index, value in enumerate(raw):
        left = row[index - bpp] if index >= bpp else 0
        above = previous[index]
        upper_left = previous[index - bpp] if index >= bpp else 0
        if filter_type == 0:
            predictor = 0
        elif filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = above
        elif filter_type == 3:
            predictor = (left + above) // 2
        elif filter_type == 4:
            predictor = _paeth(left, above, upper_left)
        else:
            raise ValueError("unsupported PNG filter")
        row[index] = (value + predictor) & 0xFF
    return row


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = abs(estimate - left), abs(estimate - above), abs(estimate - upper_left)
    if distances[0] <= distances[1] and distances[0] <= distances[2]:
        return left
    return above if distances[1] <= distances[2] else upper_left
