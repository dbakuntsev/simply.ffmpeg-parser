import type { Issue, MetadataBundle, SemanticCommand, Token } from "../types";
import { detectIssues } from "./diagnostics";
import { resetIds } from "./ids";
import { buildOptionIndex, resolveAll, ResolvedOption } from "./resolver";
import { buildSemantic } from "./semantic";
import { tokenize } from "./tokenize";

export interface ParseResult {
  tokens: Token[];
  semantic: SemanticCommand;
  issues: Issue[];
  /** Per-token resolution results from the layered resolver. Keyed by
   * ``token.id``; missing entries (value/input/output tokens) have no
   * resolution. The selection popover consults this to render layer-aware
   * details (codec-private vs driver, matched codec/format, etc.). */
  resolved: Map<string, ResolvedOption | null>;
}

export function analyzeCommand(command: string, metadata: MetadataBundle): ParseResult {
  resetIds();
  const tokens = tokenize(command);
  const index = buildOptionIndex(metadata);
  const resolved = resolveAll(tokens, index);
  const semantic = buildSemantic(tokens, metadata, resolved);
  const issues = detectIssues(tokens, semantic, metadata, resolved);
  return { tokens, semantic, issues, resolved };
}

export { buildTreeNodes, buildPipelineModel, summarizeCommand } from "./visualization";
export type {
  PipelineModel,
  PipelineBox,
  PipelineEdge,
  PipelineRow,
  PipelineStage,
} from "./visualization";
export { splitStreamSpecifier, CODEC_SELECTOR_BASES } from "./streamSpecifier";
export type { ResolvedOption, ResolutionSource } from "./resolver";
