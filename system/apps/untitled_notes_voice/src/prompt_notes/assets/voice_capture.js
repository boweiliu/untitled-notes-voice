// Adopted from Local Vocal (https://github.com/DanverImbue/local-vocal), an on-device voice
// inspiration by Danver Braganza, by way of the Red Scribble copy (boweiliu/red-scribble), which
// added microphone pre-warming, an honest "listening" signal, and silent-session diagnostics.
// Untitled Notes Voice keeps voice IN only: the text-to-speech half is removed.

// Local-Vocal: the reusable on-device voice driver.
//
// This is the DOM-free glue around the voice model (voice_session.js). It owns
// the microphone pipeline and the speech pipeline; the page passes callbacks and
// never touches the worker, the VAD, or the audio buffers directly.
//
//   Voice IN  (listen):
//     - Silero VAD (@ricky0123/vad-web) detects real speech segments.
//     - Moonshine ASR (@huggingface/transformers, in a Web Worker) transcribes
//       -- fast, good on short audio, no word timestamps.
//     - ONE streaming mechanism (VoiceSession.StreamingTurn) powers both the live
//       caption and each finalized turn: the turn's audio rolls through a bounded
//       ~20s window; heads are sliced off at genuine pauses, transcribed, and
//       folded into the turn's committed text. The caption is always
//       `committed + window hypothesis`; the finalized turn is just the committed
//       text once the last chunk folds in. See voice_session.js for the model.
//
// Everything runs in the browser: no API, no key, no server round-trip. The
// models are fetched once from a CDN and cached by the browser.
//
// Public API (all on the global `LocalVocal`):
//   LocalVocal.init({ onStatus, preferCpu })   -> begin loading the ASR model (idempotent); preferCpu
//                                                  skips WebGPU for the smaller quantized CPU model
//   LocalVocal.status                          -> "idle" | "loading" | "ready" | "error"
//   LocalVocal.whenReady()                     -> Promise, resolves when the ASR model is loaded
//   LocalVocal.prewarmMic()                    -> Promise, requests mic permission and builds (but
//                                                  does not start) a VAD instance ahead of time, so
//                                                  a later listen() can start capturing instantly
//                                                  instead of racing a short hold against setup. Call
//                                                  this as soon as voice capture becomes reachable
//                                                  (e.g. entering annotation mode), not on the hold
//                                                  itself. Safe to call speculatively; failures are
//                                                  swallowed and listen() falls back to loading inline.
//   LocalVocal.releaseMic()                    -> tear down any prewarmed/paused mic instance and
//                                                  release the stream (the browser's mic-in-use
//                                                  indicator won't clear until this runs -- stop()
//                                                  deliberately keeps the mic warm between holds).
//   LocalVocal.listen({ onCaption, onTurn, onError, onLevel, onStatus, onListening }) -> { stop() }
//   LocalVocal.encodeWav(float32, sampleRate)  -> Blob (16-bit PCM WAV of the samples)
//   LocalVocal.lastError                       -> the most recent pipeline error ({message, name,
//                                                  time, stack}) or null; for a console to inspect
//                                                  after a failure, without a callback wired up
//
// Every stage of the pipeline (model download, device selection, mic permission,
// each transcription, retries) also logs to the console with a "[red-scribble
// voice]" prefix, so `console` filtered to that string is a full trace of what
// happened during a session.
//
// Callbacks:
//   onStatus(status, detail)   loading progress: status as above, detail a string (e.g. "42%")
//   onCaption(stable, tentative)  the live caption of the active turn; stable = committed +
//                                 agreed hypothesis prefix, tentative = the rest. Both "" clears it.
//   onTurn(text, wavBlob)      a finalized turn: its committed text and the exact captured audio.
//   onLevel(level)             per-frame mic level in [0,1] for a meter, even between turns.
//   onError(error)             a recoverable pipeline error (model load, transcribe, mic).
//   onListening()              the mic is actually capturing now (not just requested) -- the point
//                              at which it is safe to assume speech can be picked up.
//
// Loads as a browser global (window.LocalVocal) via a <script> tag; it reads the
// voice model from the global `VoiceSession` (load voice_session.js first).
(function (root) {
  "use strict";

  // ---- CDN libraries (same versions the model was validated against) ----
  const MODEL = "onnx-community/moonshine-base-ONNX";
  const VAD_LIB = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.30/dist/bundle.min.js";
  const VAD_ASSETS = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.30/dist/";  // silero model + worklet
  const ORT_WASM = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/";       // matches vad-web@0.0.30 bundle
  const TRANSFORMERS = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3";

  const SR = 16000;         // the VAD's sample rate (and the model's)
  const STREAM_MS = 500;    // how often to refresh the live caption
  const MIN_INTERIM_S = 0.3;   // don't transcribe a window shorter than this for a caption
  const PAUSE_COMMIT_MS = 700;  // a pause this long hands the phrase before it to the ASR (see voice_session.js)

  // ---- diagnostics ----
  // Every log from this module carries this prefix so it's easy to filter the
  // browser console down to just the voice pipeline while debugging.
  const LOG = "[notes voice]";
  // The most recent pipeline error, parked here (not just handed to onError)
  // so a dev console can inspect `LocalVocal.lastError` after the fact and
  // decide whether a manual retry (calling `listen()` again) is worth it,
  // without needing to have had a callback wired up at the time it happened.
  let lastError = null;

  function voiceSession() {
    const vs = root.VoiceSession;
    if (!vs) throw new Error("VoiceSession is not loaded (include voice_session.js before voice_capture.js)");
    return vs;
  }

  // ---- WAV encode (the exact captured utterance, for playback / download) ----
  function encodeWav(float32, sampleRate) {
    const len = float32.length, buffer = new ArrayBuffer(44 + len * 2), view = new DataView(buffer);
    const writeStr = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
    writeStr(0, "RIFF"); view.setUint32(4, 36 + len * 2, true); writeStr(8, "WAVE");
    writeStr(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    writeStr(36, "data"); view.setUint32(40, len * 2, true);
    let o = 44; for (let i = 0; i < len; i++) { const s = Math.max(-1, Math.min(1, float32[i])); view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true); o += 2; }
    return new Blob([view], { type: "audio/wav" });
  }

  // ---- module state ----
  // status of the ASR model (the thing `listen` waits for).
  let status = "idle";               // "idle" | "loading" | "ready" | "error"
  let statusCb = null;               // onStatus(status, detail)
  let statusDetail = "";
  let preferCpu = false;             // init({ preferCpu }): use the quantized CPU model even with a GPU
  function setStatus(s, detail) {
    status = s; statusDetail = detail || "";
    if (statusCb) { try { statusCb(status, statusDetail); } catch (_) {} }
  }

  // The transcription worker is a persistent singleton (loaded once, shared by
  // every listen session). Its callbacks route into whichever session is active;
  // a result for a segment the active session doesn't know is ignored.
  let worker = null, modelReady = false, inFlight = false, pendingInterim = null;
  const commitQueue = [];            // FIFO of transcribe-and-commit jobs (head slices + drained chunks)
  let readyPromise = null, readyResolve = null;

  // The current listen session's callbacks (one microphone, so one at a time).
  let callbacks = { onCaption: null, onTurn: null, onError: null, onLevel: null, onListening: null };
  let listening = false, myvad = null;

  // A MicVAD instance built ahead of time (before the user actually holds to
  // talk) so `listen()` can skip straight to `.start()` instead of paying for
  // getUserMedia + model load on the critical path of a short hold. See
  // `prewarmMic`/`releaseMic`. `warmVadPromise` covers the in-flight case: a
  // hold that starts before pre-warming has finished still waits on the SAME
  // instance rather than kicking off a second one.
  let warmVad = null, warmVadPromise = null;

  // Per-turn streaming state (mirrors the interview loop's bookkeeping, minus the
  // DOM): a finishing turn can still be folding its last chunks while the next
  // turn has already begun, so state is keyed by segment id.
  let inSpeech = false, segId = 0, lastStream = 0, activeTurn = null, captionSeg = null;
  const turnsBySeg = {};             // seg -> StreamingTurn, until the turn finalizes
  const pendingCommits = {};         // seg -> transcribe-and-commit jobs still outstanding
  const sealedSegs = new Set();      // segments whose speech ended, awaiting their last commit
  const audioBySeg = {};             // seg -> the full raw utterance (Float32Array), for the WAV
  const speechStartedAt = {};        // seg -> performance.now() when onSpeechStart fired, for duration logs

  // One hold session's tally, reset each `listen()` call and logged as a
  // single summary line in `stop()` -- this is the direct answer to "did a
  // recording actually come through, and for how long." `sessionMaxLevel` and
  // `sessionMisfires` exist specifically to explain a *silent* session: a
  // near-zero peak level means the mic itself picked up nothing (wrong
  // device, muted, no signal reaching it), while a real peak level plus one
  // or more misfires means the VAD saw audio but judged every attempt too
  // short or too noise-like -- two different problems with different fixes.
  let sessionStartedAt = 0, sessionSegments = 0, sessionSpeechS = 0;
  let sessionMaxLevel = 0, sessionMisfires = 0;

  let vadLibPromise = null;

  const emitCaption = (stable, tentative) => { if (callbacks.onCaption) { try { callbacks.onCaption(stable, tentative); } catch (_) {} } };
  const emitTurn = (text, wav) => { if (callbacks.onTurn) { try { callbacks.onTurn(text, wav); } catch (_) {} } };
  // Every error passes through here, so this is the one place that both logs
  // it and parks it as `lastError` -- callers that see nothing else at least
  // get this in the console and on `LocalVocal.lastError`.
  const emitError = (err) => {
    lastError = { message: (err && err.message) || String(err), name: err && err.name, time: Date.now(), stack: err && err.stack };
    console.error(LOG, "error:", err);
    if (callbacks.onError) { try { callbacks.onError(err); } catch (_) {} }
  };
  const emitLevel = (level) => { if (callbacks.onLevel) { try { callbacks.onLevel(level); } catch (_) {} } };

  // ---------- transcription worker (Moonshine) ----------
  const WORKER_SRC = `
    import { pipeline, env } from "${TRANSFORMERS}";
    env.allowLocalModels = false;
    // Single-threaded, as tuned: the page is cross-origin isolated for the v2 models, which
    // would otherwise let ONNX Runtime spawn a thread pool that starves live audio.
    env.backends.onnx.wasm.numThreads = 1;
    let asr = null;
    async function load(model, device, dtype) {
      const cb = (data) => postMessage({ type: "progress", data });
      console.log("${LOG} worker: loading", model, "device=" + device, "dtype=" + dtype);
      try {
        asr = await pipeline("automatic-speech-recognition", model, { device, dtype, progress_callback: cb });
        console.log("${LOG} worker: model ready on device=" + device);
        postMessage({ type: "ready", device });
      } catch (e) {
        console.warn("${LOG} worker: device=" + device + " failed, retrying on device=wasm dtype=q8:", e);
        postMessage({ type: "progress", data: { status: "fallback", message: String(e) } });
        asr = await pipeline("automatic-speech-recognition", model, { device: "wasm", dtype: "q8", progress_callback: cb });
        console.log("${LOG} worker: model ready on fallback device=wasm");
        postMessage({ type: "ready", device: "wasm" });
      }
    }
    onmessage = async (e) => {
      const m = e.data;
      if (m.type === "load") { try { await load(m.model, m.device, m.dtype); } catch (err) { console.error("${LOG} worker: model load failed:", err); postMessage({ type: "error", error: String(err) }); } }
      else if (m.type === "transcribe") {
        if (!asr) { postMessage({ type: "result", text: "", kind: m.kind, seg: m.seg, epoch: m.epoch }); return; }
        console.log("${LOG} worker: transcribe start", { kind: m.kind, seg: m.seg, samples: m.audio && m.audio.length });
        try {
          const out = await asr(m.audio);
          const text = (out.text || "").trim();
          console.log("${LOG} worker: transcribe ok", { kind: m.kind, seg: m.seg, chars: text.length });
          postMessage({ type: "result", text, kind: m.kind, seg: m.seg, epoch: m.epoch });
        } catch (err) {
          console.error("${LOG} worker: transcribe failed", { kind: m.kind, seg: m.seg }, err);
          postMessage({ type: "error", error: String(err), kind: m.kind, seg: m.seg });
        }
      }
    };
  `;

  // transformers.js fires its progress_callback once per chunk, per file, with
  // several files downloading in parallel -- easily thousands of calls for one
  // model. Log only when a given file's rounded-to-10% bucket actually
  // advances (keyed by file, not globally -- otherwise two files interleaving
  // makes every callback look like "new" progress even when neither file
  // alone has advanced 10%), so the console gets a sparse, still-useful trail
  // per file instead of a flood that buries everything else.
  let lastLoggedProgressBuckets = {};
  // Bytes loaded/total per model file, so the status shows one overall percentage
  // instead of whichever file reported last.
  let progressByFile = {};

  async function startWorker() {
    lastLoggedProgressBuckets = {};   // a fresh load (or a retry) starts its own progress trail
    progressByFile = {};
    worker = new Worker(URL.createObjectURL(new Blob([WORKER_SRC], { type: "application/javascript" })), { type: "module" });
    worker.onmessage = (e) => {
      const m = e.data;
      if (m.type === "progress") {
        if (m.data && m.data.status === "progress" && m.data.total) {
          const pctNum = Math.round((m.data.loaded / m.data.total) * 100);
          const pct = pctNum + "%";
          const file = m.data.file || "model";
          progressByFile[file] = { loaded: m.data.loaded, total: m.data.total };
          let loaded = 0, total = 0;
          for (const entry of Object.values(progressByFile)) { loaded += entry.loaded; total += entry.total; }
          setStatus("loading", Math.round((loaded / total) * 100) + "%");
          const bucket = Math.floor(pctNum / 10);
          if (bucket !== lastLoggedProgressBuckets[file]) {
            lastLoggedProgressBuckets[file] = bucket;
            console.log(LOG, "model download progress:", file, pct);
          }
        } else if (m.data && m.data.status === "fallback") {
          setStatus("loading", "CPU");
          console.warn(LOG, "primary device failed, retrying model load on CPU (wasm):", m.data.message);
        }
      } else if (m.type === "ready") {
        modelReady = true; setStatus("ready", "");
        console.log(LOG, "model ready, device:", m.device);
        if (readyResolve) { readyResolve(); readyResolve = null; }
      } else if (m.type === "result") {
        inFlight = false;
        console.log(LOG, "transcription result received", { kind: m.kind, seg: m.seg, chars: (m.text || "").length });
        applyTranscription(m); pump();
      }
      else if (m.type === "error") {
        inFlight = false;
        emitError(new Error("transcription: " + m.error));
        // A failed transcribe-and-commit job loses that chunk's words, but must
        // not leave its turn waiting forever: settle the count and move on.
        if (m.kind === "commit" && m.seg !== undefined) settleCommit(m.seg);
        pump();
      }
    };
    // A worker THREAD crash (not a caught job error) fires here, never onmessage,
    // so inFlight would otherwise stay stuck true and stall transcription forever.
    // Recover by resetting and reloading the model. Queued jobs are dropped, so
    // any turn waiting on them finalizes with the text it has committed so far.
    worker.onerror = (e) => {
      emitError(new Error("transcription worker crashed; reloading: " + ((e && e.message) || e)));
      console.warn(LOG, "retrying: starting a fresh worker after the crash above");
      inFlight = false;
      try { worker.terminate(); } catch (_) {}
      modelReady = false; setStatus("loading", "");
      commitQueue.length = 0; pendingInterim = null;
      for (const seg in pendingCommits) pendingCommits[seg] = 0;
      for (const seg of [...sealedSegs]) maybeFinalize(seg);
      startWorker();
    };
    // Pick the device on the main thread: only use WebGPU if a real adapter exists. A
    // fallback adapter is software rendering, many times slower than the CPU model.
    let device = "wasm", dtype = "q8";
    try {
      const adapter = !preferCpu && navigator.gpu ? await navigator.gpu.requestAdapter() : null;
      if (adapter && !adapter.isFallbackAdapter && !(adapter.info && adapter.info.isFallbackAdapter)) { device = "webgpu"; dtype = "fp32"; }
    } catch (_) {}
    console.log(LOG, "selected device:", device);
    worker.postMessage({ type: "load", model: MODEL, device, dtype });
  }

  // One job at a time. Commit jobs (head slices + drained chunks) drain FIFO
  // before interims, so folds land in slice order and a stale caption refresh
  // never delays the record. Audio buffers are transferred; the model already
  // handed out copies, so nothing retained is detached.
  function requestInterim(audio, seg, epoch) { pendingInterim = { audio, seg, epoch }; pump(); }
  function requestCommit(audio, seg) {
    pendingCommits[seg] = (pendingCommits[seg] || 0) + 1;
    commitQueue.push({ audio, seg });
    pump();
  }
  function pump() {
    if (inFlight || !worker) return;
    let job = null, kind = null;
    if (commitQueue.length) { job = commitQueue.shift(); kind = "commit"; }
    else if (pendingInterim) { job = pendingInterim; pendingInterim = null; kind = "interim"; }
    if (!job) return;
    inFlight = true;
    worker.postMessage({ type: "transcribe", audio: job.audio, kind, seg: job.seg, epoch: job.epoch }, [job.audio.buffer]);
  }

  function applyTranscription(m) {
    const turn = turnsBySeg[m.seg];
    if (!turn) return;   // the segment was cancelled (misfire / session stopped)
    if (m.kind === "interim") {
      turn.applyHypothesis(m.text, m.epoch);   // stale epochs are ignored inside
      renderLive(m.seg);
      return;
    }
    turn.commitText(m.text);
    renderLive(m.seg);   // the caption's committed prefix just grew
    settleCommit(m.seg);
  }

  function settleCommit(seg) {
    if (pendingCommits[seg] !== undefined) pendingCommits[seg] -= 1;
    maybeFinalize(seg);
  }

  // The live caption shows only the active turn: all committed text plus the
  // active window's hypothesis (stable prefix, then tentative). A finishing turn
  // that is no longer the caption owner does not overwrite the caption.
  function renderLive(seg) {
    if (Number(seg) !== captionSeg) return;
    const turn = turnsBySeg[seg];
    if (!turn) return;
    const parts = turn.liveParts();
    emitCaption(parts.stable, parts.tentative);
  }

  // A turn finalizes once its speech has ended AND its last chunk has folded in.
  function maybeFinalize(seg) {
    if (!sealedSegs.has(Number(seg)) || (pendingCommits[seg] || 0) > 0) return;
    sealedSegs.delete(Number(seg));
    delete pendingCommits[seg];
    const turn = turnsBySeg[seg];
    delete turnsBySeg[seg];
    // The saved turn IS the model's committed text -- the same text the live
    // caption was showing, read at the moment the turn ended.
    const text = turn ? turn.committedText : "";
    if (Number(seg) === captionSeg) { captionSeg = null; emitCaption("", ""); }
    const audio = audioBySeg[seg]; delete audioBySeg[seg];
    if (text) {
      console.log(LOG, "segment", seg, "transcribed:", JSON.stringify(text));
      emitTurn(text, audio ? encodeWav(audio, SR) : null);
    } else {
      console.warn(LOG, "segment", seg, "ended with no transcribed text (speech detected but nothing recognized)");
    }
  }

  // Abandon all in-progress voice capture (a fresh listen): pending jobs and
  // unfinalized turns are dropped, so no late fold can write into a new session.
  function discardPendingTurns() {
    commitQueue.length = 0; pendingInterim = null;
    for (const seg in turnsBySeg) delete turnsBySeg[seg];
    for (const seg in pendingCommits) delete pendingCommits[seg];
    for (const seg in audioBySeg) delete audioBySeg[seg];
    sealedSegs.clear();
    captionSeg = null; activeTurn = null;
  }

  // ---------- Silero VAD callbacks ----------
  function onSpeechStart() {
    if (!listening) return;
    inSpeech = true; segId++; lastStream = (typeof performance !== "undefined" ? performance.now() : Date.now());
    speechStartedAt[segId] = lastStream;
    activeTurn = new (voiceSession().StreamingTurn)({ pauseCommitMs: PAUSE_COMMIT_MS });
    turnsBySeg[segId] = activeTurn;
    captionSeg = segId;
    emitCaption("", "");   // a fresh, empty caption line for the new turn
    console.log(LOG, "speech detected, segment", segId);
  }
  function onFrameProcessed(_probs, frame) {
    const level = Math.min(1, voiceSession().frameRms(frame) * 4);
    if (level > sessionMaxLevel) sessionMaxLevel = level;
    emitLevel(level);
    if (!inSpeech || !activeTurn) return;
    activeTurn.addFrame(frame.slice(0));   // copy: the VAD reuses its frame buffer
    // Slide the window: once it exceeds ~20s the model slices a head off at a
    // genuine pause; transcribing-and-committing that head is what keeps both the
    // caption and the record growing at any turn length.
    for (let head; (head = activeTurn.takeHeadSlice()); ) requestCommit(head, segId);
    const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
    if (now - lastStream > STREAM_MS) {
      lastStream = now;
      const w = activeTurn.windowAudio();
      if (w.audio.length >= SR * MIN_INTERIM_S) requestInterim(w.audio, segId, w.epoch);
    }
  }
  function onSpeechEnd(audio) {
    inSpeech = false;
    audioBySeg[segId] = audio;   // the full raw utterance, kept for the WAV
    const durationS = audio.length / SR;
    sessionSegments += 1; sessionSpeechS += durationS;
    console.log(LOG, "speech ended, segment", segId, "duration:", durationS.toFixed(2) + "s");
    sealTurn(segId);
  }
  // End the segment's capture: drain what's left of its window into
  // transcribe-and-commit chunks and finalize once they have all folded in.
  function sealTurn(seg) {
    const turn = turnsBySeg[seg];
    if (!turn || sealedSegs.has(seg)) return;
    if (pendingInterim && pendingInterim.seg === seg) pendingInterim = null;   // stale window
    for (const chunk of turn.drainChunks()) requestCommit(chunk, seg);
    sealedSegs.add(seg);
    maybeFinalize(seg);          // nothing left to transcribe -> finalize now
  }
  function onVADMisfire() {
    inSpeech = false;
    sessionMisfires += 1;
    console.log(LOG, "VAD misfire, segment", segId, "-- too short or noise, discarded (not counted as a recording)");
    delete turnsBySeg[segId]; delete pendingCommits[segId]; delete audioBySeg[segId];
    activeTurn = null;
    if (segId === captionSeg) { captionSeg = null; emitCaption("", ""); }
  }

  // ---------- VAD library loading ----------
  // Prefer an already-present global (a page may include the <script> itself);
  // otherwise inject the CDN bundle once and resolve when its global appears.
  function singleThreaded(vadLib) {
    // See the worker: keep the VAD's ONNX Runtime single-threaded on an isolated page too.
    try { if (vadLib.ort && vadLib.ort.env && vadLib.ort.env.wasm) vadLib.ort.env.wasm.numThreads = 1; } catch (_) {}
    return vadLib;
  }

  function loadVadLib() {
    return loadVadScript().then(singleThreaded);
  }

  function loadVadScript() {
    if (root.vad && root.vad.MicVAD) return Promise.resolve(root.vad);
    if (vadLibPromise) return vadLibPromise;
    vadLibPromise = new Promise((resolve, reject) => {
      if (typeof document === "undefined") { reject(new Error("no document to load the VAD library")); return; }
      const script = document.createElement("script");
      script.src = VAD_LIB;
      script.onload = () => {
        if (root.vad && root.vad.MicVAD) resolve(root.vad);
        else reject(new Error("VAD library loaded but window.vad is missing"));
      };
      script.onerror = () => reject(new Error("failed to load the VAD library from " + VAD_LIB));
      document.head.appendChild(script);
    });
    return vadLibPromise;
  }

  // The MicVAD constructor options, shared between the pre-warm path and the
  // inline fallback so they build an identical instance either way.
  function micVadOptions() {
    return {
      model: "v5",
      baseAssetPath: VAD_ASSETS,
      onnxWASMBasePath: ORT_WASM,
      // Lower than Local-Vocal's 0.5/0.35 (the library's own defaults are 0.3/0.25): a
      // quiet or muffled short phrase rarely scores 0.5, and dictation should keep it.
      positiveSpeechThreshold: 0.4,
      negativeSpeechThreshold: 0.3,
      redemptionMs: 800,        // ~0.8s silence ends an utterance
      preSpeechPadMs: 600,      // keep a soft first word that rises before detection fires
      // Dictation is deliberate, so a short phrase ("ask not", "yes") is kept rather
      // than discarded as a misfire; at 200ms Silero's confident frames for such a
      // phrase often fell short.
      minSpeechMs: 64,
      onSpeechStart, onFrameProcessed, onSpeechEnd, onVADMisfire,
    };
  }

  // Best-effort: log which physical/virtual input device the mic stream is
  // actually reading from, and whether the browser itself reports the track
  // muted/ended. `_stream` is an undocumented vad-web internal (confirmed
  // present as of @ricky0123/vad-web@0.0.30), so this never throws past its
  // own try/catch -- a peak level stuck at 0% is otherwise unexplainable from
  // inside this module, since a silent-but-open stream and a genuinely broken
  // one look identical to everything else here.
  function logCaptureDevice(instance) {
    try {
      const track = instance._stream && instance._stream.getAudioTracks && instance._stream.getAudioTracks()[0];
      if (!track) { console.warn(LOG, "could not inspect the mic stream's track (no track found)"); return; }
      const settings = track.getSettings ? track.getSettings() : {};
      console.log(
        LOG, "capturing from device:", track.label || "(no label -- device permission not fully granted?)",
        "| readyState:", track.readyState, "| muted:", track.muted,
        "| deviceId:", settings.deviceId || "(unknown)",
      );
    } catch (err) {
      console.warn(LOG, "could not inspect the mic stream's device:", err);
    }
  }

  // ---------- public API ----------
  function init(options) {
    if (options && options.onStatus) statusCb = options.onStatus;
    if (options && options.preferCpu) preferCpu = true;
    if (worker) return;
    console.log(LOG, "init: loading ASR model", MODEL);
    setStatus("loading", "");
    readyPromise = new Promise((resolve) => { readyResolve = resolve; });
    startWorker();
  }

  function whenReady() {
    init();
    return modelReady ? Promise.resolve() : readyPromise;
  }

  // Build (but do not start) a MicVAD instance ahead of an actual hold, so
  // `listen()` can skip straight to `.start()` instead of paying for
  // getUserMedia + the VAD's own model load on the critical path of a short
  // hold-to-talk -- that gap was exactly what let a quick hold end before any
  // speech was ever captured. Idempotent, and safe to call speculatively: a
  // failed pre-warm (denied permission, network) just means `listen()` falls
  // back to building its own instance inline, same as before this existed.
  function prewarmMic() {
    if (warmVad || warmVadPromise) return warmVadPromise || Promise.resolve(warmVad);
    console.log(LOG, "pre-warming the microphone in the background");
    warmVadPromise = loadVadLib()
      .then((vadLib) => vadLib.MicVAD.new(micVadOptions()))
      .then((instance) => {
        warmVad = instance;
        console.log(LOG, "microphone pre-warmed; the next hold-to-talk can start instantly");
        return instance;
      })
      .catch((err) => {
        lastError = { message: (err && err.message) || String(err), name: err && err.name, time: Date.now(), stack: err && err.stack };
        console.warn(LOG, "microphone pre-warm failed (listen() will fall back to loading on hold):", err);
        warmVadPromise = null;
        return null;
      });
    return warmVadPromise;
  }

  // Tear down any pre-warmed or paused-between-holds mic instance and release
  // the underlying stream, so the browser's mic-in-use indicator actually goes
  // away. Call this when leaving voice-capable UI (e.g. annotation mode) --
  // `stop()` alone deliberately keeps the mic warm for the next hold.
  function releaseMic() {
    if (myvad) { try { myvad.destroy(); } catch (_) {} myvad = null; }
    if (warmVad) { try { warmVad.destroy(); } catch (_) {} warmVad = null; }
    warmVadPromise = null;
  }

  function listen(options) {
    const opts = options || {};
    if (opts.onStatus) statusCb = opts.onStatus;
    // One microphone: a new listen replaces any prior session cleanly.
    if (listening || myvad) stop();
    discardPendingTurns();
    callbacks = {
      onCaption: opts.onCaption || null,
      onTurn: opts.onTurn || null,
      onError: opts.onError || null,
      onLevel: opts.onLevel || null,
      onListening: opts.onListening || null,   // fires once the mic is truly capturing, not just requested
    };
    listening = true;
    sessionStartedAt = (typeof performance !== "undefined" ? performance.now() : Date.now());
    sessionSegments = 0; sessionSpeechS = 0; sessionMaxLevel = 0; sessionMisfires = 0;
    init();
    (async () => {
      try {
        await whenReady();
        if (!listening) return;   // stopped while the model was still loading
        let instance = null;
        if (warmVad || warmVadPromise) {
          console.log(LOG, "reusing the pre-warmed microphone");
          instance = warmVad || (await warmVadPromise);
          warmVad = null; warmVadPromise = null;
        }
        if (!instance) {
          const vadLib = await loadVadLib();
          if (!listening) return;
          // MicVAD.new() is what actually calls getUserMedia -- this is the point
          // at which the browser's microphone permission prompt fires (or, if the
          // page has no permission to ask at all, the point it silently fails).
          console.log(LOG, "requesting microphone permission...");
          instance = await vadLib.MicVAD.new(micVadOptions());
        }
        if (!listening) { try { instance.destroy(); } catch (_) {} return; }
        myvad = instance;
        await myvad.start();
        logCaptureDevice(instance);
        console.log(LOG, "microphone active; listening for speech");
        if (callbacks.onListening) { try { callbacks.onListening(); } catch (_) {} }
      } catch (err) {
        listening = false;
        if (err && err.name === "NotAllowedError") {
          console.error(
            LOG,
            "microphone permission denied (NotAllowedError). If this happens with no permission " +
              "prompt at all, the embedding page's iframe likely doesn't delegate `microphone` in its " +
              "Permissions-Policy `allow=` attribute -- the browser rejects getUserMedia before ever " +
              "asking. Otherwise, the user (or a prior site visit) denied the prompt; they need to " +
              "grant it from the browser's own site settings before a retry can succeed.",
            err,
          );
        }
        emitError(err instanceof Error ? err : new Error(String(err)));
      }
    })();
    return { stop };
  }

  function stop() {
    listening = false;
    // If the stop cut a turn off mid-speech, seal it: the words captured so far
    // drain through the normal commit path and still land as a finalized turn
    // (the worker keeps draining the queue after the mic is gone).
    if (inSpeech && activeTurn) sealTurn(segId);
    inSpeech = false; activeTurn = null;
    pendingInterim = null;
    if (sessionStartedAt) {
      const heldS = ((typeof performance !== "undefined" ? performance.now() : Date.now()) - sessionStartedAt) / 1000;
      if (sessionSegments > 0) {
        console.log(
          LOG, "session ended: held", heldS.toFixed(2) + "s,",
          sessionSegments, sessionSegments === 1 ? "speech segment" : "speech segments",
          "captured, total speech", sessionSpeechS.toFixed(2) + "s",
        );
      } else {
        // Distinguish the two very different reasons nothing was recorded: a
        // near-silent mic (wrong/no device, muted, no signal reaching it) vs.
        // a mic that did pick up audio but the VAD never called it speech.
        const peakPct = Math.round(sessionMaxLevel * 100);
        let why;
        if (sessionMaxLevel < 0.05) {
          why =
            "the mic picked up almost no audio at all (peak level " + peakPct + "%) -- check the " +
            "right microphone is selected and unmuted at the OS/browser level, and that this tab " +
            "still has an active mic permission (see LocalVocal.lastError and the permission logs above)";
        } else if (sessionMisfires > 0) {
          why =
            "the mic picked up audio (peak level " + peakPct + "%) but " + sessionMisfires +
            (sessionMisfires === 1 ? " attempt was" : " attempts were") +
            " judged too short or noise-like and discarded -- try holding a beat longer once you start talking";
        } else {
          why =
            "the mic picked up audio (peak level " + peakPct + "%) but it never crossed the " +
            "speech-detection threshold -- try speaking louder, closer to the mic, or check for " +
            "background noise that could be masking your voice";
        }
        console.warn(LOG, "session ended: held", heldS.toFixed(2) + "s, no speech was captured --", why);
      }
      sessionStartedAt = 0;
    }
    // Pause rather than destroy: keeps the mic stream warm for the next hold
    // within this session (see prewarmMic/releaseMic). This does mean the
    // browser's mic-in-use indicator can stay lit between holds, not just
    // during one -- releaseMic() is what actually turns it off.
    if (myvad) {
      const toWarm = myvad;
      myvad = null;
      try { toWarm.pause(); warmVad = toWarm; } catch (_) { try { toWarm.destroy(); } catch (_) {} }
    }
    emitLevel(0);
  }

  const LocalVocal = {
    init,
    whenReady,
    listen,
    prewarmMic,
    releaseMic,
    encodeWav,
    get status() { return status; },
    get statusDetail() { return statusDetail; },
    // The most recent pipeline error ({message, name, time, stack} or null),
    // for a dev console to inspect after the fact -- see the `lastError`
    // declaration above for why this exists.
    get lastError() { return lastError; },
  };

  if (typeof module !== "undefined" && module.exports) module.exports = LocalVocal;
  else root.LocalVocal = LocalVocal;
})(typeof self !== "undefined" ? self : this);
