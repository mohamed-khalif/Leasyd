// LLM list prices, to estimate an agent's cost from its tokens when the spans don't report one
// (gen_ai.usage.cost). USD per million tokens, input / output, public list prices as of 2026-10;
// a model is matched by its longest known prefix ("gpt-4o-mini-2024-07-18" -> "gpt-4o-mini").
// Nothing is stored with these, so correcting a price corrects history too.
const PRICES: [prefix: string, input: number, output: number][] = [
  ["gpt-5-nano", 0.05, 0.4], ["gpt-5-mini", 0.25, 2], ["gpt-5", 1.25, 10],
  ["gpt-4.1-nano", 0.1, 0.4], ["gpt-4.1-mini", 0.4, 1.6], ["gpt-4.1", 2, 8],
  ["gpt-4o-mini", 0.15, 0.6], ["gpt-4o", 2.5, 10],
  ["o4-mini", 1.1, 4.4], ["o3-mini", 1.1, 4.4], ["o3", 2, 8],
  ["claude-opus-4", 15, 75], ["claude-sonnet-4", 3, 15], ["claude-3-7-sonnet", 3, 15], ["claude-3-5-sonnet", 3, 15],
  ["claude-haiku-4", 1, 5], ["claude-3-5-haiku", 0.8, 4], ["claude-3-haiku", 0.25, 1.25],
  ["gemini-2.5-pro", 1.25, 10], ["gemini-2.5-flash-lite", 0.1, 0.4], ["gemini-2.5-flash", 0.3, 2.5],
  ["gemini-2.0-flash", 0.1, 0.4],
].sort((a, b) => (b[0] as string).length - (a[0] as string).length) as [string, number, number][];

/** USD per million tokens for a model, or null when it isn't in the table. Provider prefixes
 *  ("openai/", "anthropic.", "us.anthropic.") and Bedrock version suffixes don't matter. */
export function llmPrice(model: string | null | undefined): { input: number; output: number } | null {
  if (!model) return null;
  const m = model.toLowerCase().replace(/^.*[/]/, "").replace(/^(us\.|eu\.|apac\.)?(anthropic|openai|google)\./, "");
  const hit = PRICES.find(([p]) => m.startsWith(p));
  return hit ? { input: hit[1], output: hit[2] } : null;
}

/** Estimated USD for tokens on a model, or null for an unknown model. */
export function estimateCost(model: string | null | undefined, input: number, output: number): number | null {
  const p = llmPrice(model);
  return p ? (input * p.input + output * p.output) / 1e6 : null;
}
