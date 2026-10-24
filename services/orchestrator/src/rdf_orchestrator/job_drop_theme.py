"""TDM-108 job-drop card theme: the GeonosisAI brand (``docs/brand/project/tokens.json``), Foundry (dark).

The card renderer (``job_drop_card.py``) reads every colour and font from the ``CardTheme`` this module
returns, so swapping the brand is one place: edit tokens.json (TDM-109 owns it), or point
``RDF_DROP_BRAND_TOKENS`` at another tokens file, or pass a ``CardTheme`` to the renderer.

  * Colours come from the tokens' ``foundry`` values (``RDF_DROP_THEME=dune`` for the light theme).
    Missing file or token -> the neutral fallbacks below (so a checkout without the brand still renders).
  * Fonts are the brand's three families, vendored under ``assets/tdm108_fonts`` (SIL OFL 1.1, licence
    files next to them): Big Shoulders Stencil (wordmark), Big Shoulders (titles, tags), IBM Plex Sans
    (text), IBM Plex Mono (price, ids, links). Variable fonts are pinned to the weight the brand names.
    A font that will not load falls back to Pillow's built-in scalable font.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import ImageFont

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TOKENS = REPO_ROOT / "docs" / "brand" / "project" / "tokens.json"
FONT_DIR = Path(__file__).resolve().parent / "assets" / "tdm108_fonts"

# Neutral fallback (used only when tokens.json is missing a value).
NEUTRAL = {
    "ground": "#121212", "surface": "#1c1c1c", "surface-sunken": "#0b0b0b", "line": "#333333",
    "line-strong": "#6b6b6b", "ink": "#f0f0f0", "ink-muted": "#a8a8a8", "melt": "#ff7a1f",
    "melt-soft": "#3b1d0a", "on-melt": "#1a0d04", "ochre": "#d39457", "ochre-soft": "#33220f",
    "arc": "#79c7ff", "arc-soft": "#0e2738", "steel": "#8f9ca5", "steel-soft": "#222a2f",
}

# family -> (file, variation axes in the file's own axis order, or None for a static file)
FONT_FILES = {
    "stencil": ("BigShouldersStencil-VF.ttf", {"Weight": 800, "Optical size": 72}),
    "display": ("BigShoulders-VF.ttf", {"Weight": 800, "Optical size": 72}),
    "display-bold": ("BigShoulders-VF.ttf", {"Weight": 700, "Optical size": 72}),
    "sans": ("IBMPlexSans-VF.ttf", {"Weight": 400, "Width": 100}),
    "sans-strong": ("IBMPlexSans-VF.ttf", {"Weight": 600, "Width": 100}),
    "mono": ("IBMPlexMono-Medium.ttf", None),
    "mono-regular": ("IBMPlexMono-Regular.ttf", None),
}


@dataclass(frozen=True)
class CardTheme:
    name: str
    product: str  # "GeonosisAI"
    wordmark: tuple[str, str]  # ("Geonosis", "AI"): the second part is set in `melt`
    colors: dict[str, str] = field(default_factory=dict)
    source: str = "neutral"

    def c(self, token: str) -> str:
        return self.colors.get(token) or NEUTRAL.get(token) or "#ff00ff"

    def rgb(self, token: str) -> tuple[int, int, int]:
        h = self.c(token).lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def int_color(self, token: str) -> int:
        r, g, b = self.rgb(token)
        return (r << 16) | (g << 8) | b

    def font(self, family: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        return _font(family, int(size))


def _resolve(colors: dict[str, Any], v: Any, theme: str, depth: int = 0) -> str | None:
    if isinstance(v, dict):
        v = v.get(theme)
    if isinstance(v, str) and v.startswith("{") and v.endswith("}") and depth < 4:
        return _resolve(colors, colors.get(v[1:-1]), theme, depth + 1)
    return v if isinstance(v, str) and v.startswith("#") else None


@lru_cache(maxsize=8)
def _load(path: str, theme: str) -> CardTheme:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return CardTheme(name=theme, product="GeonosisAI", wordmark=("Geonosis", "AI"), colors=dict(NEUTRAL))
    raw = {t.get("name"): t.get("value") for t in (data.get("color") or {}).get("tokens") or [] if t.get("name")}
    colors = dict(NEUTRAL)
    for k, v in raw.items():
        got = _resolve(raw, v, theme)
        if got:
            colors[k] = got
    product = str(data.get("name") or "GeonosisAI")
    head, tail = (product[:-2], product[-2:]) if product.endswith("AI") else (product, "")
    return CardTheme(name=theme, product=product, wordmark=(head, tail), colors=colors, source=str(path))


def load_theme(path: str | Path | None = None, theme: str | None = None) -> CardTheme:
    p = str(path or os.environ.get("RDF_DROP_BRAND_TOKENS") or DEFAULT_TOKENS)
    return _load(p, (theme or os.environ.get("RDF_DROP_THEME") or "foundry").lower())


@lru_cache(maxsize=64)
def _font(family: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    file, axes = FONT_FILES.get(family, FONT_FILES["sans"])
    try:
        f = ImageFont.truetype(str(FONT_DIR / file), size)
        if axes:
            vals = []
            for a in f.get_variation_axes():
                n = a["name"].decode() if isinstance(a["name"], bytes) else str(a["name"])
                vals.append(max(a["minimum"], min(a["maximum"], axes.get(n, a["default"]))))
            f.set_variation_by_axes(vals)
        return f
    except Exception:  # noqa: BLE001 - missing file / no FreeType variation support: still render
        return ImageFont.load_default(size=size)
