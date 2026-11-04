"""Redraw the installed-app icons from the root icon.svg.

Run after changing icon.svg:

    uv run --extra test python tools/build_pwa_icons.py [--browser /path/to/chromium]

It rewrites src/prompt_notes/assets/pwa/. The server hashes those files into the icon
addresses in the web app manifest, so a redraw makes installed copies fetch the new art.

Three shapes, because each platform treats an icon differently:

- ``icon-<size>.png`` keeps the rounded tile with transparent corners. Desktop browsers
  and the Dock show it as drawn.
- ``icon-maskable-<size>.png`` and ``apple-touch-icon.png`` are the same art with square
  corners and the colour running to every edge. Android (including Firefox's home screen
  shortcut) crops them to a circle or squircle, and iOS rounds them itself. The mark sits
  inside the central 80% circle that cropping always keeps (checked below).
"""
import argparse
import math
import re
import shutil
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "icon.svg"
OUT = ROOT / "src" / "prompt_notes" / "assets" / "pwa"
DEFAULT_BROWSER = "/opt/fortress/tilion-fortress/tilion"
TILE = 216  # the icon.svg viewBox
SAFE_RADIUS = 0.4 * TILE  # maskable safe zone: circle of 80% of the width, centred

# (file name, pixel size, square corners)
OUTPUTS = [
    ("icon-192.png", 192, False),
    ("icon-512.png", 512, False),
    ("icon-maskable-192.png", 192, True),
    ("icon-maskable-512.png", 512, True),
    ("apple-touch-icon.png", 180, True),
]


def square_corners(svg: str) -> str:
    squared, count = re.subn(r'(<rect [^>]*?)\brx="[^"]*"', r'\1rx="0"', svg, count=1)
    if count != 1:
        raise SystemExit("icon.svg has no <rect rx=...> tile to square off")
    return squared


def farthest_mark_distance(page: Page, svg: str) -> float:
    """Largest distance from the tile centre to any point of the mark (the non-tile paths)."""
    page.set_content(f'<body style="margin:0">{svg}</body>')
    return page.evaluate(
        """(tile) => {
            let farthest = 0;
            for (const path of document.querySelectorAll('svg path')) {
                const length = path.getTotalLength();
                for (let i = 0; i <= 600; i++) {
                    const point = path.getPointAtLength(length * i / 600);
                    farthest = Math.max(farthest, Math.hypot(point.x - tile / 2, point.y - tile / 2));
                }
            }
            return farthest;
        }""",
        TILE,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default=DEFAULT_BROWSER)
    args = parser.parse_args()

    svg = SOURCE.read_text()
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, OUT / "icon.svg")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser, args=["--no-sandbox"])
        page = browser.new_page()
        distance = farthest_mark_distance(page, svg)
        if distance > SAFE_RADIUS:
            raise SystemExit(
                f"the mark reaches {distance:.1f} from the centre; maskable icons keep only {SAFE_RADIUS:.1f}"
            )
        print(f"mark reaches {distance:.1f} of {SAFE_RADIUS:.1f} allowed from the centre")
        for name, size, square in OUTPUTS:
            art = square_corners(svg) if square else svg
            sized = re.sub(r'width="\d+" height="\d+"', f'width="{size}" height="{size}"', art, count=1)
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(f'<body style="margin:0;background:transparent">{sized}</body>')
            page.screenshot(path=str(OUT / name), omit_background=True)
            print("wrote", OUT / name)
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
