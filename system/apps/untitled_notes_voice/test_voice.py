"""Dictation: the served voice modules, the streaming model's invariants, and the record button in a browser."""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from prompt_notes import runner

ASSETS = Path(runner.__file__).parent / "assets"
FORTRESS = "/opt/fortress/tilion-fortress/tilion"
# The workspace shell's app-window sandbox, with and without the microphone delegated.
SANDBOX = "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals"
SPEECH_SAMPLE = "https://huggingface.co/datasets/Xenova/transformers.js-docs/resolve/main/jfk.wav"


def test_page_is_cross_origin_isolated_for_the_v2_models(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    response = runner.app.test_client().get("/")
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
    streaming = runner.app.test_client().get("/voice/voice_streaming.js")
    assert streaming.status_code == 200 and b"moonshine-wasm@0.1.5" in streaming.data


def test_voice_modules_are_served_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    client = runner.app.test_client()
    capture = client.get("/voice/voice_capture.js")
    assert capture.status_code == 200 and capture.mimetype == "text/javascript"
    assert b"LocalVocal" in capture.data and b"prewarmMic" in capture.data
    assert b"VoiceSession" in client.get("/voice/voice_session.js").data
    assert client.get("/voice/runner.py").status_code == 404
    assert client.get("/voice/..%2Frunner.py").status_code == 404


def test_page_loads_voice_modules_before_the_editor_script(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    page = runner.app.test_client().get("/").get_data(as_text=True)
    session = page.index('<script src="/voice/voice_session.js">')
    capture = page.index('<script src="/voice/voice_capture.js">')
    editor = page.index("const voiceButton")
    assert session < capture < editor
    assert 'id="voice-button"' in page


def test_streaming_model_invariants():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run([node, "--test", str(ASSETS / "voice_session.test.mjs")], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout


def serve(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def open_framed(browser, port, allow):
    page = browser.new_page()
    # Host the frame on another origin, as the shell does.
    page.goto(f"http://localhost:{port}/health")
    page.set_content(
        f'<iframe id="app" src="http://127.0.0.1:{port}/" sandbox="{SANDBOX}" allow="{allow}" '
        'style="width:800px;height:700px"></iframe>'
    )
    return page, page.frame_locator("#app")


@pytest.mark.browser
def test_blocked_window_offers_its_own_tab(tmp_path, monkeypatch):
    server = serve(tmp_path, monkeypatch)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=FORTRESS, args=["--no-sandbox"])
            page, frame = open_framed(browser, server.port, "clipboard-read; clipboard-write")
            frame.locator("#voice-button").click()
            frame.locator("#voice-notice").wait_for(state="visible")
            assert frame.locator("#voice-notice-text").inner_text() == "The microphone is blocked inside this window."
            link = frame.locator("#voice-open-tab")
            assert link.is_visible()
            assert link.get_attribute("href") == f"http://127.0.0.1:{server.port}/"
            assert link.get_attribute("target") == "_blank"
            assert frame.locator("#voice-button").get_attribute("aria-pressed") == "false"
            with page.expect_popup() as popup:
                link.click()
            assert popup.value.url == f"http://127.0.0.1:{server.port}/"
            frame.locator("#voice-notice-close").click()
            assert frame.locator("#voice-notice").is_hidden()
            browser.close()
    finally:
        server.shutdown()


@pytest.mark.browser
def test_model_menu_opens_on_right_click_and_hold_and_remembers_the_choice(tmp_path, monkeypatch):
    server = serve(tmp_path, monkeypatch)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=FORTRESS, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 390, "height": 760}, has_touch=True)
            page.goto(f"http://127.0.0.1:{server.port}/")
            assert page.evaluate("crossOriginIsolated")
            page.locator("#voice-button").click(button="right")
            menu = page.locator("#voice-menu")
            assert menu.is_visible()
            options = menu.locator("[role=menuitemradio]")
            assert options.count() == 4
            assert menu.locator('[aria-checked="true"]').inner_text().startswith("Moonshine Base")
            assert all(not options.nth(index).is_disabled() for index in range(4))
            menu.locator('[data-engine="v2-small"]').click()
            assert menu.is_hidden()
            assert page.evaluate("localStorage.getItem('untitled-notes-voice.engine.v1')") == "v2-small"
            assert page.locator("#voice-button").get_attribute("aria-pressed") == "false"
            # Press and hold on a touch screen opens the menu, and releasing does not start dictation.
            page.reload()
            page.locator("#voice-button").dispatch_event("pointerdown", {"pointerType": "touch", "isPrimary": True})
            page.wait_for_timeout(700)
            page.locator("#voice-button").dispatch_event("pointerup", {"pointerType": "touch", "isPrimary": True})
            page.locator("#voice-button").dispatch_event("click")
            assert menu.is_visible()
            assert menu.locator('[aria-checked="true"]').inner_text().startswith("Moonshine v2 Small")
            assert page.locator("#voice-button").get_attribute("aria-pressed") == "false"
            page.keyboard.press("Escape")
            assert menu.is_hidden()
            browser.close()
    finally:
        server.shutdown()


QUIET_PHONE_MIC = """() => {
  // A stand-in microphone engine: speech at RMS 0.02, typical of a phone with noise suppression.
  window.LocalVocal = {
    status: 'ready', statusDetail: '', lastError: null,
    init() {}, releaseMic() {}, prewarmMic: async () => ({}),
    listen(callbacks) {
      setTimeout(() => callbacks.onListening(), 50);
      const timer = setInterval(() => callbacks.onLevel(Math.min(1, 0.02 * 4)), 30);
      return { stop: () => clearInterval(timer) };
    },
  };
}"""


@pytest.mark.browser
def test_recording_indicator_is_visible_on_a_phone_mic(tmp_path, monkeypatch):
    server = serve(tmp_path, monkeypatch)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=FORTRESS, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 390, "height": 760}, is_mobile=True, has_touch=True)
            page.goto(f"http://127.0.0.1:{server.port}/")
            page.evaluate(QUIET_PHONE_MIC)
            button = page.locator("#voice-button")
            button.tap()
            page.wait_for_function("document.getElementById('voice-button').classList.contains('listening')")
            page.wait_for_timeout(400)
            level = float(button.evaluate("el => el.style.getPropertyValue('--level')"))
            assert level >= 0.4, level   # a clearly visible swell, not the old 0.08
            pulse = button.evaluate("el => getComputedStyle(el, '::after').animationName")
            assert pulse == "voice-live"
            button.tap()
            assert button.evaluate("el => getComputedStyle(el, '::after').animationName") == "none"
            assert button.evaluate("el => el.style.getPropertyValue('--level')") == "0"
            browser.close()
    finally:
        server.shutdown()


@pytest.mark.browser
def test_v2_models_are_unavailable_inside_a_window(tmp_path, monkeypatch):
    server = serve(tmp_path, monkeypatch)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=FORTRESS, args=["--no-sandbox"])
            page, frame = open_framed(browser, server.port, "clipboard-read; clipboard-write; microphone")
            frame.locator("#voice-button").click(button="right")
            menu = frame.locator("#voice-menu")
            assert not menu.locator('[data-engine="base"]').is_disabled()
            assert menu.locator('[data-engine="v2-small"]').is_disabled()
            assert "own tab" in menu.inner_text()
            browser.close()
    finally:
        server.shutdown()


@pytest.mark.browser
def test_denied_microphone_explains_without_a_tab_link(tmp_path, monkeypatch):
    server = serve(tmp_path, monkeypatch)
    try:
        with sync_playwright() as playwright:
            # No fake-UI flag: headless Chromium refuses the permission request.
            browser = playwright.chromium.launch(
                executable_path=FORTRESS, args=["--no-sandbox", "--use-fake-device-for-media-stream"]
            )
            page = browser.new_page()
            models = []
            page.route("https://huggingface.co/**", lambda route: (models.append(route.request.url), route.abort()))
            page.goto(f"http://127.0.0.1:{server.port}/")
            page.evaluate("() => { navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('denied', 'NotAllowedError')); }")
            page.locator("#voice-button").click()
            page.locator("#voice-notice").wait_for(state="visible", timeout=20000)
            assert "site settings" in page.locator("#voice-notice-text").inner_text()
            assert page.locator("#voice-open-tab").is_hidden()
            assert page.locator("#voice-button").get_attribute("aria-pressed") == "false"
            assert models == []  # a refused microphone never starts the speech model download
            browser.close()
    finally:
        server.shutdown()


def speech_wav(directory: Path) -> Path:
    """The JFK sample, mono, padded with silence so speech detection ends the utterance."""
    raw = directory / "jfk.wav"
    with urllib.request.urlopen(SPEECH_SAMPLE, timeout=60) as response:
        raw.write_bytes(response.read())
    with wave.open(str(raw)) as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        frames = source.readframes(source.getnframes())
    assert width == 2
    samples = memoryview(frames).cast("h")
    mono = bytearray()
    for index in range(0, len(samples), channels):
        mono += int(sum(samples[index:index + channels]) / channels).to_bytes(2, "little", signed=True)
    padded = directory / "speech.wav"
    with wave.open(str(padded), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(int(rate * 1.5) * 2) + bytes(mono) + bytes(int(rate * 6) * 2))
    return padded


@pytest.mark.browser
@pytest.mark.voice_model
# The v2 run uses Tiny: on a test machine's CPU, Small transcribes no faster than real
# time, so a live session falls behind (fed whole, Small reads this sample best of all).
@pytest.mark.parametrize("framing, engine", [("top", "base"), ("framed-with-microphone", "base"), ("top", "v2-tiny")])
def test_dictation_transcribes_speech_into_the_saved_draft(tmp_path, monkeypatch, framing, engine):
    """Downloads and runs the real speech model (about 2 GB of browser memory) on a recorded sample."""
    sample = speech_wav(tmp_path)
    data = tmp_path / "data"
    server = serve(data, monkeypatch)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=FORTRESS, args=[
                "--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={sample}",
            ])
            context = browser.new_context()
            # This browser reports a graphics card the machine does not have; hide WebGPU so
            # the check runs the CPU model a GPU-less device would use, not software rendering.
            context.add_init_script("Object.defineProperty(Navigator.prototype, 'gpu', { get: () => undefined })")
            # It also reports 16 processor cores; the speech runtimes size their thread pools
            # from that, and on a 4-core test machine live audio then falls behind.
            cores = os.cpu_count() or 4
            context.add_init_script(f"Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {{ get: () => {cores} }})")
            context.add_init_script(f"localStorage.setItem('untitled-notes-voice.engine.v1', '{engine}')")
            voice_log = []
            context.on("console", lambda message: voice_log.append(message.text) if "[notes voice]" in message.text else None)
            if framing == "top":
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{server.port}/")
                view = page
            else:
                page, view = open_framed(context, server.port, "clipboard-read; clipboard-write; microphone")
            view.locator("#draft-title").fill("Dictated")
            view.locator("#draft-text").fill("Start.")
            # The fake microphone plays the sample from the moment it opens, so load the
            # model first; otherwise the opening words pass while the model is still loading.
            if engine == "base":
                view.locator("body").evaluate("() => LocalVocal.whenReady()")
            else:
                view.locator("body").evaluate("() => MoonshineStreaming.preload('TinyStreaming')")
            view.locator("#voice-button").click()
            deadline = time.time() + 120
            while time.time() < deadline and "for your country" not in view.locator("#draft-text").input_value().lower():
                assert view.locator("#voice-notice").is_hidden(), view.locator("#voice-notice-text").inner_text()
                time.sleep(0.5)
            text = view.locator("#draft-text").input_value()
            assert text.startswith("Start. ")
            # Every pause-separated phrase lands, in order. The model's reading of this old
            # recording varies by a word ("fellow-american", "as what"), so match the stable parts.
            trace = "\n".join(line for line in voice_log if "segment" in line or "misfire" in line)
            position = 0
            for phrase in ["fellow", "country can do for you", "you can do for your country"]:
                found = text.lower().find(phrase, position)
                assert found >= 0, f"{phrase!r} missing after position {position}: {text}\n{trace}"
                position = found + len(phrase)
            if engine == "base":
                # The short "ask not" between the long pauses is kept as speech, never discarded.
                assert "misfire" not in trace, trace
                assert trace.count("speech detected") >= 3, trace
            assert view.locator("#voice-button").get_attribute("aria-pressed") == "true"
            view.locator("#voice-button").click()
            assert view.locator("#voice-button").get_attribute("aria-pressed") == "false"
            # A phrase finishing after the stop still lands; wait for the text to settle and save.
            deadline = time.time() + 20
            saved, shown = [], None
            while time.time() < deadline:
                time.sleep(1)
                current = view.locator("#draft-text").input_value()
                saved = json.load(urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/drafts"))["drafts"]
                if current == shown and saved and saved[0]["body"] == current:
                    break
                shown = current
            assert saved[0]["title"] == "Dictated" and saved[0]["body"] == view.locator("#draft-text").input_value()
            browser.close()
    finally:
        server.shutdown()
