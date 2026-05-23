import type { Issue, MetadataBundle, SemanticCommand, Token } from "../types";
import { detectIssues } from "./diagnostics";
import { resetIds } from "./ids";
import { buildSemantic } from "./semantic";
import { tokenize } from "./tokenize";

export interface ParseResult {
  tokens: Token[];
  semantic: SemanticCommand;
  issues: Issue[];
}

export function analyzeCommand(command: string, metadata: MetadataBundle): ParseResult {
  resetIds();
  const tokens = tokenize(command);
  const semantic = buildSemantic(tokens, metadata);
  const issues = detectIssues(tokens, semantic, metadata);
  return { tokens, semantic, issues };
}

export { buildTreeNodes, buildFlowNodes, summarizeCommand } from "./visualization";
export { splitStreamSpecifier } from "./streamSpecifier";
