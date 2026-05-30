import type { Issue, MetadataBundle, SemanticCommand, Token } from "../types";
import { detectIssues } from "./diagnostics";
import { resetIds } from "./ids";
import { buildMetadataLookups, MetadataLookups } from "./lookups";
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

/** Analyze a command. ``lookups`` is precomputed once per metadata bundle
 * (see ``useMetadata``); falls back to building it on the fly when omitted,
 * which keeps callers that don't memoize working at a small per-call cost. */
export function analyzeCommand(
  command: string,
  metadata: MetadataBundle,
  lookups?: MetadataLookups
): ParseResult {
  resetIds();
  const tokens = tokenize(command);
  const index = buildOptionIndex(metadata);
  const resolved = resolveAll(tokens, index);
  const semantic = buildSemantic(tokens, metadata, resolved);
  const effective = lookups ?? buildMetadataLookups(metadata);
  const issues = detectIssues(tokens, semantic, effective, resolved);
  return { tokens, semantic, issues, resolved };
}

export { buildTreeNodes } from "./tree";
export { buildPipelineModel } from "./pipeline";
export type {
  PipelineModel,
  PipelineBox,
  PipelineEdge,
  PipelineRow,
  PipelineStage,
} from "./pipeline";
export { splitStreamSpecifier, CODEC_SELECTOR_BASES } from "./streamSpecifier";
export { buildMetadataLookups } from "./lookups";
export type { MetadataLookups, CatalogLookups, NamedLookup, FilterInfo } from "./lookups";
export type { ResolvedOption, ResolutionSource } from "./resolver";
