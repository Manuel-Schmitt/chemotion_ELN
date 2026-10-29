#!/usr/bin/env python3
"""Generate a standalone, printable Chemotion Inventory Data Matrix SVG label."""

import argparse
import sys
import textwrap
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

PREFIX = "ci1|"
FIELDS = ("building", "room", "cabinet", "group", "borrower")
MAX_TEXT_LENGTH = 100
MAX_PAYLOAD_BYTES = 2048


class LabelError(ValueError):
    pass


def validate_text(name: str, value: str) -> str:
    if (not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise LabelError(f"{name} must be trimmed text of at most {MAX_TEXT_LENGTH} characters without controls.")
    return value


def escape_value(value: str) -> str:
    return value.replace("%", "%25").replace("|", "%7C")


def build_payload(fields: dict[str, str], collection_id: int | None = None) -> str:
    if set(fields) != set(FIELDS):
        raise LabelError("Supply exactly building, room, cabinet, group and borrower values.")
    values = [validate_text(name, fields[name]) for name in FIELDS]
    if not any(values):
        raise LabelError("A destination label must supply at least one nonblank location, group or borrower value.")
    if collection_id is not None and (type(collection_id) is not int or collection_id <= 0):
        raise LabelError("Collection ID must be a positive integer.")
    collection_slot = "" if collection_id is None else str(collection_id)
    payload = PREFIX + "|".join([*(escape_value(value) for value in values), collection_slot])
    try:
        payload_bytes = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LabelError("Destination labels must contain valid UTF-8 text.") from exc
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise LabelError(f"Label payload exceeds {MAX_PAYLOAD_BYTES} UTF-8 bytes.")
    return payload


def matrix_path(pixels: bytes, width: int, height: int) -> str:
    if len(pixels) != width * height * 3:
        raise LabelError("Data Matrix encoder returned invalid RGB pixels.")
    paths = []
    start = 0
    while start < height:
        row = pixels[start * width * 3:(start + 1) * width * 3]
        end = start + 1
        while end < height and pixels[end * width * 3:(end + 1) * width * 3] == row:
            end += 1
        column = 0
        while column < width:
            color = row[column * 3:(column + 1) * 3]
            if color == b"\xff\xff\xff":
                column += 1
                continue
            if color != b"\x00\x00\x00":
                raise LabelError("Data Matrix encoder returned a non-monochrome symbol.")
            right = column + 1
            while right < width and row[right * 3:(right + 1) * 3] == color:
                right += 1
            paths.append(f"M{column} {start}h{right - column}v{end - start}h{column - right}z")
            column = right
        start = end
    return "".join(paths)


def caption_lines(fields: dict[str, str], collection_id: int | None) -> list[str]:
    lines = []
    for label, field in (("Building", "building"), ("Room", "room"), ("Cabinet", "cabinet"),
                         ("Group", "group"), ("Borrower", "borrower")):
        if fields[field]:
            lines.extend(textwrap.wrap(f"{label}: {fields[field]}", width=48) or [""])
    if collection_id is not None:
        lines.append(f"Collection: {collection_id}")
    lines.append("Blank fields clear")
    return lines


def text_width(text: str, font_size: float) -> float:
    width = 0.0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += font_size if unicodedata.east_asian_width(char) in ("F", "W") else font_size * 0.62
    return width


def write_label(fields: dict[str, str], collection_id: int | None, output: Path) -> None:
    payload = build_payload(fields, collection_id)
    try:
        from pylibdmtx.pylibdmtx import PyLibDMTXError, encode
    except ImportError as exc:
        raise LabelError("Install pylibdmtx and the operating-system libdmtx package to generate Data Matrix labels.") from exc
    try:
        symbol = encode(payload.encode("utf-8"))
    except PyLibDMTXError as exc:
        raise LabelError("Could not encode the Data Matrix label. Check payload size and the local libdmtx installation.") from exc
    if symbol.bpp != 24:
        raise LabelError("Data Matrix encoder did not return a 24-bit RGB image.")

    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)
    quiet = 1
    lines = caption_lines(fields, collection_id)
    matrix_width = symbol.width + quiet * 2
    font_size = 8
    width = max(matrix_width, int(max(text_width(line, font_size) for line in lines) + 8))
    matrix_x = (width - matrix_width) / 2
    matrix_height = symbol.height + quiet * 2
    gap, line_height = 5, 11
    total_height = matrix_height + gap + line_height * len(lines)
    svg = ET.Element(f"{{{namespace}}}svg", {
        "version": "1.1",
        "width": f"{width / 5}mm",
        "height": f"{total_height / 5}mm",
        "viewBox": f"0 0 {width} {total_height}",
    })
    title = " | ".join(line for line in lines if line != "Blank fields clear")
    ET.SubElement(svg, f"{{{namespace}}}title").text = title or "Inventory destination"
    ET.SubElement(svg, f"{{{namespace}}}rect", {
        "width": str(width), "height": str(total_height), "fill": "white",
    })
    ET.SubElement(svg, f"{{{namespace}}}path", {
        "d": matrix_path(symbol.pixels, symbol.width, symbol.height),
        "transform": f"translate({matrix_x + quiet:g} {quiet})",
        "fill": "black",
    })
    for index, line in enumerate(lines):
        text = ET.SubElement(svg, f"{{{namespace}}}text", {
            "x": str(width / 2),
            "y": str(matrix_height + gap + 9 + index * line_height),
            "text-anchor": "middle",
            "font-family": "sans-serif",
            "font-size": str(font_size),
            "fill": "black",
        })
        text.text = line
    with output.open("xb") as stream:
        ET.ElementTree(svg).write(stream, encoding="utf-8", xml_declaration=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Generate a printable compact Chemotion Inventory Data Matrix SVG")
    result.add_argument("kind", choices=("location", "person"),
                        help="Operator-facing label category; both create identical destination payloads")
    for field in FIELDS:
        result.add_argument(f"--{field}", default="")
    result.add_argument("--collection-id", type=int,
                        help="Optional inventory collection ID; required when the label will start an audit")
    result.add_argument("--output", type=Path, required=True, help="New SVG output path; existing files are not overwritten")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    fields = {field: getattr(args, field) for field in FIELDS}
    try:
        write_label(fields, args.collection_id, args.output)
    except (LabelError, OSError) as exc:
        print(f"Inventory label: {exc}", file=sys.stderr)
        return 1
    print(f"Created {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
