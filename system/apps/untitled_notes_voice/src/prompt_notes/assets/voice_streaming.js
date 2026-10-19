// Moonshine v2 streaming models (tiny / small / medium), through Moonshine's own
// browser package, @moonshine-ai/moonshine-wasm. It does its own speech detection
// and streaming, so none of voice_capture.js's pipeline is involved; the page drives
// both engines through the same callbacks.
//
//   MoonshineStreaming.start({ arch, onStatus, onCaption, onTurn, onListening, onError })
//     -> Promise<{ stop() }>
//   MoonshineStreaming.preload(arch) -> Promise, resolves once the model is loaded
//
// arch is a ModelArch name ("TinyStreaming", "SmallStreaming", "MediumStreaming").
// The package's multithreaded build needs a cross-origin-isolated page (the server
// sends COOP/COEP); start() rejects with an IsolationError otherwise. A loaded model
// stays in memory for the next session; models are cached by the browser after the
// first download.
(function (root) {
  "use strict";

  const PACKAGE = "https://cdn.jsdelivr.net/npm/@moonshine-ai/moonshine-wasm@0.1.5/dist/index.js";
  const LOG = "[notes voice v2]";

  let library = null;
  const loaded = {};        // arch -> Promise<MicTranscriber>
  let session = null;       // the active session's callbacks
  let progress = null;      // the loading session's progress callback

  function packageModule() {
    return library || (library = import(PACKAGE));
  }

  function transcriberFor(arch) {
    if (!loaded[arch]) {
      loaded[arch] = (async () => {
        const moonshine = await packageModule();
        if (!(arch in moonshine.ModelArch)) throw new Error("unknown model " + arch);
        console.log(LOG, "loading", arch);
        const mic = new moonshine.MicTranscriber()
          .language("en")
          .modelArch(moonshine.ModelArch[arch])
          .onText((text) => { if (session) session.onText(text); })
          .onLine((line) => { if (session) session.onLine(line); })
          .onError((error) => { console.error(LOG, error); if (session) session.onError(error); })
          .onProgress((fraction) => { if (progress) progress(fraction); });
        await mic.load();
        console.log(LOG, arch, "ready");
        return mic;
      })();
      loaded[arch].catch(() => { delete loaded[arch]; });   // a failed load is retried next time
    }
    return loaded[arch];
  }

  async function start(options) {
    if (!root.crossOriginIsolated) {
      const error = new Error("This model needs a cross-origin-isolated page.");
      error.name = "IsolationError";
      throw error;
    }
    const { arch, onStatus, onCaption, onTurn, onListening, onError } = options;
    progress = (fraction) => onStatus("loading", Math.round(fraction * 100) + "%");
    onStatus("loading", "");
    let mic;
    try { mic = await transcriberFor(arch); } finally { progress = null; }
    session = {
      onText: (text) => onCaption("", text || ""),
      onLine: (line) => {
        const text = ((line && line.text) || "").trim();
        onCaption("", "");
        if (text) onTurn(text);
      },
      onError,
    };
    const mine = session;
    try {
      await mic.start();
    } catch (error) {
      if (session === mine) session = null;
      throw error;
    }
    console.log(LOG, "listening");
    onListening();
    return {
      // Stopping flushes the line in progress through onLine before the session ends.
      stop: async () => {
        try { await mic.stop(); } finally { if (session === mine) session = null; }
      },
    };
  }

  // Load a model ahead of the first press (resolves when it is ready).
  function preload(arch) { return transcriberFor(arch).then(() => undefined); }

  root.MoonshineStreaming = { start, preload };
})(typeof self !== "undefined" ? self : this);
