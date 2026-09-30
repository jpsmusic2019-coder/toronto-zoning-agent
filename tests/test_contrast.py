# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""Every text/background pair in the zoning report reaches 4.5:1 (WCAG AA), in the dark
default, the light theme and print. Reads the colour tokens from the template itself."""
import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "toronto_zoning_agent" / "templates" / "zoning_report.html"
CSS = TEMPLATE.read_text(encoding="utf-8")

TEXT = ["head", "ink", "ink-2", "muted", "accent"]
# (background token, what it sits on): translucent panels are composited over both ends
# of the page gradient; table headers and row highlights sit on a panel.
SURFACES = [
    ("page", None), ("page-2", None), ("panel", "page"), ("panel", "page-2"),
    ("panel-solid", None), ("toc-bg", "page"), ("toc-bg", "page-2"),
    ("panel-2", ("panel", "page-2")), ("accent-wash", ("panel", "page-2")),
    ("sheet", "page"), ("sheet", "page-2"), ("panel", ("sheet", "page-2")),
    ("panel-2", ("panel", ("sheet", "page-2"))), ("nav-bg", "page"), ("nav-bg", "page-2"),
]


def _block(selector: str) -> dict:
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\n\}", CSS, re.S)
    assert m, selector
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", m.group(1)))


def _print_block() -> dict:
    m = re.search(r"@media print \{\s*:root, :root\[data-theme\] \{(.*?)\}", CSS, re.S)
    assert m
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", m.group(1)))


DARK = _block(":root")
LIGHT = {**DARK, **_block(':root[data-theme="light"]')}
PRINT = {**LIGHT, **_print_block()}


def _rgba(v: str):
    v = v.strip()
    if v.startswith("#"):
        h = v[1:]
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,)
    m = re.match(r"rgba\(([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)", v)
    assert m, v
    return tuple(float(x) for x in m.groups()[:3]) + (float(m.group(4)),)


def _over(fg, bg):
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def _solid(tokens: dict, name, under=None):
    c = _rgba(tokens[name])
    if under is None:
        assert c[3] == 1.0, f"{name} is translucent; give it a backdrop"
        return c
    base = _solid(tokens, under[0], under[1]) if isinstance(under, tuple) else _solid(tokens, under)
    return _over(c, base)


def _lum(c):
    def ch(x):
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _pairs(tokens: dict):
    out = []
    for bg, under in SURFACES:
        b = _solid(tokens, bg, under)
        for t in TEXT:
            out.append((t, f"{bg}/{under}", ratio(_solid(tokens, t), b)))
    for page in ("page", "page-2"):
        base = _solid(tokens, page)
        out.append(("warn-ink", f"warn-wash/{page}", ratio(_solid(tokens, "warn-ink"), _over(_rgba(tokens["warn-wash"]), base))))
        for sig in ("strong", "mixed", "weak", "sparse"):
            wash = _over(_rgba(tokens[f"sig-{sig}-wash"]), base)
            for t in (f"sig-{sig}-ink", "head", "ink-2"):
                out.append((t, f"sig-{sig}-wash/{page}", ratio(_solid(tokens, t), wash)))
    out.append(("zone-ink", "zone", ratio(_solid(tokens, "zone-ink"), _solid(tokens, "zone"))))
    out.append(("page", "head (pressed chip)", ratio(_solid(tokens, "page"), _solid(tokens, "head"))))
    return out


@pytest.mark.unit
@pytest.mark.parametrize("name,tokens", [("dark", DARK), ("light", LIGHT), ("print", PRINT)])
def test_text_contrast_is_at_least_4_5(name, tokens):
    low = [(t, bg, round(r, 2)) for t, bg, r in _pairs(tokens) if r < 4.5]
    assert low == [], f"{name} theme pairs under 4.5:1: {low}"
