import assert from "node:assert/strict";
import { test } from "node:test";
import { ProgressStore, progressSyncDecision } from "../web/js/progress.js";

const local = { segmentIndex: 4, audioTime: 2, updatedAt: "2026-09-26T01:00:00Z" };
const remote = { segmentIndex: 9, audioTime: 0, updatedAt: "2026-09-26T02:00:00Z" };

test("reading position sync resolves one-sided edits and asks on conflicts", () => {
  assert.equal(progressSyncDecision(local, null), "device");
  assert.equal(progressSyncDecision(null, remote), "computer");
  assert.equal(progressSyncDecision(local, remote, "old", remote.updatedAt), "device");
  assert.equal(progressSyncDecision(local, remote, local.updatedAt, "old"), "computer");
  assert.equal(progressSyncDecision(local, remote, "old", "old"), "choose");
  assert.equal(progressSyncDecision(local, remote, local.updatedAt, remote.updatedAt), "choose");
  assert.equal(progressSyncDecision(local, { ...remote, segmentIndex: 4, audioTime: 2.5 }), "none");
});

test("server position retains its timestamp when saved locally", () => {
  const entries = new Map();
  const storage = { setItem: (key, value) => entries.set(key, value),
    getItem: key => entries.get(key) ?? null, removeItem: key => entries.delete(key) };
  const store = new ProgressStore(storage);
  const document = { title: "Demo" };
  const segments = Array.from({ length: 10 }, (_, index) => ({ chapterIndex: 0, index }));
  store.save({ documentId: "book:demo", title: document.title, chapterIndex: 0,
    segmentIndex: remote.segmentIndex, audioTime: remote.audioTime, updatedAt: remote.updatedAt });
  assert.equal(store.load("book:demo", document, segments).updatedAt, remote.updatedAt);
});
