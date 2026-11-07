import assert from "node:assert/strict";
import test from "node:test";

import {
  clampNumber,
  dialogKindRu,
  formatDateTimeRu,
  formatRestrictionUntilMoscow,
  splitRefs,
} from "../src/lib/display.ts";

test("clamps numeric input and falls back for invalid values", () => {
  assert.equal(clampNumber("7.8", 1, 10, 2), 7);
  assert.equal(clampNumber("20", 1, 10, 2), 10);
  assert.equal(clampNumber("invalid", 1, 10, 2), 2);
});

test("splits and deduplicates source references", () => {
  assert.deepEqual(splitRefs("@a, @b\n@a"), ["@a", "@b"]);
  assert.deepEqual(splitRefs("  "), []);
});

test("formats invalid dates and Moscow deadlines safely", () => {
  assert.equal(formatDateTimeRu("bad date"), "нет");
  assert.equal(formatRestrictionUntilMoscow(null), null);
  assert.deepEqual(formatRestrictionUntilMoscow("2026-01-01T00:00:00Z"), {
    date: "01.01.26",
    time: "03:00 МСК",
  });
  assert.equal(dialogKindRu("channel"), "канал");
  assert.equal(dialogKindRu("other"), "other");
});
