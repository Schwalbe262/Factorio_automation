from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import struct
from typing import Iterable


@dataclass(frozen=True)
class BmpImage:
    width: int
    height: int
    pixels_bgra: bytes
    bottom_up: bool

    def pixel_bgr(self, x: int, y: int) -> tuple[int, int, int]:
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            raise IndexError((x, y))
        row = self.height - 1 - y if self.bottom_up else y
        offset = (row * self.width + x) * 4
        return (
            self.pixels_bgra[offset],
            self.pixels_bgra[offset + 1],
            self.pixels_bgra[offset + 2],
        )


@dataclass(frozen=True)
class VanillaScreenState:
    kind: str
    confidence: float
    width: int
    height: int
    features: dict[str, float]
    evidence: list[str]
    screenshot_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read_bmp(path: str | Path) -> BmpImage:
    data = Path(path).read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError(f"not a BMP file: {path}")
    pixel_offset = struct.unpack("<I", data[10:14])[0]
    header_size = struct.unpack("<I", data[14:18])[0]
    if header_size < 40:
        raise ValueError("unsupported BMP DIB header")
    width = struct.unpack("<i", data[18:22])[0]
    height_raw = struct.unpack("<i", data[22:26])[0]
    planes = struct.unpack("<H", data[26:28])[0]
    bit_count = struct.unpack("<H", data[28:30])[0]
    compression = struct.unpack("<I", data[30:34])[0]
    if planes != 1 or bit_count != 32 or compression != 0:
        raise ValueError("only uncompressed 32-bit BGRA BMP files are supported")
    height = abs(height_raw)
    expected = width * height * 4
    pixels = data[pixel_offset : pixel_offset + expected]
    if len(pixels) != expected:
        raise ValueError(f"BMP pixel data is truncated: expected {expected}, got {len(pixels)}")
    return BmpImage(width=width, height=height, pixels_bgra=pixels, bottom_up=height_raw > 0)


def classify_bmp_file(path: str | Path) -> VanillaScreenState:
    image = read_bmp(path)
    state = classify_image(image)
    return VanillaScreenState(
        kind=state.kind,
        confidence=state.confidence,
        width=state.width,
        height=state.height,
        features=state.features,
        evidence=state.evidence,
        screenshot_path=str(path),
    )


def classify_image(image: BmpImage) -> VanillaScreenState:
    features = _image_features(image)
    evidence: list[str] = []

    if image.width < 320 or image.height < 240:
        return _state(
            "not_gameplay_capture",
            0.95,
            image,
            features,
            ["capture is smaller than a usable gameplay viewport"],
        )

    if features["left_blue_ratio"] > 0.004 and features["dark_ratio"] > 0.55 and features["terrain_center_ratio"] < 0.12:
        evidence.append("dark Factorio UI with blue left navigation/logo")
        return _state("settings_menu", 0.86, image, features, evidence)

    if features["terrain_center_ratio"] > 0.34 and features["dark_ratio"] < 0.55:
        evidence.append("center viewport is dominated by terrain-like colors")
        return _state("gameplay", 0.78, image, features, evidence)

    if features["dark_ratio"] > 0.58 and features["bright_ratio"] < 0.18:
        evidence.append("dark Factorio-style menu/dialog surface")
        return _state("menu_or_dialog", 0.64, image, features, evidence)

    if features["bright_ratio"] > 0.55 and features["dark_ratio"] < 0.25:
        evidence.append("capture is dominated by bright non-Factorio colors")
        return _state("unknown_or_occluded", 0.62, image, features, evidence)

    evidence.append("no strong Factorio screen signature matched")
    return _state("unknown", 0.35, image, features, evidence)


def _state(
    kind: str,
    confidence: float,
    image: BmpImage,
    features: dict[str, float],
    evidence: list[str],
) -> VanillaScreenState:
    return VanillaScreenState(
        kind=kind,
        confidence=round(confidence, 3),
        width=image.width,
        height=image.height,
        features={key: round(value, 4) for key, value in sorted(features.items())},
        evidence=evidence,
    )


def _image_features(image: BmpImage) -> dict[str, float]:
    all_points = _sample_region(image, 0.0, 0.0, 1.0, 1.0)
    center_points = _sample_region(image, 0.18, 0.12, 0.82, 0.82)
    left_points = _sample_region(image, 0.0, 0.0, 0.28, 1.0)
    bottom_points = _sample_region(image, 0.0, 0.82, 1.0, 1.0)
    return {
        "dark_ratio": _ratio(all_points, _is_dark),
        "bright_ratio": _ratio(all_points, _is_bright),
        "terrain_center_ratio": _ratio(center_points, _is_terrain_like),
        "left_blue_ratio": _ratio(left_points, _is_factorio_blue),
        "bottom_dark_ratio": _ratio(bottom_points, _is_dark),
        "color_variety": _color_variety(all_points),
    }


def _sample_region(
    image: BmpImage,
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    max_samples_per_axis: int = 80,
) -> list[tuple[int, int, int]]:
    x0 = max(0, min(image.width - 1, int(image.width * left)))
    x1 = max(x0 + 1, min(image.width, int(image.width * right)))
    y0 = max(0, min(image.height - 1, int(image.height * top)))
    y1 = max(y0 + 1, min(image.height, int(image.height * bottom)))
    x_step = max(1, (x1 - x0) // max_samples_per_axis)
    y_step = max(1, (y1 - y0) // max_samples_per_axis)
    points: list[tuple[int, int, int]] = []
    for y in range(y0, y1, y_step):
        for x in range(x0, x1, x_step):
            points.append(image.pixel_bgr(x, y))
    return points


def _ratio(points: Iterable[tuple[int, int, int]], predicate) -> float:
    rows = list(points)
    if not rows:
        return 0.0
    return sum(1 for point in rows if predicate(point)) / len(rows)


def _is_dark(bgr: tuple[int, int, int]) -> bool:
    b, g, r = bgr
    return (r + g + b) / 3.0 < 68


def _is_bright(bgr: tuple[int, int, int]) -> bool:
    b, g, r = bgr
    return (r + g + b) / 3.0 > 190


def _is_factorio_blue(bgr: tuple[int, int, int]) -> bool:
    b, g, r = bgr
    return b > 130 and 60 <= g <= 190 and r < 80


def _is_terrain_like(bgr: tuple[int, int, int]) -> bool:
    b, g, r = bgr
    brown = r > 80 and 45 <= g <= 170 and b < 125 and r >= g * 0.85
    green = g > 75 and r < 150 and b < 135 and g > r * 0.8
    ore = abs(r - g) < 45 and abs(g - b) < 55 and 55 <= r <= 185
    return brown or green or ore


def _color_variety(points: Iterable[tuple[int, int, int]]) -> float:
    rows = list(points)
    if not rows:
        return 0.0
    buckets = {
        (b // 32, g // 32, r // 32)
        for b, g, r in rows
    }
    return min(1.0, len(buckets) / 128.0)
