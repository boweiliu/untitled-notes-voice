// Adopted from Local Vocal (https://github.com/DanverImbue/local-vocal), an on-device voice
// inspiration by Danver Braganza, via the Red Scribble copy (boweiliu/red-scribble). Design rationale:
// that repo's red_scribble/reference/voice-transcription.md.

// The voice-capture model: ONE streaming mechanism powers both the live caption
// and the saved transcript, with NO knowledge of the DOM, the ASR worker, or the
// network.
//
// Mechanism ("silence-anchored commit-and-slice"): a StreamingTurn holds, for
// one VAD speech segment,
//   - the committed transcript words (grow without bound), and
//   - a rolling active-audio window (bounded to ~20s of samples).
// The driver periodically transcribes the whole active window; LocalAgreement-2
// over consecutive hypotheses styles the caption's tail (stable vs tentative).
// Once the window exceeds the cap, the turn slices off a head of audio at a
// genuine pause (a contiguous low-energy run of >= ~200ms -- a real inter-word
// gap, never a stop-consonant closure), hands that head out to be transcribed,
// and folds the resulting text into the committed words. Because the cut lands
// in silence, no word is split; a word-level seam de-dup backstop protects the
// rare forced cut (no pause found by the ~30s hard cap). At end of turn the
// remaining window drains the same way, so the saved turn is simply
// `committedText` once the last chunk folds in. The live caption is always
// `committed + current window hypothesis` -- the same text the save will read,
// just read earlier.
//
// Moonshine (via transformers.js) returns only {text} -- no word timestamps --
// which is why the slice points come from audio energy, not token times.
//
// Loads two ways: as a browser global (window.VoiceSession) via a <script> tag,
// and as a CommonJS module (module.exports) so node --test can exercise it.
(function (root) {
  "use strict";

  // ---- word/text normalisation (case- and punctuation-insensitive) ----
  const _PUNCT = /[.,!?;:"'\u2019]/g;
  function normWord(w) { return w.replace(_PUNCT, "").toLowerCase(); }
  function normText(s) { return (s || "").replace(_PUNCT, "").toLowerCase().replace(/\s+/g, " ").trim(); }

  // ---- collapse a runaway ASR repetition loop to a single copy ----
  // A mirror of the server-side collapse_runaway_repetition (transcription.py):
  // the client uses it for a clean live/committed caption; the server remains
  // the durable authority. Shortest repeating phrase wins (the loop's true
  // period); single words need a higher bar so natural emphasis ("no no no")
  // survives.
  const MAX_PHRASE_WORDS = 30, MIN_REPEATS_MULTIWORD = 3, MIN_REPEATS_SINGLE_WORD = 4;
  function collapseRepeats(text) {
    const wordsArr = (text || "").split(/\s+/).filter(Boolean);
    const n = wordsArr.length;
    if (n < 2) return (text || "").trim();
    const normed = wordsArr.map(normWord);
    const out = [];
    let i = 0;
    while (i < n) {
      let collapsed = false;
      const maxLen = Math.min(MAX_PHRASE_WORDS, Math.floor((n - i) / 2));
      for (let length = 1; length <= maxLen; length++) {
        let reps = 1, j = i + length;
        while (j + length <= n && _rangeEq(normed, i, j, length)) { reps++; j += length; }
        const threshold = length === 1 ? MIN_REPEATS_SINGLE_WORD : MIN_REPEATS_MULTIWORD;
        if (reps >= threshold) {
          for (let k = i; k < i + length; k++) out.push(wordsArr[k]);  // keep one copy, verbatim
          i = j; collapsed = true; break;
        }
      }
      if (!collapsed) { out.push(wordsArr[i]); i++; }
    }
    return out.join(" ");
  }
  function _rangeEq(arr, a, b, length) {
    for (let k = 0; k < length; k++) if (arr[a + k] !== arr[b + k]) return false;
    return true;
  }

  // ---- ASR text hygiene ----
  // Moonshine emits bracketed annotations on non-speech audio ("[BLANK_AUDIO]",
  // "(coughs)"). Strip them; if nothing pronounceable remains, the text is junk
  // (returned as ""). Any runaway decoder loop is collapsed to one copy -- the
  // server repeats that collapse on the durable record as the safety net.
  function cleanAsrText(text) {
    const stripped = (text || "").replace(/\[[^\]]*\]|\([^)]*\)/g, " ");
    if (!/[a-z0-9]/i.test(stripped)) return "";
    return collapseRepeats(stripped.replace(/\s+/g, " ").trim());
  }

  // ---- word-level seam de-dup ----
  // When a transcribed chunk folds onto the committed words, drop its leading
  // words if they repeat the committed trailing words (normalised comparison).
  // On a pause-anchored cut the chunks share no audio, so overlap is rare; the
  // backstop exists for the forced (no-pause) cut, where the ASR can hear the
  // boundary word in both chunks. Capped so a genuine long repetition by the
  // author is never eaten.
  const MAX_SEAM_OVERLAP_WORDS = 6;
  function dropSeamOverlap(committedWords, newWords) {
    const maxK = Math.min(MAX_SEAM_OVERLAP_WORDS, committedWords.length, newWords.length);
    for (let k = maxK; k >= 1; k--) {
      let match = true;
      for (let i = 0; i < k; i++) {
        if (normWord(committedWords[committedWords.length - k + i]) !== normWord(newWords[i])) { match = false; break; }
      }
      if (match) return newWords.slice(k);
    }
    return newWords;
  }

  // ---- frame energy (shared with the driver's level meter) ----
  function frameRms(frame) {
    let s = 0;
    for (let i = 0; i < frame.length; i++) s += frame[i] * frame[i];
    return Math.sqrt(s / Math.max(1, frame.length));
  }

  function _concatF32(chunks) {
    let n = 0; for (const c of chunks) n += c.length;
    const out = new Float32Array(n);
    let o = 0; for (const c of chunks) { out.set(c, o); o += c.length; }
    return out;
  }

  const STREAMING_TURN_DEFAULTS = {
    sampleRate: 16000,
    // The model-input cap: once the window exceeds this, look for a pause to
    // slice at. Moonshine is a short-audio model; past ~20s its decoder loops.
    maxWindowSeconds: 20,
    // Waiting for a genuine pause may let the window grow past the cap; at this
    // absolute bound a cut is forced (quietest frame) rather than waiting longer.
    hardCapSeconds: 30,
    // A qualifying pause is a contiguous quiet run at least this long -- a real
    // inter-word/phrase gap. Shorter dips (a stop-consonant closure is ~50-100ms)
    // can sit INSIDE a word, and cutting there would split it.
    minPauseMs: 200,
    // Never slice off a sliver: a head must be at least this long.
    minHeadSeconds: 1,
    // The forced cut hunts for the quietest single frame this far back from the cap.
    forcedCutSearchSeconds: 4,
    // A frame is "quiet" when its RMS is under both an absolute floor and a
    // fraction of the loudest frame heard this turn (mic gains vary widely).
    quietAbsoluteRms: 0.006,
    quietPeakFraction: 0.08,
    // When set, a quiet run at least this long commits the speech before it right
    // away, even under the window cap. Moonshine tends to drop a short phrase that
    // a long silence follows within one window ("ask not ... what your country"),
    // so dictation hands each phrase over at its pause. 0 keeps the original
    // cap-only slicing.
    pauseCommitMs: 0,
  };

  // The per-turn streaming transcriber state. Pure and synchronous: the driver
  // owns the ASR worker and feeds results back in. Contract with the driver:
  //   - addFrame(frame) for every VAD frame of the segment;
  //   - takeHeadSlice() after feeding: non-null means "transcribe this audio and
  //     pass the text to commitText()" (results must fold in slice order --
  //     a single FIFO worker gives that for free);
  //   - windowAudio() -> {audio, epoch} for periodic live-caption transcription,
  //     the text fed to applyHypothesis(text, epoch) (stale epochs are ignored);
  //   - at end of turn, drainChunks() empties the window; commit each chunk's
  //     text, then committedText IS the turn's transcript.
  class StreamingTurn {
    constructor(options) {
      const o = Object.assign({}, STREAMING_TURN_DEFAULTS, options || {});
      this._maxWindow = Math.round(o.maxWindowSeconds * o.sampleRate);
      this._hardCap = Math.round(o.hardCapSeconds * o.sampleRate);
      this._minPauseSamples = Math.round((o.minPauseMs / 1000) * o.sampleRate);
      this._pauseCommitSamples = Math.round((o.pauseCommitMs / 1000) * o.sampleRate);
      this._minHead = Math.round(o.minHeadSeconds * o.sampleRate);
      this._forcedSearch = Math.round(o.forcedCutSearchSeconds * o.sampleRate);
      this._quietAbs = o.quietAbsoluteRms;
      this._quietFrac = o.quietPeakFraction;
      this.reset();
    }

    reset() {
      this._committed = [];   // words committed this turn; grows without bound
      this._frames = [];      // the active window's audio frames
      this._frameRms = [];    // per-frame RMS, parallel to _frames
      this._samples = 0;
      this._peakRms = 0;
      this._epoch = 0;        // bumped on every slice/drain; stale hypotheses are ignored
      this._hyp = [];         // latest window hypothesis (words)
      this._agreed = 0;       // LocalAgreement-2: prefix agreed by the last two hypotheses
      this._prevHyp = [];
    }

    // Feed one frame of turn audio (Float32Array of samples at sampleRate).
    // The frame is retained; pass a copy if the caller reuses its buffer.
    addFrame(frame) {
      const rms = frameRms(frame);
      this._frames.push(frame);
      this._frameRms.push(rms);
      this._samples += frame.length;
      if (rms > this._peakRms) this._peakRms = rms;
    }

    get bufferedSamples() { return this._samples; }
    get epoch() { return this._epoch; }
    get committedText() { return this._committed.join(" "); }
    get committedWordCount() { return this._committed.length; }

    // A copy of the current active window, tagged with the epoch that must
    // accompany the resulting hypothesis (a slice in between invalidates it).
    windowAudio() { return { audio: _concatF32(this._frames), epoch: this._epoch }; }

    // Fold one live hypothesis over the current window into the caption state.
    // Never touches the committed words -- those come only from commitText().
    applyHypothesis(text, epoch) {
      if (epoch !== this._epoch) return;
      const hyp = cleanAsrText(text).split(" ").filter(Boolean);
      let i = 0;
      const m = Math.min(this._prevHyp.length, hyp.length);
      while (i < m && normWord(hyp[i]) === normWord(this._prevHyp[i])) i++;
      this._agreed = i;
      this._prevHyp = hyp;
      this._hyp = hyp;
    }

    // The live caption, at any moment and any turn length:
    // stable = all committed text + the window hypothesis' agreed prefix;
    // tentative = the rest of the latest window hypothesis.
    liveParts() {
      return {
        stable: this._committed.concat(this._hyp.slice(0, this._agreed)).join(" "),
        tentative: this._hyp.slice(this._agreed).join(" "),
      };
    }
    get liveText() {
      const p = this.liveParts();
      return (p.stable + " " + p.tentative).trim();
    }

    // Fold a transcribed audio chunk (a head slice or a drained chunk) into the
    // committed words, junk-filtered, de-looped, and seam-de-duped.
    commitText(rawText) {
      const text = cleanAsrText(rawText);
      if (!text) return;
      const words = dropSeamOverlap(this._committed, text.split(" "));
      for (const w of words) this._committed.push(w);
    }

    // If the window is over the cap and a safe cut exists, slice off the head:
    // remove that audio from the window and return it for transcribe-and-commit.
    // Returns null while no cut is due -- or, past the cap but under the hard
    // cap, while no qualifying pause exists yet (never cut inside a word).
    takeHeadSlice() {
      if (this._samples <= this._maxWindow) {
        if (!this._pauseCommitSamples) return null;
        return this._sliceAt(this._findPauseCutFrame(this._pauseCommitSamples, true));
      }
      let cutFrame = this._findPauseCutFrame();
      if (cutFrame === null) {
        if (this._samples < this._hardCap) return null;   // keep waiting for a real pause
        cutFrame = this._forcedCutFrame();
      }
      return this._sliceAt(cutFrame);
    }

    // Empty the window as a sequence of transcribe-and-commit chunks (each cut
    // pause-anchored where possible, every chunk <= the cap except a degenerate
    // single over-long frame). Call at end of turn; commit each chunk's text in
    // order and committedText is then the turn's full transcript.
    drainChunks() {
      const chunks = [];
      while (this._samples > this._maxWindow) {
        let cutFrame = this._findPauseCutFrame();
        if (cutFrame === null) cutFrame = this._forcedCutFrame();
        const head = this._sliceAt(cutFrame);
        if (!head) break;   // cannot cut (single giant frame): take it all below
        chunks.push(head);
      }
      if (this._samples > 0) {
        chunks.push(_concatF32(this._frames));
        this._dropWindow(this._frames.length);
      }
      return chunks;
    }

    // ---- internals ----

    _quietThreshold() { return Math.max(this._quietAbs, this._quietFrac * this._peakRms); }

    // Frame index of the sample offset of each frame's start.
    _frameOffsets() {
      const offsets = new Array(this._frames.length + 1);
      let o = 0;
      for (let i = 0; i < this._frames.length; i++) { offsets[i] = o; o += this._frames[i].length; }
      offsets[this._frames.length] = o;
      return offsets;
    }

    // The LATEST genuine pause whose cut point fits in [minHead, maxWindow]:
    // a contiguous run of quiet frames spanning >= minPause. Cut at the run's
    // middle (silence on both sides -- between words by construction), walked
    // back inside the run if the middle overshoots the cap. `afterSpeech` skips a
    // run at the window's start, so a head is never silence alone.
    _findPauseCutFrame(minPauseSamples = this._minPauseSamples, afterSpeech = false) {
      const threshold = this._quietThreshold();
      const offsets = this._frameOffsets();
      const n = this._frames.length;
      let best = null;
      let runStart = -1;
      for (let i = 0; i <= n; i++) {
        const quiet = i < n && this._frameRms[i] <= threshold;
        if (quiet) { if (runStart < 0) runStart = i; continue; }
        if (runStart >= 0) {
          const runEnd = i;   // exclusive
          if (offsets[runEnd] - offsets[runStart] >= minPauseSamples && !(afterSpeech && runStart === 0)) {
            let cut = runStart + Math.floor((runEnd - runStart) / 2);
            while (cut > runStart && offsets[cut] > this._maxWindow) cut--;
            const at = offsets[cut];
            if (at >= this._minHead && at <= this._maxWindow) best = cut;   // latest qualifying run wins
          }
          runStart = -1;
        }
      }
      return best;
    }

    // No pause by the hard cap: cut at the quietest single frame near the cap
    // (best effort -- the seam de-dup in commitText backstops this cut).
    _forcedCutFrame() {
      const offsets = this._frameOffsets();
      const from = Math.max(this._minHead, this._maxWindow - this._forcedSearch);
      let cut = null, best = Infinity;
      let lastInCap = null;
      for (let i = 0; i < this._frames.length; i++) {
        const at = offsets[i];
        if (at > this._maxWindow) break;
        if (at >= this._minHead) lastInCap = i;
        if (at >= from && this._frameRms[i] < best) { best = this._frameRms[i]; cut = i; }
      }
      return cut !== null ? cut : lastInCap;
    }

    _sliceAt(cutFrame) {
      if (cutFrame === null || cutFrame <= 0) return null;
      const head = _concatF32(this._frames.slice(0, cutFrame));
      this._dropWindow(cutFrame);
      return head;
    }

    // Drop the first `count` frames and invalidate the window hypothesis (it
    // described audio that is no longer the window).
    _dropWindow(count) {
      this._frames = this._frames.slice(count);
      this._frameRms = this._frameRms.slice(count);
      this._samples = 0;
      for (const f of this._frames) this._samples += f.length;
      this._epoch++;
      this._hyp = []; this._prevHyp = []; this._agreed = 0;
    }
  }

  // ---- the running conversation ----
  // The finalized author turns of one recording. This is the client-side source
  // of truth for what was said -- held here, in the model, never read back out of
  // the DOM. A turn is *born* here when it finalizes; the on-screen paragraph and
  // the server post are both just renderings of it. So the display filling,
  // scrolling, or erroring cannot affect what is stored.
  class Transcript {
    constructor() { this.reset(); }
    reset() { this._turns = []; }
    // Record a finalized turn; returns the trimmed text actually stored ("" if empty).
    record(text) {
      const t = (text || "").trim();
      if (t) this._turns.push(t);
      return t;
    }
    get turns() { return this._turns.slice(); }
    get text() { return this._turns.join("\n\n"); }
    get length() { return this._turns.length; }
  }

  const api = {
    StreamingTurn, Transcript,
    collapseRepeats, cleanAsrText, dropSeamOverlap, frameRms, normWord, normText,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.VoiceSession = api;
})(typeof self !== "undefined" ? self : this);
