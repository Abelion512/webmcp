"""Responsive contract for the injected overlay.

Every fixed surface anchored to a viewport edge must be capped by the viewport
width. A bare pixel width hangs off the opposite edge on a narrow window —
measured live, the setup card (``width: 288px`` with ``right: 18px``) reached
x = -26 at vw = 280.

This guard fails if someone reintroduces an uncapped fixed width on an
edge-anchored surface, or drops the box-sizing that keeps padding and border
inside the cap.

Both the browser extension and the Snapgent App bundle carry an identical
overlay.css, so both are checked.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OVERLAYS = [
    REPO / "snapgent-extension" / "overlay.css",
    REPO / "snapgent-app" / "extension" / "overlay.css",
]


def _rules(css):
    """Yield (selector, body) for plain rules, skipping @media preludes."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = match.group(1).strip()
        if selector.startswith("@"):
            continue
        yield selector, match.group(2)


def test_overlay_copies_are_identical():
    bodies = [path.read_text(encoding="utf-8") for path in OVERLAYS]
    assert bodies[0] == bodies[1], "the app copy of overlay.css has diverged"


def test_edge_anchored_fixed_surfaces_are_viewport_capped():
    for path in OVERLAYS:
        css = path.read_text(encoding="utf-8")
        offenders = []
        for selector, body in _rules(css):
            if "position: fixed" not in body:
                continue
            if "right:" not in body and "left:" not in body:
                continue
            widths = re.findall(r"(?:max-)?width:\s*([^;]+);", body)
            if not widths:
                continue
            if not any("vw" in width or "min(" in width for width in widths):
                offenders.append((selector, widths))
        assert not offenders, f"{path.name}: uncapped edge-anchored fixed widths: {offenders}"


def test_setup_card_and_toast_use_viewport_caps():
    """The two surfaces that actually overflowed, named explicitly."""
    for path in OVERLAYS:
        css = path.read_text(encoding="utf-8")
        setup = " ".join(body for selector, body in _rules(css) if selector.endswith("#zs-setup"))
        toast = " ".join(body for selector, body in _rules(css) if selector.endswith(".zs-toast"))
        assert "min(288px, calc(100vw - 36px))" in setup, f"{path.name}: {setup}"
        assert "min(300px, calc(100vw - 36px))" in toast, f"{path.name}: {toast}"
        # The cap must include padding and border, or the box is wider than it.
        assert "box-sizing: border-box" in setup, f"{path.name}: setup card needs border-box"
        assert "box-sizing: border-box" in toast, f"{path.name}: toast needs border-box"
