"""The native package keeps draft edits across reloads and switches."""
import threading
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from prompt_notes import runner

import pytest


@pytest.mark.browser
@pytest.mark.timeout(60)
def test_native_draft_reload_and_model_choice(tmp_path, monkeypatch):
    root=tmp_path
    monkeypatch.setattr(runner, "DATA_DIR", root/"drafts")
    monkeypatch.setattr(runner, "studio_workspaces", lambda: None)
    calls=[]
    def fake_chat(machine, method, path, payload=None, timeout=45):
        calls.append((machine,method,path))
        if method != "GET": raise AssertionError("No outbound sends allowed")
        if path=="/api/harnesses": return 200, {}
        if path=="/api/accounts": return 200, {"accounts":[]}
        if path=="/api/chats": return 200, {"chats":[]}
        return 404, {}
    monkeypatch.setattr(runner, "chat_request", fake_chat)
    modules = tmp_path / "shell-modules"
    modules.mkdir()
    (modules / "app_contract.js").write_text("export const connectToShell = () => ({viewportRectForElement: () => null});")
    (modules / "context_menu.js").write_text("export const installElementContextMenu = () => {};")
    monkeypatch.setattr(runner, "SHELL_STATIC_MODULES_DIR", modules)
    server=make_server("127.0.0.1",0,runner.app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
     with sync_playwright() as pw:
      browser=pw.chromium.launch(executable_path="/opt/fortress/tilion-fortress/tilion",args=["--no-sandbox"])
      page=browser.new_page(viewport={"width":390,"height":760},is_mobile=True,has_touch=True)
      errors=[]; page.on("pageerror",lambda e:errors.append(str(e)))
      page.route("**/api/fire",lambda r:r.abort())
      page.goto(f"http://127.0.0.1:{server.server_port}/")
      for name in runner.SHELL_STATIC_MODULE_NAMES:
       assert page.request.get(f"http://127.0.0.1:{server.server_port}/_static/{name}").status==200
      page.get_by_label("Draft title",exact=True).fill("Launch outline")
      page.get_by_label("Draft text",exact=True).fill("Make a short introduction.")
      expect(page.locator("#save-indicator")).to_have_attribute("aria-label","Saved")
      page.get_by_label("Draft text",exact=True).fill("Make a short introduction. Include a next action.")
      expect(page.locator("#save-indicator")).to_have_attribute("aria-label","Saved")
      page.reload()
      expect(page.get_by_label("Draft title",exact=True)).to_have_value("Launch outline")
      expect(page.get_by_label("Draft text",exact=True)).to_have_value("Make a short introduction. Include a next action.")
      page.locator("#voice-button").click(button="right")
      expect(page.locator("#voice-menu [role=menuitemradio]")).to_have_count(4)
      page.locator("#voice-menu [data-engine=v2-tiny]").click()
      page.reload()
      page.locator("#voice-button").click(button="right")
      assert "Tiny" in page.locator('#voice-menu [aria-checked=true]').inner_text()
      page.keyboard.press("Escape")
      page.get_by_role("button",name="New draft",exact=True).filter(visible=True).click()
      page.get_by_label("Draft title",exact=True).fill("Second outline")
      page.get_by_label("Draft text",exact=True).fill("Another sample thought.")
      expect(page.locator("#save-indicator")).to_have_attribute("aria-label","Saved")
      page.locator("#drafts-button").click()
      page.get_by_role("button",name="Launch outline",exact=True).click()
      expect(page.get_by_label("Draft text",exact=True)).to_have_value("Make a short introduction. Include a next action.")
      for sel in ("#voice-button","#continue-button"):
       b=page.locator(sel).bounding_box(); assert b and b["y"]>=0 and b["y"]+b["height"]<=760
      page.screenshot(path=str(root/"draft-smoke.png"))
      assert len(page.request.get(f"http://127.0.0.1:{server.server_port}/api/drafts").json()["drafts"])==2
      assert not errors, errors
      assert all(method=="GET" for _,method,_ in calls)
      browser.close()
     print("PASS: native shell modules, create/edit/reload/switch two drafts, four model choices, persisted Tiny choice, mobile footer, no browser errors or real sends")
    finally:
     server.shutdown(); thread.join(timeout=5)
