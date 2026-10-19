// Unit tests for the streaming voice-capture model (run with `node --test`).
//
// The StreamingTurn tests drive the model exactly the way the page does --
// frames in, head slices out to a (fake) ASR, chunk texts folded back -- with
// synthetic audio whose "words" are encoded in the samples: word i is a run of
// constant amplitude 0.1 + i/1000, and silence is zeros. The fake ASR decodes a
// chunk by reading those runs, so text<->audio correspondence is exact and the
// no-loss / no-duplication / no-split invariants are provable.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const {
  StreamingTurn, Transcript,
  collapseRepeats, cleanAsrText, dropSeamOverlap, frameRms,
} = require("./voice_session.js");

// ---- synthetic audio at a shrunken scale (1s window instead of 20s) ----
const SR = 16000;
const FRAME = 320;                    // 20ms frames, like the real VAD's
const OPTS = {
  sampleRate: SR,
  maxWindowSeconds: 1,                // window cap: 16000 samples
  hardCapSeconds: 1.5,                // forced-cut bound: 24000 samples
  minPauseMs: 200,                    // 10 frames of contiguous silence qualify
  minHeadSeconds: 0.1,
  forcedCutSearchSeconds: 0.4,
};
const MAX_WINDOW_SAMPLES = SR * OPTS.maxWindowSeconds;
const HARD_CAP_SAMPLES = SR * OPTS.hardCapSeconds;
const WORD_FRAMES = 15;               // 300ms of speech per word
const WORD_SAMPLES = WORD_FRAMES * FRAME;

function amp(i) { return 0.1 + i * 0.001; }
// Frames for `count` words starting at index `from`, with `gapFrames` of
// silence after each word.
function wordStream(from, count, gapFrames) {
  const frames = [];
  for (let w = from; w < from + count; w++) {
    for (let f = 0; f < WORD_FRAMES; f++) frames.push(new Float32Array(FRAME).fill(amp(w)));
    for (let g = 0; g < gapFrames; g++) frames.push(new Float32Array(FRAME));
  }
  return frames;
}
function expectedWords(from, count) {
  return Array.from({ length: count }, (_, k) => "w" + (from + k)).join(" ");
}

// The constant-amplitude runs of a chunk (its "words" and their integrity).
function nonzeroRuns(audio) {
  const runs = [];
  let i = 0;
  while (i < audio.length) {
    if (audio[i] === 0) { i++; continue; }
    const v = audio[i];
    let j = i;
    while (j < audio.length && audio[j] === v) j++;
    runs.push({ value: v, length: j - i });
    i = j;
  }
  return runs;
}
// The fake ASR: a run of at least half a word's samples reads as that word
// (like a real model, it can "hear" a word from either side of a mid-word cut).
function fakeAsr(audio) {
  return nonzeroRuns(audio)
    .filter((r) => r.length >= WORD_SAMPLES / 2)
    .map((r) => "w" + Math.round((r.value - 0.1) / 0.001))
    .join(" ");
}

// Drive a whole turn the way the page does: feed frames, transcribe-and-commit
// every head slice as it comes off, then drain and commit the rest. `mangle`
// lets a test distort each chunk's text (e.g. simulate boundary-word echo).
function driveTurn(turn, frames, { mangle } = {}) {
  const chunks = [];
  const fold = (audio) => {
    let text = fakeAsr(audio);
    if (mangle) text = mangle(text, chunks.length);
    chunks.push({ audio, text });
    turn.commitText(text);
  };
  for (const frame of frames) {
    turn.addFrame(frame);
    for (let head; (head = turn.takeHeadSlice()); ) fold(head);
  }
  for (const chunk of turn.drainChunks()) fold(chunk);
  return chunks;
}

// ---- pause commit (dictation): hand each phrase over at a long pause ----

function silence(frames) { return Array.from({ length: frames }, () => new Float32Array(FRAME)); }
const PAUSE_OPTS = { ...OPTS, maxWindowSeconds: 20, hardCapSeconds: 30, pauseCommitMs: 600 };

test("pause commit slices a phrase off at a long pause, under the window cap", () => {
  const turn = new StreamingTurn(PAUSE_OPTS);
  const heads = [];
  for (const frame of [...wordStream(0, 2, 3), ...silence(40), ...wordStream(2, 2, 3)]) {
    turn.addFrame(frame);
    for (let head; (head = turn.takeHeadSlice()); ) { heads.push(head); turn.commitText(fakeAsr(head)); }
  }
  assert.equal(heads.length, 1, "one phrase handed over at the 800ms pause");
  assert.equal(fakeAsr(heads[0]), "w0 w1");
  for (const chunk of turn.drainChunks()) turn.commitText(fakeAsr(chunk));
  assert.equal(turn.committedText, "w0 w1 w2 w3");
});

test("pause commit ignores pauses shorter than its threshold and leading silence", () => {
  const turn = new StreamingTurn(PAUSE_OPTS);
  const frames = [...silence(60), ...wordStream(0, 3, 20)];   // 1.2s lead-in, then 400ms gaps
  for (const frame of frames) {
    turn.addFrame(frame);
    assert.equal(turn.takeHeadSlice(), null);
  }
});

test("without pause commit, a long pause under the cap stays in one window", () => {
  const turn = new StreamingTurn({ ...PAUSE_OPTS, pauseCommitMs: 0 });
  for (const frame of [...wordStream(0, 2, 3), ...silence(40), ...wordStream(2, 2, 3)]) {
    turn.addFrame(frame);
    assert.equal(turn.takeHeadSlice(), null);
  }
});

// ---- the single-mechanism invariants ----

test("committed text accumulates without bound across many window slides, losing and duplicating nothing", () => {
  const turn = new StreamingTurn(OPTS);
  const chunks = driveTurn(turn, wordStream(0, 60, 12));   // ~32s of audio at this scale
  assert.ok(chunks.length > 10, "expected many window slides, got " + chunks.length);
  assert.equal(turn.committedText, expectedWords(0, 60));
  assert.equal(turn.committedWordCount, 60);
});

test("every chunk handed to the ASR stays within the window cap when pauses exist", () => {
  const turn = new StreamingTurn(OPTS);
  const chunks = driveTurn(turn, wordStream(0, 40, 12));
  for (const { audio } of chunks) assert.ok(audio.length <= MAX_WINDOW_SAMPLES);
});

test("cuts land in real pauses: no word is ever split across chunks", () => {
  const turn = new StreamingTurn(OPTS);
  const chunks = driveTurn(turn, wordStream(0, 40, 12));
  for (const { audio } of chunks) {
    for (const run of nonzeroRuns(audio)) {
      assert.equal(run.length, WORD_SAMPLES, "a word was split by a cut");
    }
  }
});

test("a short dip (stop-consonant closure) is not a pause: no cut until the hard cap", () => {
  // Gaps of 140ms are below the 200ms minimum-pause bar, so the model must
  // refuse to slice past the cap and wait for the hard cap instead.
  const turn = new StreamingTurn(OPTS);
  let firstSliceAt = null;
  for (const frame of wordStream(0, 40, 7)) {
    turn.addFrame(frame);
    const before = turn.bufferedSamples;
    let head = turn.takeHeadSlice();
    if (head && firstSliceAt === null) firstSliceAt = before;
    while (head) { turn.commitText(fakeAsr(head)); head = turn.takeHeadSlice(); }
  }
  assert.ok(firstSliceAt !== null, "expected a forced cut eventually");
  assert.ok(firstSliceAt >= HARD_CAP_SAMPLES, "sliced at " + firstSliceAt + " before the hard cap");
  for (const chunk of turn.drainChunks()) turn.commitText(fakeAsr(chunk));
  assert.equal(turn.committedText, expectedWords(0, 40));
});

test("continuous speech with no pause at all: forced cuts still lose nothing", () => {
  const turn = new StreamingTurn(OPTS);
  const chunks = driveTurn(turn, wordStream(0, 60, 0));
  assert.ok(chunks.length > 5);
  assert.equal(turn.committedText, expectedWords(0, 60));
});

test("seam de-dup removes a boundary word the ASR hears on both sides of a forced cut", () => {
  // Simulate the real forced-cut hazard: the chunk after a cut re-hears the
  // previous chunk's last word (context bleed). The fold must drop the echo.
  const turn = new StreamingTurn(OPTS);
  let lastWord = "";
  driveTurn(turn, wordStream(0, 60, 0), {
    mangle: (text) => {
      const echoed = lastWord && text ? lastWord + " " + text : text;
      const words = text.split(" ").filter(Boolean);
      if (words.length) lastWord = words[words.length - 1];
      return echoed;
    },
  });
  assert.equal(turn.committedText, expectedWords(0, 60));
});

test("the live caption is committed + active tail and keeps advancing past the window cap", () => {
  const turn = new StreamingTurn(OPTS);
  const frames = wordStream(0, 20, 12);   // ~10.8s of audio against a 1s window
  let framesSeen = 0;
  for (const frame of frames) {
    turn.addFrame(frame);
    for (let head; (head = turn.takeHeadSlice()); ) turn.commitText(fakeAsr(head));
    framesSeen++;
    if (framesSeen % 3 === 0) {
      const w = turn.windowAudio();
      turn.applyHypothesis(fakeAsr(w.audio), w.epoch);
    }
  }
  const w = turn.windowAudio();
  turn.applyHypothesis(fakeAsr(w.audio), w.epoch);
  // Minutes-of-history behaviour at test scale: the caption holds every word
  // spoken, far more than one window (1s ~ at most 2 words) could ever show.
  assert.equal(turn.liveText, expectedWords(0, 20));
  assert.ok(turn.committedWordCount > 2, "the committed history must extend past one window");
  const parts = turn.liveParts();
  assert.ok(parts.stable.startsWith("w0 "), "committed history leads the caption");
});

test("a hypothesis from before a slice is stale and is ignored", () => {
  const turn = new StreamingTurn(OPTS);
  const before = turn.windowAudio();
  driveTurn(turn, wordStream(0, 10, 12));   // slices happen, epoch advances
  const said = turn.committedText;
  turn.applyHypothesis("w999", before.epoch);
  assert.equal(turn.liveText, said);        // the stale hypothesis changed nothing
});

test("hypotheses style the caption but never touch the committed transcript", () => {
  const turn = new StreamingTurn(OPTS);
  turn.applyHypothesis("hello there", turn.epoch);
  turn.applyHypothesis("hello there friend", turn.epoch);
  assert.equal(turn.committedText, "");
  const parts = turn.liveParts();
  assert.equal(parts.stable, "hello there");      // agreed across two hypotheses
  assert.equal(parts.tentative, "friend");        // still tentative
});

test("the saved turn is simply the model's committed text when the turn ends", () => {
  const turn = new StreamingTurn(OPTS);
  driveTurn(turn, wordStream(0, 30, 12));
  const saved = turn.committedText;
  assert.equal(saved, expectedWords(0, 30));
  const transcript = new Transcript();
  assert.equal(transcript.record(saved), saved);
  assert.equal(transcript.text, saved);
});

test("an all-silence turn commits nothing and saves nothing", () => {
  const turn = new StreamingTurn(OPTS);
  for (let i = 0; i < 20; i++) turn.addFrame(new Float32Array(FRAME));
  for (const chunk of turn.drainChunks()) turn.commitText(fakeAsr(chunk));
  turn.commitText("[BLANK_AUDIO]");
  assert.equal(turn.committedText, "");
});

// ---- the fold's text hygiene ----

test("commitText folds cleaned text and de-dups the seam (case/punctuation-insensitively)", () => {
  const turn = new StreamingTurn(OPTS);
  turn.commitText("Alpha beta Gamma.");
  turn.commitText("gamma delta");
  assert.equal(turn.committedText, "Alpha beta Gamma. delta");
});

test("commitText collapses a runaway decoder loop within a chunk", () => {
  const turn = new StreamingTurn(OPTS);
  const unit = "so this would be a circle";
  turn.commitText(Array(6).fill(unit).join(" "));
  assert.equal(turn.committedText, unit);
});

test("cleanAsrText strips annotations and rejects junk", () => {
  assert.equal(cleanAsrText("[BLANK_AUDIO]"), "");
  assert.equal(cleanAsrText("(coughs)  ..."), "");
  assert.equal(cleanAsrText(" well [noise] then "), "well then");
});

test("dropSeamOverlap drops only a genuine repeated seam", () => {
  assert.deepEqual(dropSeamOverlap(["a", "b", "c"], ["b", "c", "d"]), ["d"]);
  assert.deepEqual(dropSeamOverlap(["a", "b"], ["c", "d"]), ["c", "d"]);
  assert.deepEqual(dropSeamOverlap([], ["x"]), ["x"]);
});

test("frameRms measures a frame's level", () => {
  assert.equal(frameRms(new Float32Array(4)), 0);
  assert.ok(Math.abs(frameRms(new Float32Array(4).fill(0.5)) - 0.5) < 1e-6);
});

// ---- carried-forward behaviour (characterization of what the cut kept) ----

test("collapseRepeats reduces a runaway loop but keeps natural repetition", () => {
  const unit = "So this would be a circle and drag on the screen.";
  assert.equal(collapseRepeats(Array(7).fill(unit).join(" ")), unit);
  assert.equal(collapseRepeats("no no no"), "no no no");
});

test("Transcript accumulates finalized turns as the client source of truth", () => {
  const t = new Transcript();
  assert.equal(t.length, 0);
  assert.equal(t.record("  first point  "), "first point");   // returns the stored (trimmed) text
  t.record("second point");
  t.record("");                                                // empty turns are not stored
  assert.equal(t.length, 2);
  assert.deepEqual(t.turns, ["first point", "second point"]);
  assert.equal(t.text, "first point\n\nsecond point");
  t.reset();
  assert.equal(t.length, 0);
  assert.equal(t.text, "");
});
