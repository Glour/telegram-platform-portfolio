import assert from "node:assert/strict";
import test from "node:test";
import {
  cleanOptionalText,
  numberOrNull,
  parseReactionWeights,
  reactionWeightsText,
  splitTextareaValues,
} from "../src/lib/form-parsing.ts";

test("normalizes optional values and unique textarea entries", () => {
  assert.equal(cleanOptionalText("  sample  "), "sample");
  assert.equal(cleanOptionalText("  "), null);
  assert.deepEqual(splitTextareaValues("one; two, one\nthree"), [
    "one",
    "two",
    "three",
  ]);
  assert.equal(numberOrNull("  "), null);
  assert.equal(numberOrNull("not a number"), null);
  assert.equal(numberOrNull("-2.5"), -2.5);
});

test("parses and renders reaction weights", () => {
  const weights = parseReactionWeights("👍=3; ❤️:2\n🔥=-1\ninvalid");
  assert.deepEqual(weights, { "👍": 3, "❤️": 2 });
  assert.equal(reactionWeightsText(weights), "👍=3\n❤️=2");
  assert.equal(reactionWeightsText(null), "");
  assert.equal(reactionWeightsText([]), "");
});
