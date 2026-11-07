import assert from "node:assert/strict";
import test from "node:test";
import {
  campaignErrorText,
  campaignEventLabel,
  campaignEventMessage,
  campaignSkipReasonLabel,
  isCampaignStoppable,
  statusLabel,
} from "../src/lib/campaign-labels.ts";

test("labels known states and preserves unknown values", () => {
  assert.equal(statusLabel("running"), "Запущен");
  assert.equal(statusLabel("custom"), "custom");
  assert.equal(campaignEventLabel("run_failed"), "Ошибка запуска");
  assert.equal(campaignEventLabel("custom"), "custom");
  assert.equal(campaignSkipReasonLabel("duplicate"), "ответ уже был создан");
  assert.equal(campaignEventMessage("unrecognized"), "unrecognized");
});

test("classifies stoppable campaigns and describes errors", () => {
  assert.equal(isCampaignStoppable("pending"), true);
  assert.equal(isCampaignStoppable("running"), true);
  assert.equal(isCampaignStoppable("completed"), false);
  assert.match(campaignErrorText(null, "COMMENTS_CLOSED"), /обсуждения/);
  assert.equal(campaignErrorText("  detail  ", "E01"), "E01: detail");
});
