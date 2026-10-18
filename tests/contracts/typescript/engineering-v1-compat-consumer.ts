import type {
  EngineeringBudgetOutcome,
  EngineeringContextBuildResult,
  EngineeringContextPack,
  EngineeringRendering,
} from "../../../generated/typescript/application/v1/index.js";

declare const rendering: EngineeringRendering;
declare const outcome: EngineeringBudgetOutcome;
declare const pack: EngineeringContextPack;
declare const result: EngineeringContextBuildResult;

// These are intentionally strict number assignments. They fail to compile if
// the established v1 names weaken either legacy count to `number | undefined`.
const directRenderingTokens: number = rendering.token_count;
const directOutcomeTokens: number = outcome.rendered_tokens;
const packRenderingTokens: number = pack.rendering.token_count;
const packOutcomeTokens: number = pack.budget.rendered_tokens;
const resultRenderingTokens: number = result.pack.rendering.token_count;
const resultOutcomeTokens: number = result.pack.budget.rendered_tokens;

export const legacyTokenTotal =
  directRenderingTokens +
  directOutcomeTokens +
  packRenderingTokens +
  packOutcomeTokens +
  resultRenderingTokens +
  resultOutcomeTokens;
