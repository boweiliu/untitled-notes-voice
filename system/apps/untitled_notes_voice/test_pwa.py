"""The installed-app icon: manifest, icon files, page tags, and what a real browser makes of them."""
import json
import re
import struct
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

from prompt_notes import runner

FORTRESS = "/opt/fortress/tilion-fortress/tilion"
REPO = Path(__file__).parent


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def manifest(client) -> dict:
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json"
    return response.get_json(force=True)


def test_manifest_names_the_app_and_every_icon_a_platform_needs() -> None:
    data = manifest(runner.app.test_client())
    assert data["name"] == "Untitled Notes Voice"
    assert data["id"] == "/" and data["start_url"] == "/" and data["scope"] == "/"
    assert data["display"] == "standalone"
    # Desktop browsers show the rounded tile ("any"); Android crops the full-bleed one ("maskable").
    assert {(icon["purpose"], icon["sizes"]) for icon in data["icons"]} == {
        ("any", "192x192"),
        ("any", "512x512"),
        ("maskable", "192x192"),
        ("maskable", "512x512"),
    }


def test_every_icon_in_the_manifest_downloads_at_the_size_it_claims() -> None:
    client = runner.app.test_client()
    for icon in manifest(client)["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200 and response.mimetype == "image/png", icon["src"]
        width, height = (int(side) for side in icon["sizes"].split("x"))
        assert png_size(response.data) == (width, height), icon["src"]
    apple = client.get(runner.pwa_icon_url("apple-touch-icon.png"))
    assert apple.status_code == 200 and png_size(apple.data) == (180, 180)


def test_changing_an_icon_changes_its_address_so_installed_copies_fetch_it_again(tmp_path, monkeypatch) -> None:
    before = {icon["src"] for icon in manifest(runner.app.test_client())["icons"]}
    for name in runner.PWA_ICON_NAMES:
        (tmp_path / name).write_bytes((runner.PWA_DIR / name).read_bytes())
    (tmp_path / "icon-512.png").write_bytes((runner.PWA_DIR / "icon-512.png").read_bytes() + b"redrawn")
    monkeypatch.setattr(runner, "PWA_DIR", tmp_path)
    after = {icon["src"] for icon in manifest(runner.app.test_client())["icons"]}
    assert len(before - after) == 1 and "icon-512.png" in next(iter(before - after))
    assert len(after - before) == 1 and "icon-512.png" in next(iter(after - before))


def test_only_the_listed_icon_files_are_served() -> None:
    client = runner.app.test_client()
    for name in ("icon.svg", "../runner.py", "..%2Frunner.py", "missing.png"):
        assert client.get(f"/pwa-icons/{name}").status_code == 404, name


def test_page_links_the_manifest_with_credentials_and_carries_its_own_tab_icon() -> None:
    html = runner.app.test_client().get("/").get_data(as_text=True)
    assert '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">' in html
    assert re.search(r'<link rel="icon" type="image/svg\+xml" href="data:image/svg\+xml;base64,[A-Za-z0-9+/=]+">', html)
    assert f'<link rel="apple-touch-icon" href="{runner.pwa_icon_url("apple-touch-icon.png")}">' in html
    assert '<meta name="theme-color" content="#faf9f5">' in html
    assert "<!-- pwa head -->" not in html


def test_the_icon_copy_the_server_inlines_is_the_workspace_icon() -> None:
    assert (runner.PWA_DIR / "icon.svg").read_text() == (REPO / "icon.svg").read_text()


@pytest.fixture
def behind_sign_in(tmp_path, monkeypatch):
    """The app behind a front door like the share link's: no cookie, no answer."""
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    app_server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=app_server.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{app_server.server_port}"

    @Request.application
    def front_door(request: Request) -> Response:
        if request.cookies.get("signed_in") != "yes":
            return Response("sign in first", status=401, mimetype="text/plain")
        try:
            with urllib.request.urlopen(upstream + request.full_path.rstrip("?")) as reply:
                return Response(reply.read(), status=reply.status, content_type=reply.headers["Content-Type"])
        except urllib.error.HTTPError as error:
            return Response(error.read(), status=error.code)

    front_server = make_server("127.0.0.1", 0, front_door, threaded=True)
    threading.Thread(target=front_server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{front_server.server_port}"
    finally:
        front_server.shutdown()
        app_server.shutdown()


@pytest.mark.browser
def test_browser_reads_the_manifest_through_the_sign_in_and_finds_the_app_installable(behind_sign_in) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=FORTRESS, args=["--no-sandbox"])
        context = browser.new_context()
        context.add_cookies([{"name": "signed_in", "value": "yes", "url": behind_sign_in}])
        page = context.new_page()
        page.goto(behind_sign_in + "/")
        cdp = context.new_cdp_session(page)
        cdp.send("Page.enable")

        reply = cdp.send("Page.getAppManifest")
        assert reply["errors"] == [], reply["errors"]
        assert reply["url"] == behind_sign_in + "/manifest.webmanifest"
        data = json.loads(reply["data"])
        assert data["name"] == "Untitled Notes Voice"
        assert {icon["purpose"] for icon in data["icons"]} == {"any", "maskable"}
        assert cdp.send("Page.getAppId")["appId"] == behind_sign_in + "/"
        # A private test window can never install anything; that is the only objection allowed.
        problems = cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
        assert [problem["errorId"] for problem in problems] == ["in-incognito"]

        # What the icons actually look like: the tile has see-through corners, the maskable
        # art runs colour to every edge, and the mark is drawn in the middle of both.
        pixels = page.evaluate(
            """async (icons) => {
                const out = {};
                for (const icon of icons) {
                    const bitmap = await createImageBitmap(await (await fetch(icon.src)).blob());
                    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
                    const context = canvas.getContext('2d');
                    context.drawImage(bitmap, 0, 0);
                    const at = (x, y) => Array.from(context.getImageData(x, y, 1, 1).data);
                    out[icon.purpose + icon.sizes] = {
                        corner: at(0, 0), edge: at(bitmap.width - 1, bitmap.height >> 1),
                        centre: at(bitmap.width >> 1, Math.round(bitmap.height * 0.3)),
                    };
                }
                return out;
            }""",
            data["icons"],
        )
        browser.close()

    for key, seen in pixels.items():
        assert seen["centre"][3] == 255 and seen["centre"][0] > 180, (key, seen)  # the pink mark
        if key.startswith("any"):
            assert seen["corner"][3] == 0, (key, seen)
        else:
            for spot in ("corner", "edge"):  # #492222, give or take the browser's colour rounding
                assert all(abs(got - want) <= 3 for got, want in zip(seen[spot], [73, 34, 34, 255])), (key, seen)
