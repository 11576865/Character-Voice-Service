import assert from "node:assert/strict";
import { test } from "node:test";
import { VariantStore } from "../web/js/variants.js";

test("automatic reuse honors voice settings, explicit audition can replay historical audio", async () => {
  const store = new VariantStore();
  const settings = { voice: "a", modelId: "m1", referenceId: "r1", speed: 1 };
  const audio = new Blob(["audio"]);
  await store.add("paragraph", [audio], settings);
  assert.equal(await store.selectedClip("paragraph", 0, 1, settings), null);
  assert.equal(await store.selectedClip("paragraph", 0, 1), audio);
  await store.add("paragraph", [audio], {...settings, settingsVersion: 1});
  assert.equal(await store.selectedClip("paragraph", 0, 1, settings), audio);
  for (const changed of [{voice: "b"}, {modelId: "m2"}, {referenceId: "r2"}, {speed: 1.2}]) {
    assert.equal(await store.selectedClip("paragraph", 0, 1, {...settings, ...changed}), null);
  }
  assert.equal(await store.selectedClip("paragraph", 0, 1), audio);
  assert.equal(await store.selectedClip("paragraph", 0, 2, settings), null);
});
