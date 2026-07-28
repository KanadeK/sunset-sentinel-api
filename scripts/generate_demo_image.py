"""Render a terminal-style PNG from the real committed assessment JSON."""

from __future__ import annotations

import argparse
import binascii
import json
import os
import struct
import tempfile
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSESSMENT = PROJECT_ROOT / "docs" / "demo" / "assessment.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "demo" / "sunset-sentinel-dashboard.png"
WIDTH = 1200
HEIGHT = 675

Color = tuple[int, int, int]

BACKGROUND = (5, 12, 20)
PANEL = (10, 24, 36)
PANEL_ALT = (14, 32, 46)
BORDER = (37, 60, 75)
TEXT = (221, 232, 239)
MUTED = (128, 151, 166)
CYAN = (77, 208, 225)
GREEN = (81, 207, 155)
AMBER = (244, 190, 74)
RED = (242, 112, 112)

_FONT_ROWS: dict[str, str] = {
    " ": "00000/00000/00000/00000/00000/00000/00000",
    "A": "01110/10001/10001/11111/10001/10001/10001",
    "B": "11110/10001/10001/11110/10001/10001/11110",
    "C": "01111/10000/10000/10000/10000/10000/01111",
    "D": "11110/10001/10001/10001/10001/10001/11110",
    "E": "11111/10000/10000/11110/10000/10000/11111",
    "F": "11111/10000/10000/11110/10000/10000/10000",
    "G": "01111/10000/10000/10111/10001/10001/01110",
    "H": "10001/10001/10001/11111/10001/10001/10001",
    "I": "11111/00100/00100/00100/00100/00100/11111",
    "J": "00111/00010/00010/00010/00010/10010/01100",
    "K": "10001/10010/10100/11000/10100/10010/10001",
    "L": "10000/10000/10000/10000/10000/10000/11111",
    "M": "10001/11011/10101/10101/10001/10001/10001",
    "N": "10001/11001/10101/10011/10001/10001/10001",
    "O": "01110/10001/10001/10001/10001/10001/01110",
    "P": "11110/10001/10001/11110/10000/10000/10000",
    "Q": "01110/10001/10001/10001/10101/10010/01101",
    "R": "11110/10001/10001/11110/10100/10010/10001",
    "S": "01111/10000/10000/01110/00001/00001/11110",
    "T": "11111/00100/00100/00100/00100/00100/00100",
    "U": "10001/10001/10001/10001/10001/10001/01110",
    "V": "10001/10001/10001/10001/10001/01010/00100",
    "W": "10001/10001/10001/10101/10101/10101/01010",
    "X": "10001/10001/01010/00100/01010/10001/10001",
    "Y": "10001/10001/01010/00100/00100/00100/00100",
    "Z": "11111/00001/00010/00100/01000/10000/11111",
    "0": "01110/10001/10011/10101/11001/10001/01110",
    "1": "00100/01100/00100/00100/00100/00100/01110",
    "2": "01110/10001/00001/00010/00100/01000/11111",
    "3": "11110/00001/00001/01110/00001/00001/11110",
    "4": "00010/00110/01010/10010/11111/00010/00010",
    "5": "11111/10000/10000/11110/00001/00001/11110",
    "6": "01110/10000/10000/11110/10001/10001/01110",
    "7": "11111/00001/00010/00100/01000/01000/01000",
    "8": "01110/10001/10001/01110/10001/10001/01110",
    "9": "01110/10001/10001/01111/00001/00001/01110",
    "-": "00000/00000/00000/11111/00000/00000/00000",
    "_": "00000/00000/00000/00000/00000/00000/11111",
    "/": "00001/00010/00010/00100/01000/01000/10000",
    ":": "00000/00100/00100/00000/00100/00100/00000",
    ".": "00000/00000/00000/00000/00000/00100/00100",
    "#": "01010/11111/01010/01010/11111/01010/00000",
    "?": "01110/10001/00001/00010/00100/00000/00100",
}


class Canvas:
    """Tiny RGB bitmap canvas backed only by Python's standard library."""

    def __init__(self, width: int, height: int, background: Color) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(background) * width * height)

    def rectangle(self, x: int, y: int, width: int, height: int, color: Color) -> None:
        left = max(0, x)
        top = max(0, y)
        right = min(self.width, x + width)
        bottom = min(self.height, y + height)
        if left >= right or top >= bottom:
            return
        row = bytes(color) * (right - left)
        for row_y in range(top, bottom):
            start = (row_y * self.width + left) * 3
            self.pixels[start : start + len(row)] = row

    def circle(self, center_x: int, center_y: int, radius: int, color: Color) -> None:
        squared = radius * radius
        for y in range(center_y - radius, center_y + radius + 1):
            for x in range(center_x - radius, center_x + radius + 1):
                if (x - center_x) ** 2 + (y - center_y) ** 2 <= squared:
                    self.rectangle(x, y, 1, 1, color)

    def text(self, x: int, y: int, value: str, color: Color, *, scale: int = 3) -> None:
        cursor = x
        for character in value.upper():
            rows = _FONT_ROWS.get(character, _FONT_ROWS["?"]).split("/")
            for row_index, row in enumerate(rows):
                for column, pixel in enumerate(row):
                    if pixel == "1":
                        self.rectangle(
                            cursor + column * scale,
                            y + row_index * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += 6 * scale


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(data, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _encode_png(canvas: Canvas, description: str) -> bytes:
    scanlines = bytearray()
    stride = canvas.width * 3
    for y in range(canvas.height):
        scanlines.append(0)
        start = y * stride
        scanlines.extend(canvas.pixels[start : start + stride])
    header = struct.pack(">IIBBBBB", canvas.width, canvas.height, 8, 2, 0, 0, 0)
    metadata = b"Description\x00" + description.encode("latin-1", errors="replace")
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", header),
            _png_chunk(b"tEXt", metadata),
            _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9)),
            _png_chunk(b"IEND", b""),
        )
    )


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("assessment records must be a JSON array")
    return [_as_mapping(record, "assessment record") for record in raw_records]


def _string(mapping: Mapping[str, object], key: str, default: str = "UNKNOWN") -> str:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else default


def _priority(record: Mapping[str, object]) -> int:
    scores = _as_mapping(record.get("scores"), "record scores")
    value = scores.get("priority")
    if not isinstance(value, int):
        raise ValueError("record priority must be an integer")
    return value


def _endpoint(record: Mapping[str, object]) -> str:
    endpoints = record.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        return "SERVICE"
    endpoint = _as_mapping(endpoints[0], "record endpoint")
    return f"{_string(endpoint, 'method')} {_string(endpoint, 'path')}"


def _priority_color(record: Mapping[str, object]) -> Color:
    scores = _as_mapping(record.get("scores"), "record scores")
    band = _string(scores, "priority_band").lower()
    if band in {"critical", "high"}:
        return RED
    if band == "medium":
        return AMBER
    return GREEN


def _fit(value: str, characters: int) -> str:
    normalized = value.upper()
    if len(normalized) <= characters:
        return normalized
    return normalized[: max(0, characters - 2)] + ".."


def _real_counts(records: list[Mapping[str, object]]) -> tuple[int, int]:
    consumers: set[str] = set()
    signal_count = 0
    for record in records:
        raw_consumers = record.get("consumers")
        if isinstance(raw_consumers, list):
            for value in raw_consumers:
                consumer = _as_mapping(value, "record consumer")
                consumers.add(_string(consumer, "id"))
        raw_signals = record.get("signals")
        if isinstance(raw_signals, list):
            signal_count += len(raw_signals)
    return len(consumers), signal_count


def _draw_dashboard(payload: Mapping[str, object]) -> Canvas:
    records = sorted(_records(payload), key=_priority, reverse=True)
    consumers, signals = _real_counts(records)
    generated_at = _string(payload, "generated_at")
    sunsets = sorted(
        value for value in (_string(record, "sunset_at", "") for record in records) if value
    )
    next_sunset = sunsets[0][:10] if sunsets else "NO KNOWN DATE"

    canvas = Canvas(WIDTH, HEIGHT, BACKGROUND)
    canvas.rectangle(38, 28, 1124, 619, PANEL)
    canvas.rectangle(38, 28, 1124, 52, PANEL_ALT)
    canvas.rectangle(38, 79, 1124, 2, BORDER)
    canvas.circle(64, 54, 7, RED)
    canvas.circle(88, 54, 7, AMBER)
    canvas.circle(112, 54, 7, GREEN)
    canvas.text(145, 43, "SUNSET SENTINEL / OFFLINE ASSESSMENT", TEXT, scale=3)
    canvas.text(918, 47, "NO NETWORK", GREEN, scale=2)

    cards = (
        ("RECORDS", str(len(records)), CYAN),
        ("SIGNALS", str(signals), AMBER),
        ("CONSUMERS", str(consumers), GREEN),
        ("NEXT SUNSET", next_sunset, RED),
    )
    for index, (label, value, color) in enumerate(cards):
        x = 62 + index * 272
        canvas.rectangle(x, 104, 248, 104, PANEL_ALT)
        canvas.rectangle(x, 104, 4, 104, color)
        canvas.text(x + 20, 122, label, MUTED, scale=2)
        canvas.text(x + 20, 156, _fit(value, 17), color, scale=3)

    canvas.text(62, 234, "PRIORITY QUEUE", TEXT, scale=3)
    canvas.text(865, 238, "SCORE / STATE / SUNSET", MUTED, scale=2)
    for index, record in enumerate(records[:3]):
        y = 276 + index * 92
        color = _priority_color(record)
        canvas.rectangle(62, y, 1076, 74, PANEL_ALT)
        canvas.rectangle(62, y, 5, 74, color)
        canvas.text(84, y + 14, f"P{_priority(record):02d}", color, scale=3)
        identity = f"{_string(record, 'target_id')}  {_endpoint(record)}"
        canvas.text(190, y + 12, _fit(identity, 41), TEXT, scale=3)
        state = _string(record, "state").replace("_", " ")
        sunset = _string(record, "sunset_at", "NO KNOWN DATE")[:10]
        canvas.text(190, y + 45, _fit(f"{state} / {sunset}", 63), MUTED, scale=2)

    canvas.rectangle(62, 574, 1076, 1, BORDER)
    canvas.text(62, 595, f"GENERATED {generated_at}", MUTED, scale=2)
    canvas.text(868, 595, "BUNDLED THREE-SOURCE DEMO", CYAN, scale=2)
    return canvas


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _project_path(path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a standard-library PNG from a real assessment.json."
    )
    parser.add_argument("--assessment", type=Path, default=DEFAULT_ASSESSMENT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read real assessment values and render the committed dashboard image."""
    args = build_parser().parse_args(argv)
    assessment_path = _project_path(args.assessment)
    output_path = _project_path(args.output)
    loaded = cast(object, json.loads(assessment_path.read_text(encoding="utf-8")))
    payload = _as_mapping(loaded, "assessment")
    records = _records(payload)
    if not records:
        raise ValueError("assessment must contain at least one record")
    png = _encode_png(
        _draw_dashboard(payload),
        f"Sunset Sentinel real offline assessment with {len(records)} records",
    )
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 100:
        raise RuntimeError("generated PNG failed structural validation")
    _atomic_write(output_path, png)
    print(
        json.dumps(
            {
                "assessment": str(assessment_path),
                "bytes": len(png),
                "height": HEIGHT,
                "output": str(output_path),
                "records": len(records),
                "width": WIDTH,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
