import type {
  EngineeringBudgetOutcomeV2,
  EngineeringContextBuildResultV2,
  EngineeringContextPackV2,
  EngineeringRenderingV2,
} from "../../../generated/typescript/application/v1/index.js";

declare const rendering: EngineeringRenderingV2;
declare const outcome: EngineeringBudgetOutcomeV2;
declare const pack: EngineeringContextPackV2;
declare const result: EngineeringContextBuildResultV2;

const format: "engineering_context.v2" = pack.format_version;
const renderedBytes: number = result.pack.rendering.byte_count;
const outcomeBytes: number = result.pack.budget.rendered_bytes;

// The byte-only v2 views must not expose the v1 token fields at any nesting
// depth. Each expected error also proves the alias is being checked by tsc.
// @ts-expect-error token_count is absent from EngineeringRenderingV2.
rendering.token_count;
// @ts-expect-error rendered_tokens is absent from EngineeringBudgetOutcomeV2.
outcome.rendered_tokens;
// @ts-expect-error model_tokens is absent from the v2 effective budget.
result.pack.budget.effective.model_tokens;
// @ts-expect-error nested rendering uses EngineeringRenderingV2.
result.pack.rendering.token_count;

export const byteOnlyView = { format, renderedBytes, outcomeBytes };
