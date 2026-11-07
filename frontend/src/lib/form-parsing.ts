export function cleanOptionalText(value: FormDataEntryValue | null) {
  const text = String(value || "").trim();
  return text || null;
}

export function splitTextareaValues(value: FormDataEntryValue | null) {
  return Array.from(
    new Set(
      String(value || "")
        .split(/[\n,;]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

export function numberOrNull(value: FormDataEntryValue | null) {
  const text = String(value || "").trim();
  if (!text) return null;
  const number = Number(text);
  return Number.isFinite(number) ? number : null;
}

export function parseReactionWeights(value: FormDataEntryValue | null) {
  const text = String(value || "").trim();
  if (!text) return {};
  const weights: Record<string, number> = {};
  text
    .split(/[\n,;]+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .forEach((line) => {
      const [reactionRaw, weightRaw] = line.split(/[=:]/, 2);
      const reaction = String(reactionRaw || "").trim();
      const weight = Number(String(weightRaw || "").trim());
      if (reaction && Number.isFinite(weight) && weight > 0) {
        weights[reaction] = Math.round(weight);
      }
    });
  return weights;
}

export function reactionWeightsText(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  return Object.entries(value as Record<string, unknown>)
    .map(([reaction, weight]) => `${reaction}=${weight}`)
    .join("\n");
}
