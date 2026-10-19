---
title: "Untitled Notes Voice"
description: "Draft messages with on-device voice dictation, then send them to chats across your Imbue Studio machines."
thumbnail: "template.svg"
version: v4
format: v2
---

# Untitled Notes Voice

This file is the manifest for the **Untitled Notes Voice** template (slug:
`untitled-notes-voice`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Draft messages with on-device voice dictation, then send them to chats across your Imbue Studio machines.

A quiet place to compose a message before sending it to an AI chat. Type or dictate into multiple autosaved drafts, then choose a new or existing chat on this Studio machine or a linked machine. The interface includes an editor, a speech-model menu, destination controls and an outbox.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/untitled_notes_voice`
- `system/supervisord.conf.d/untitled-notes-voice.conf`
- `pyproject.toml`
- `system/supervisord.conf.d/host-backup.conf`
- `system/supervisord.conf.d/share-gateway.conf`

The app directory contains the Flask server, browser voice modules, icon, manifest and existing functional/browser/Node tests. Its supervisor program registers `app.toml` with `forward_port.py` at port 8096 and runs `uv run --package untitled-notes-voice untitled-notes-voice` from `/home/user/workspace`. Root metadata retains the full base workspace and adds the local package mapping. The two baseline service configurations differ only by removal of comment-only credential references that the assembler otherwise mistakes for runtime requirements; every runtime field is unchanged.

Typed and recognized text autosaves to SQLite under `data/.apps/untitled-notes-voice`. Audio processing stays in the browser. Base uses Silero VAD, Transformers.js and Moonshine; v2 uses Moonshine's streaming WASM package. Browser modules load from public CDNs/model hosts and the workspace's built shell assets. Sending uses the local Studio chat service on port 8010; linked destinations use the workspace gateway and SSH. The app itself makes no direct LLM call.

The original app is external to the source monorepo. To update, stage a clean archive of the chosen external voice source into `system/apps/untitled_notes_voice`, reapply the rules in `template.toml`, then run the documented full-tree assembly. Do not expect the monorepo SHA alone to contain the app. This version maps source workspace `f2a3ae339767c332dc22473e260bc6c0cd4eb5d4` plus external `boweiliu/prompt-notes` voice commit `c532cbf134db2802f3b1acb55d7134d5b4cd39c4` onto clean base `15f79b99adf24c58c2c2177ecb3ad173798ccd2d`. The private source is provenance only; adopters need no access to it.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


- requires_permission: minds-workspaces / minds-workspaces-read (optional linked-machine browsing; initiate a workspace permission request during activation if using linked destinations)
- requires_permission: minds-workspaces / minds-workspaces-ssh (optional sending/browsing on each linked destination; initiate a targeted workspace permission request)

These are Studio workspace permissions, not predefined-service permissions. Use the local `minds-api` skill's exact request form: POST to `http://latchkey-self.invalid/permission-requests` with `agent_id` set to `${MINDS_CHAT_ID:-$MNGR_AGENT_ID}`, `type` set to `workspace`, a clear `rationale`, and `payload.permissions` containing the relevant permission. Omit `payload.target_workspace_id` for the read request; set it to the selected destination's agent id for SSH. Wait for approval and verify the destination. Local-only drafting/sending needs neither optional permission.

- Open dictation in a standalone HTTPS browser tab and grant microphone permission. Current Studio frames block microphone; v2 choices also require cross-origin isolation. The app supplies the isolation headers for its own tab. First use needs internet to download browser runtimes/models. Moonshine Base is the default; right-click or long-press the microphone to choose v2 Tiny, Small or Medium. v2 Small may not keep up on modest CPUs.
- Sending requires an AI account already connected to Studio chat on the chosen machine. The app delegates through Studio, so there is no app-specific LLM method, transcription key or secret to supply.
- Review uncertain sends at their destination before retrying manually: the original text is kept and never automatically resent. Creating a new chat can be treated as success before the initial prompt appears in its history. This existing behavior is preserved.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

The native package declares Flask, flask-sock and Werkzeug; uv installs them. `openssh-client` supplies `ssh` and `ssh-keygen` for optional linked-machine access. Python 3, uv and the shell's built browser modules are supplied by the base. Node is used by the existing test suite only. Browser speech runtimes/models are fetched on first use, not installed as global server tools.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-10-19) -- Browser dictation, autosaved drafts and Studio chat destinations

First public packaging of external voice source `16a40263d4f303b804e4d866a0600e26c4eddf21` (`boweiliu/prompt-notes`, voice), staged with source workspace `8fc760b88fdcba8bd41ccecda5219315689ace90`. Includes native installation and existing tests, with private deployment automation and runtime data excluded. Two comment-only baseline credential references are removed to avoid a known assembler false positive; runtime settings are unchanged. Local Vocal attribution and Moonshine dependency notices are retained. No real drafts, recordings, accounts or source history are included.

### v2 (2026-10-19) -- Native workspace compatibility checks

Add the standard native-package checks and typing compatibility, keep the existing draft-persistence browser check with an explicit bounded timeout, and register the real-model test marker. Executable server behavior, browser voice modules, source anchors, requirements and private exclusions are unchanged.

### v3 (2026-10-19) -- Tab from the title goes to the note text

Refresh the app from external voice source `2ae501e8f88a5c2d52d13a5d6d850aaaa8717245`: pressing Tab in the draft title now moves the cursor into the note text instead of the header buttons; Shift+Tab is unchanged. Adds a browser check for it. Server behavior, voice modules, requirements and private exclusions are unchanged.

### v4 (2026-10-19) -- Destination choices load instantly

Refresh the app from external voice source `c532cbf134db2802f3b1acb55d7134d5b4cd39c4`: the server remembers the last machine list, model lists and chat lists (in memory and in `choice_cache.json` under the app's data folder) and answers from that copy at once, refreshing it in the background when it is over 30 seconds old; the refresh buttons ask again. A new browser no longer waits for a linked machine before the model menu fills in. Adds checks for it. Requirements and private exclusions are unchanged.

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
