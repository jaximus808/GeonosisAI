"""TDM-108 job-drop share card: a PNG that says what the paid task is, what it pays, how long it takes, shows
the robot's scene, and carries a QR code + short link to the task.

    png = render_card(info, size="wide", link="https://.../j/12/x", short="host/j/12/x", frame=img,
                      frame_label="SIM SCENE  TOP VIEW")

Sizes: ``wide`` 1200x675 (X, Bluesky, link previews), ``square`` 1080x1080 (Discord, Telegram, Instagram).
Style: GeonosisAI Foundry (``job_drop_theme``): ``ground`` page, ``surface`` plates with a 1px ``line``
border, wordmark in Big Shoulders Stencil with a ``melt`` "AI", task title in Big Shoulders, price in
``ochre`` IBM Plex Mono, "Test mode" in the footer while Stripe runs on a test key.

The frame is, in order of preference (``frame_for`` in ``job_drops.py``): the robot's reset start photo
(a real camera frame), else a top-down sketch of the ticket's scene spec drawn here (``scene_sketch``),
labelled as a sketch, never passed off as a photo.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Union

import qrcode
from PIL import Image, ImageDraw

from .job_drop_theme import CardTheme, load_theme

SIZES = {"wide": (1200, 675), "square": (1080, 1080)}
FrameSource = Union[Image.Image, Callable[[int, int], Image.Image], None]  # an image, or draw-to-size


@dataclass
class CardInfo:
    task_id: int
    kind: str  # "Recovery takeover"
    title: str  # what the person does (the ticket's task sentence)
    instructions: str = ""
    price: str = "$0.00"
    price_note: str = "per accepted episode"
    facts: list[str] = field(default_factory=list)  # ["About 2 min", "yam-01", "From your phone"]
    count: int = 1  # tasks in this drop (a plan posts several at once)
    access_code: str | None = None  # shown only with RDF_DROP_SHOW_ACCESS_CODE=1
    test_mode: bool = True
    sample: bool = False  # a test drop with no task behind it


# --------------------------------------------------------------------------- text helpers

def _w(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def wrap(draw: ImageDraw.ImageDraw, text: str, font, width: float, max_lines: int) -> list[str]:
    words = " ".join((text or "").split()).split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        if not w:
            continue
        nxt = f"{cur} {w}".strip()
        if _w(draw, nxt, font) <= width:
            cur = nxt
            continue
        if cur:
            lines.append(cur)
        cur = w
        while _w(draw, cur, font) > width and len(cur) > 1:  # one very long word
            cut = len(cur)
            while cut > 1 and _w(draw, cur[:cut] + "-", font) > width:
                cut -= 1
            lines.append(cur[:cut] + "-")
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _w(draw, last + "...", font) > width:
            last = last[:-1].rstrip()
        lines[-1] = last.rstrip(" .,;:") + "..."
    return lines


def _fit(draw: ImageDraw.ImageDraw, theme: CardTheme, family: str, text: str, width: float, max_lines: int,
         sizes: tuple[int, ...]) -> tuple[Any, list[str], int]:
    """Largest size in ``sizes`` at which ``text`` fits in ``max_lines`` without an ellipsis."""
    for s in sizes:
        f = theme.font(family, s)
        lines = wrap(draw, text, f, width, max_lines)
        if not lines or not lines[-1].endswith("...") or s == sizes[-1]:
            return f, lines, s
    f = theme.font(family, sizes[-1])
    return f, wrap(draw, text, f, width, max_lines), sizes[-1]


# --------------------------------------------------------------------------- pieces

def qr_side_px(data: str, px: int, border: int = 2) -> int:
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    q.add_data(data)
    q.make(fit=True)
    n = len(q.get_matrix()) + 2 * border
    return n * max(1, px // n)


def qr_image(data: str, px: int, dark: tuple[int, int, int], light: tuple[int, int, int], border: int = 2) -> Image.Image:
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    q.add_data(data)
    q.make(fit=True)
    m = q.get_matrix()
    n = len(m) + 2 * border
    scale = max(1, px // n)
    img = Image.new("RGB", (n * scale, n * scale), light)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(m):
        for x, on in enumerate(row):
            if on:
                x0, y0 = (x + border) * scale, (y + border) * scale
                d.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill=dark)
    return img


def _tag_width(draw: ImageDraw.ImageDraw, theme: CardTheme, text: str, size: int) -> int:
    f = theme.font("display-bold", size)
    label = text.upper()
    tw = sum(_w(draw, ch, f) for ch in label) + size * 0.12 * (len(label) - 1)
    pad, riv, gap = int(size * 0.6), max(6, size // 3), int(size * 0.35)
    return int(pad + riv + gap + tw + pad)


def _tag(draw: ImageDraw.ImageDraw, theme: CardTheme, x: int, y: int, text: str, size: int,
         fg: str = "ochre", bg: str = "ochre-soft") -> int:
    """A brand status tag: square rivet + uppercase tracked Big Shoulders. Returns its right edge."""
    f = theme.font("display-bold", size)
    label = text.upper()
    track = size * 0.12
    pad, riv, gap = int(size * 0.6), max(6, size // 3), int(size * 0.35)
    h = int(size * 1.7)
    w = _tag_width(draw, theme, text, size)
    draw.rectangle([x, y, x + w, y + h], fill=theme.c(bg))
    ry = y + (h - riv) // 2
    draw.rectangle([x + pad, ry, x + pad + riv - 1, ry + riv - 1], fill=theme.c(fg))
    cx = x + pad + riv + gap
    for ch in label:
        draw.text((cx, y + h / 2), ch, font=f, fill=theme.c(fg), anchor="lm")
        cx += _w(draw, ch, f) + track
    return x + w


def _wordmark(draw: ImageDraw.ImageDraw, theme: CardTheme, x: int, y: int, size: int) -> int:
    f = theme.font("stencil", size)
    head, tail = theme.wordmark
    track = size * 0.02
    cx = float(x)
    for part, color in ((head.upper(), "ink"), (tail.upper(), "melt")):
        for ch in part:
            draw.text((cx, y), ch, font=f, fill=theme.c(color), anchor="ls")
            cx += _w(draw, ch, f) + track
    return int(cx)


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    sw, sh = img.size
    s = max(w / sw, h / sh)
    r = img.convert("RGB").resize((max(1, int(sw * s)), max(1, int(sh * s))), Image.LANCZOS)
    left, top = (r.width - w) // 2, (r.height - h) // 2
    return r.crop((left, top, left + w, top + h))


def _rounded_paste(base: Image.Image, img: Image.Image, xy: tuple[int, int], radius: int) -> None:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=radius, fill=255)
    base.paste(img, xy, mask)


def _hit(a: tuple, b: tuple, pad: float = 5) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def _short_label(text: str, limit: int = 16) -> str:
    words = text.replace("_", " ").split()
    if words and words[0].lower() == "the":
        words = words[1:]
    out = ""
    for w in words:
        if len(f"{out} {w}".strip()) > limit:
            break
        out = f"{out} {w}".strip()
    return out or text[:limit]


def scene_sketch(spec: Mapping[str, Any] | None, w: int, h: int, theme: CardTheme | None = None) -> Image.Image:
    """Top-down sketch of a scene spec v1 (robot base at the bottom centre, forward = up)."""
    theme = theme or load_theme()
    img = Image.new("RGB", (w, h), theme.c("surface-sunken"))
    d = ImageDraw.Draw(img)
    spec = spec or {}
    table = spec.get("table") or {"center": [0.30, 0.0], "size": [0.96, 0.96]}
    (cx, cy), (sx, sy) = table.get("center", [0.30, 0.0]), table.get("size", [0.96, 0.96])
    # world (x fwd, y left) -> image (u right, v down): u = -y, v = -x. Zoom to the work area (the arm's
    # base, every object and region), not the whole table, so small objects stay readable.
    xs, ys = [-0.06, 0.06], [-0.06, 0.06]
    for o in spec.get("objects") or []:
        try:
            r = float(o.get("radius") or max(o.get("size") or [0.03]) / 2 or 0.03)
            xs += [float(o["pose"]["pos"][0]) - r, float(o["pose"]["pos"][0]) + r]
            ys += [float(o["pose"]["pos"][1]) - r, float(o["pose"]["pos"][1]) + r]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    for r in (spec.get("regions") or {}).values():
        try:
            xs += [float(v) for v in r["x"]]
            ys += [float(v) for v in r["y"]]
        except (KeyError, TypeError, ValueError):
            continue
    m = 0.07
    x_lo, x_hi = max(min(xs) - m, cx - sx / 2 - 0.02), min(max(xs) + m, cx + sx / 2 + 0.02)
    y_lo, y_hi = max(min(ys) - m, cy - sy / 2 - 0.02), min(max(ys) + m, cy + sy / 2 + 0.02)
    # keep the well's aspect so the sketch fills it
    span_u, span_v = y_hi - y_lo, x_hi - x_lo
    if span_u / span_v < w / h:
        grow = (span_v * w / h - span_u) / 2
        y_lo, y_hi = y_lo - grow, y_hi + grow
    else:
        grow = (span_u * h / w - span_v) / 2
        x_lo, x_hi = x_lo - grow, x_hi + grow
    pad = 0.04 * min(w, h)
    s = min((w - 2 * pad) / (y_hi - y_lo), (h - 2 * pad) / (x_hi - x_lo))
    u0 = w / 2 + (y_hi + y_lo) / 2 * s
    v0 = h / 2 + (x_hi + x_lo) / 2 * s

    def P(x: float, y: float) -> tuple[float, float]:
        return u0 - y * s, v0 - x * s

    def rgb(c: Any, fallback: str) -> Any:
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            return tuple(int(max(0.0, min(1.0, float(v))) * 255) for v in c[:3])
        return theme.c(fallback)

    a, b = P(cx + sx / 2, cy + sy / 2), P(cx - sx / 2, cy - sy / 2)
    d.rectangle([min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])], fill=theme.c("surface"),
                outline=theme.c("line-strong"), width=2)
    # reach band (dotted arc at the max reach)
    rmax = 0.55
    for deg in range(-80, 81, 4):
        t = math.radians(deg)
        u, v = P(rmax * math.cos(t), rmax * math.sin(t))
        d.rectangle([u - 1.5, v - 1.5, u + 1.5, v + 1.5], fill=theme.c("line-strong"))
    label_font = theme.font("mono-regular", max(13, min(20, int(h * 0.05))))
    placed: list[tuple[float, float, float, float]] = []
    region_labels: list[tuple[tuple[float, float], str]] = []  # drawn after the objects' labels
    for name, r in (spec.get("regions") or {}).items():
        try:
            (xa, xb), (ya, yb) = r["x"], r["y"]
        except (KeyError, TypeError, ValueError):
            continue
        p, q = P(xb, yb), P(xa, ya)
        box = [min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])]
        col = rgb(r.get("color"), "steel")
        # dashed outline
        for (u1, v1, u2, v2) in ((box[0], box[1], box[2], box[1]), (box[2], box[1], box[2], box[3]),
                                 (box[2], box[3], box[0], box[3]), (box[0], box[3], box[0], box[1])):
            n = max(1, int(math.hypot(u2 - u1, v2 - v1) // 10))
            for i in range(0, n, 2):
                t1, t2 = i / n, min(1.0, (i + 1) / n)
                d.line([u1 + (u2 - u1) * t1, v1 + (v2 - v1) * t1, u1 + (u2 - u1) * t2, v1 + (v2 - v1) * t2],
                       fill=col, width=2)
        region_labels.append(((box[0] + 4, box[3] - 3), str(r.get("label") or name).replace("_", " ")))
    for o in spec.get("objects") or []:
        try:
            x, y = float(o["pose"]["pos"][0]), float(o["pose"]["pos"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        col = rgb(o.get("color"), "steel")
        prim = o.get("primitive")
        if prim == "box":
            hx, hy = float(o["size"][0]) / 2, float(o["size"][1]) / 2
            yaw = float(o["pose"].get("yaw") or 0.0)
            pts = []
            for dx, dy in ((hx, hy), (hx, -hy), (-hx, -hy), (-hx, hy)):
                pts.append(P(x + dx * math.cos(yaw) - dy * math.sin(yaw), y + dx * math.sin(yaw) + dy * math.cos(yaw)))
            d.polygon(pts, fill=col, outline=theme.c("ink"))
        else:
            rad = float(o.get("radius") or 0.03) * s
            u, v = P(x, y)
            if prim == "plate":
                d.ellipse([u - rad, v - rad, u + rad, v + rad], fill=rgb(o.get("rim_color") or o.get("color"), "steel"),
                          outline=theme.c("ink"), width=2)
                ri = rad * 0.72
                d.ellipse([u - ri, v - ri, u + ri, v + ri], fill=col)
            else:
                d.ellipse([u - rad, v - rad, u + rad, v + rad], fill=col, outline=theme.c("ink"))
        u, v = P(x, y)
        full = str(o.get("label") or o.get("id") or "")
        lab = _short_label(full)
        if len(lab) < len(full.removeprefix("the ").strip()) and o.get("id"):
            lab = _short_label(str(o["id"]))  # a cut-off label reads worse than the id ("full plate")
        if lab:
            ry = float(o.get("radius") or max(o.get("size") or [0.03]) / 2 or 0.03) * s
            box = d.textbbox((u, v + ry + 6), lab, font=label_font, anchor="mt")
            if not any(_hit(box, b2) for b2 in placed):
                placed.append(box)
                d.text((u, v + ry + 6), lab, font=label_font, fill=theme.c("ink"), anchor="mt")
    for at, text in region_labels:
        lb = d.textbbox(at, text, font=label_font, anchor="lb")
        if not any(_hit(lb, b2) for b2 in placed):
            placed.append(lb)
            d.text(at, text, font=label_font, fill=theme.c("ink-muted"), anchor="lb")
    # the arm: base block at the origin and a link pointing forward
    bu, bv = P(0.0, 0.0)
    r = 0.045 * s
    d.line([bu, bv, *P(0.20, 0.0)], fill=theme.c("steel"), width=max(4, int(0.03 * s)))
    d.rectangle([bu - r, bv - r, bu + r, bv + r], fill=theme.c("steel"), outline=theme.c("ink"))
    d.text((bu + r + 6, bv), "arm", font=label_font, fill=theme.c("ink-muted"), anchor="lm")
    return img


# --------------------------------------------------------------------------- card

def render_card(info: CardInfo, *, size: str = "wide", link: str, short: str, frame: FrameSource = None,
                frame_label: str = "", theme: CardTheme | None = None) -> bytes:
    theme = theme or load_theme()
    W, H = SIZES[size]
    img = Image.new("RGB", (W, H), theme.c("ground"))
    d = ImageDraw.Draw(img)
    M = 56 if size == "wide" else 64  # outer margin
    line = theme.c("line")

    # header: wordmark left, kind tag right
    head_base = M + 40
    _wordmark(d, theme, M, head_base, 46)
    tag_size = 22
    kind = info.kind if info.count <= 1 else f"{info.count} x {info.kind}"
    _tag(d, theme, W - M - _tag_width(d, theme, kind, tag_size), head_base - 32, kind, tag_size)
    d.line([M, head_base + 22, W - M, head_base + 22], fill=line, width=1)
    top = head_base + 46

    foot_mid = H - M + 8  # footer text centre line
    rule_y = foot_mid - 28
    bottom = rule_y - 22  # content ends here
    qr_px = 216 if size == "wide" else 240
    qr_side = qr_side_px(link, qr_px)

    if size == "wide":
        right_w = 440
        col_w = W - 2 * M - right_w - 40
        x = M
        y = top
        tfnt, tlines, ts = _fit(d, theme, "display", info.title, col_w, 2, (66, 60, 54, 48, 44))
        for ln in tlines:
            d.text((x, y), ln, font=tfnt, fill=theme.c("ink"))
            y += int(ts * 1.0)
        y += 14
        body = theme.font("sans", 25)
        for ln in wrap(d, info.instructions, body, col_w, 3 if len(tlines) == 1 else 2):
            d.text((x, y), ln, font=body, fill=theme.c("ink-muted"))
            y += 34
        # price block, bottom of the left column
        pf = theme.font("mono", 92)
        py = bottom - 170
        d.text((x, py), info.price, font=pf, fill=theme.c("ochre"))
        nf = theme.font("sans", 22)
        d.text((x, py + 108), info.price_note, font=nf, fill=theme.c("ink-muted"))
        ff = theme.font("sans-strong", 22)
        d.text((x, py + 140), " · ".join(info.facts), font=ff, fill=theme.c("ink"))
        # right column: frame well, then QR + short link
        fx, fy = W - M - right_w, top
        qy = bottom - qr_side
        _frame(img, d, theme, frame, frame_label, fx, fy, right_w, qy - 18 - fy)
        _qr_block(img, d, theme, link, short, fx, qy, right_w, qr_px, info)
    else:
        col_w = W - 2 * M
        x, y = M, top
        tfnt, tlines, ts = _fit(d, theme, "display", info.title, col_w, 2, (78, 70, 62, 56, 50))
        for ln in tlines:
            d.text((x, y), ln, font=tfnt, fill=theme.c("ink"))
            y += int(ts * 1.0)
        y += 12
        body = theme.font("sans", 28)
        for ln in wrap(d, info.instructions, body, col_w, 2):
            d.text((x, y), ln, font=body, fill=theme.c("ink-muted"))
            y += 38
        y += 14
        by = bottom - qr_side
        _frame(img, d, theme, frame, frame_label, x, y, col_w, by - 24 - y)
        pf = theme.font("mono", 100)
        d.text((x, by - 6), info.price, font=pf, fill=theme.c("ochre"))
        nf = theme.font("sans", 24)
        d.text((x, by + 112), info.price_note, font=nf, fill=theme.c("ink-muted"))
        ff = theme.font("sans-strong", 24)
        qw = qr_px + 160
        fy2 = by + 150
        for ln in wrap(d, " · ".join(info.facts), ff, W - M - qw - x - 24, 2):
            d.text((x, fy2), ln, font=ff, fill=theme.c("ink"))
            fy2 += 32
        _qr_block(img, d, theme, link, short, W - M - qw, by, qw, qr_px, info)

    # footer
    d.line([M, rule_y, W - M, rule_y], fill=line, width=1)
    sf = theme.font("display-bold", 20)
    left = "TEST MODE  PAYOUTS ARE STRIPE TEST MONEY" if info.test_mode else "PAID ON ACCEPTANCE"
    if info.sample:
        left = "SAMPLE POST  NO TASK BEHIND IT" + ("  TEST MODE" if info.test_mode else "")
    _tracked(d, theme, M, foot_mid, left, sf, "steel" if not info.test_mode else "ochre")
    right = f"TASK #{info.task_id}" if info.task_id else ""
    if right:
        idf = theme.font("mono", 22)
        d.text((W - M, foot_mid), right, font=idf, fill=theme.c("ink"), anchor="rm")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _tracked(d: ImageDraw.ImageDraw, theme: CardTheme, x: int, y: int, text: str, f, color: str) -> None:
    cx = float(x)
    for ch in text:
        d.text((cx, y), ch, font=f, fill=theme.c(color), anchor="lm")
        cx += _w(d, ch, f) + 20 * 0.12


def _frame(img: Image.Image, d: ImageDraw.ImageDraw, theme: CardTheme, frame: FrameSource, label: str,
           x: int, y: int, w: int, h: int) -> None:
    h = max(60, h)
    d.rounded_rectangle([x, y, x + w, y + h], radius=4, fill=theme.c("surface-sunken"), outline=theme.c("line"))
    if callable(frame):  # drawn for this exact well (the scene sketch), so nothing is cropped
        frame = frame(w - 2, h - 2)
    if frame is not None:
        _rounded_paste(img, _cover(frame, w - 2, h - 2), (x + 1, y + 1), 4)
    if label:
        f = theme.font("mono", 17)
        tw = int(_w(d, label, f)) + 16
        d.rectangle([x + 10, y + 10, x + 10 + tw, y + 38], fill=theme.c("ground"))
        d.text((x + 18, y + 24), label, font=f, fill=theme.c("ink"), anchor="lm")


def _qr_block(img: Image.Image, d: ImageDraw.ImageDraw, theme: CardTheme, link: str, short: str, x: int, y: int,
              w: int, qr_px: int, info: CardInfo) -> None:
    q = qr_image(link, qr_px, theme.rgb("ground"), theme.rgb("ink"))
    img.paste(q, (x, y))
    tx = x + q.width + 22
    lab = theme.font("display-bold", 26)
    d.text((tx, y + 4), "SCAN TO CLAIM", font=lab, fill=theme.c("ink"))
    sf = theme.font("mono-regular", 19)
    yy = y + 48
    for ln in _split_link(short, d, sf, w - q.width - 22):
        d.text((tx, yy), ln, font=sf, fill=theme.c("arc"))
        yy += 26
    if info.access_code:
        cf = theme.font("mono", 22)
        d.text((tx, yy + 14), f"Code {info.access_code}", font=cf, fill=theme.c("ink"))


def _split_link(s: str, d: ImageDraw.ImageDraw, f, width: float) -> list[str]:
    out, cur = [], ""
    for ch in s:
        if _w(d, cur + ch, f) > width and cur:
            out.append(cur)
            cur = ""
        cur += ch
    if cur:
        out.append(cur)
    return out[:4]
