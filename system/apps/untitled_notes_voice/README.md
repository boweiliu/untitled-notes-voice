# Untitled Notes Voice

Draft messages with browser speech recognition and send them to new or existing
Studio chats, on this machine or a linked machine. Titles, multiple drafts,
destination choices, provider catalogs and delivery history persist across reloads.

## Install in an Imbue Studio workspace

This package is a native member of the full template's uv workspace. From the
workspace root, run `uv sync --all-packages`. The included supervisor program
registers `app.toml` on port 8096 and runs
`uv run --package untitled-notes-voice untitled-notes-voice` with the workspace
root as its working directory. Bootstrap builds the shell browser modules served
from `system/apps/system_interface/imbue/system_interface/static/_static`.
No private source checkout or scheduled updater is required.

## Dictation

Open the app in a standalone HTTPS browser tab and allow the microphone. Current
Studio frames block microphone access; a browser tab is required. Click the mic to
begin or stop. Right-click or long-press it to choose Moonshine Base (default),
v2 Tiny, v2 Small, or v2 Medium. The v2 models require cross-origin isolation,
provided by the app's response headers when visited as a top-level page. v2 Small
may not keep up on modest CPUs. First use downloads runtime/model files from the
internet; later use benefits from browser caching. No transcription API key is
needed. Audio stays in the browser; recognized text and typed drafts are autosaved
on this workspace's server. Sending delivers text to the selected Studio chat.

Recognition builds on [Local Vocal](https://github.com/DanverImbue/local-vocal),
Moonshine, Silero VAD, ONNX Runtime Web and Transformers.js. Original attribution
comments are retained in the voice modules. Base uses `voice_capture.js`; v2 uses
`@moonshine-ai/moonshine-wasm` 0.1.5 from jsDelivr and download.moonshine.ai.

## Sending and storage

Select a destination, provider, model and effort, then review and send. Normal
sending uses the AI account already connected to Studio chat; this app does not
call an LLM directly. Optional linked-machine access requires workspace read and
SSH permissions (see the root template manifest). Drafts and delivery history
are stored in `data/.apps/untitled-notes-voice`, outside the published tree.
`PROMPT_NOTES_DATA_DIR`, `PROMPT_NOTES_PORT` and `PROMPT_NOTES_CHAT_URL` allow
isolated testing. Remote destinations require `ssh`, `ssh-keygen` and Python 3.

Uncertain sends preserve the original text and never automatically resend. New
chat creation can be considered successful before the initial prompt appears in
chat history; inspect the destination before sending again.

## Verification

From the workspace root:

```sh
uv run --package untitled-notes-voice pytest -c system/apps/untitled_notes_voice/pyproject.toml system/apps/untitled_notes_voice -m 'not voice_model' --no-cov
node --test system/apps/untitled_notes_voice/src/prompt_notes/assets/voice_session.test.mjs
```

Browser tests use Fortress and disposable draft data. Real-model checks marked
`voice_model` require model downloads and roughly 2 GB of browser memory.

## New in v5

Provider and subscription choices now include pi accounts (Claude, Codex/ChatGPT,
OpenCode and other installed providers) and Antigravity. Destination defaults are
remembered per machine, model catalogs survive reloads, and sent notes can be
inspected, copied, removed or explicitly resent. The page detects updates and
includes home-screen icons and a web app manifest.

## Optional destination integrations

Claude, Codex and OpenCode need the corresponding connected account on each
chosen destination. Antigravity needs its connected account and installed command.
The pi picker requires pi and its account/model support on the destination. The
private deployment scripts and vendored Claude Code/shared-bridge and Codex-login
extensions are not included or automatically installed. Install compatible pi
provider extensions yourself if needed; missing pi or accounts appear unavailable.
No app module imports the excluded deployment tree. The developer tool that probes
third-party APIs for retired models is excluded; the static retired-model list is included.

`PROMPT_NOTES_PRIMARY_MACHINE` defaults to empty, so installations keep their own
notes. Set it to a linked destination id to use a shared primary store; that
requires the same read and destination SSH permissions as remote sending. When
that primary is unreachable, the app reports it and does not create a split store.
Private copying, cache-prefill and update schedules are excluded. Existing mirror
markers remain readable, but this template does not create or refresh copies.
