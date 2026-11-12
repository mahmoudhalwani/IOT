"""Builds the static demo of the resident website (for GitHub Pages).

It copies the real pages from doorbell/web/static and swaps the door unit for
demo/demo-api.js, a pretend door that runs inside the browser.

Usage: python tools/build_demo.py <output dir>
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "doorbell" / "web" / "static"
BANNER_CSS = """
.demo-banner { margin: 0; padding: 10px 16px; text-align: center; font-size: .85rem;
               background: var(--warn); color: #fff; }
"""


def build(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    html = (STATIC / "index.html").read_text()
    html = html.replace('href="/static/app.css"', 'href="app.css"')
    html = html.replace('<script src="/static/app.js" defer></script>',
                        '<script src="demo-api.js"></script>\n<script src="app.js" defer></script>')
    html = html.replace("<title>Front Door</title>", "<title>Front Door (demo)</title>")
    if "/static/" in html:
        raise SystemExit("index.html still has a /static/ path the demo cannot serve")
    (out / "index.html").write_text(html)
    (out / "app.css").write_text((STATIC / "app.css").read_text() + BANNER_CSS)
    shutil.copy(STATIC / "app.js", out / "app.js")
    shutil.copy(ROOT / "demo" / "demo-api.js", out / "demo-api.js")
    (out / ".nojekyll").write_text("")
    print(f"demo written to {out}")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
