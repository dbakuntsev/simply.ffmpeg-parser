import type { Issue, MetadataBundle, SemanticCommand, Token } from "../types";
import { nextIssueId } from "./ids";
import { ResolvedOption, shouldExpectValue } from "./resolver";
import { splitStreamSpecifier } from "./streamSpecifier";

export function detectIssues(
  tokens: Token[],
  semantic: SemanticCommand,
  _metadata: MetadataBundle,
  resolved: Map<string, ResolvedOption | null>
): Issue[] {
  const issues: Issue[] = [];

  let hasGenericCodecFlag = false;
  const copyCodecTokenIds: string[] = [];

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token.type !== "flag") {
      continue;
    }

    const resolution = resolved.get(token.id) ?? null;
    const optionInfo = resolution?.info ?? null;
    if (!optionInfo) {
      issues.push({
        id: nextIssueId(),
        severity: "warning",
        code: "unknown-option",
        message: `Unknown option ${token.text}`,
        explanation: "This flag is not present in the selected FFmpeg version metadata.",
        tokenIds: [token.id],
        scope: "global",
        relatedIds: [],
      });
      continue;
    }

    const { base } = splitStreamSpecifier(token.normalizedText);
    if (base === "-c" && token.normalizedText === "-c") {
      hasGenericCodecFlag = true;
    }

    const valueToken = tokens[i + 1];
    const expectsValue = shouldExpectValue(optionInfo);
    if (expectsValue) {
      const valueMissing = !valueToken || (valueToken.type === "flag" && !allowsFlagValue(optionInfo, valueToken.text));
      if (valueMissing) {
        issues.push({
          id: nextIssueId(),
          severity: "error",
          code: "missing-option-value",
          message: `Missing value for ${token.text}`,
          explanation: `The option ${token.text} expects a value.`,
          tokenIds: [token.id],
          scope: optionInfo.scope,
          relatedIds: [],
        });
        continue;
      }

      if (valueToken) {
        const validation = validateOptionValue(optionInfo, valueToken.text);
        if (validation) {
          issues.push({
            id: nextIssueId(),
            severity: validation.severity,
            code: validation.code,
            message: validation.message,
            explanation: validation.explanation,
            tokenIds: [token.id, valueToken.id],
            scope: optionInfo.scope,
            relatedIds: [],
          });
        }

        if (base === "-c" && valueToken.normalizedText === "copy") {
          copyCodecTokenIds.push(token.id, valueToken.id);
        }
      }
    }
  }

  if (semantic.outputs.length === 0) {
    issues.push({
      id: nextIssueId(),
      severity: "error",
      code: "missing-output",
      message: "No output target detected",
      explanation: "FFmpeg commands require at least one output file or stream.",
      tokenIds: [],
      scope: "output",
      relatedIds: [],
    });
  }

  if (hasGenericCodecFlag) {
    issues.push({
      id: nextIssueId(),
      severity: "warning",
      code: "generic-codec",
      message: "Generic -c used without stream specifier",
      explanation: "Consider -c:v or -c:a to avoid unintended codec selection.",
      tokenIds: tokens.filter((t) => t.normalizedText === "-c").map((t) => t.id),
      scope: "output",
      relatedIds: [],
    });
  }

  if (copyCodecTokenIds.length > 0 && semantic.filters.length > 0) {
    issues.push({
      id: nextIssueId(),
      severity: "warning",
      code: "copy-with-filters",
      message: "Stream copy with filters",
      explanation: "Filters require re-encoding; remove -c copy or filters.",
      tokenIds: copyCodecTokenIds,
      scope: "output",
      relatedIds: [],
    });
  }

  return issues;
}

function allowsFlagValue(optionInfo: { valueType: string }, value: string) {
  if (optionInfo.valueType === "int" || optionInfo.valueType === "float") {
    return /^-\d/.test(value);
  }
  return false;
}

function validateOptionValue(optionInfo: { valueType: string; values: string[] }, value: string) {
  const trimmed = value.trim();
  if (!trimmed) {
    return {
      severity: "error" as const,
      code: "empty-option-value",
      message: "Empty value provided",
      explanation: "This option expects a non-empty value.",
    };
  }

  if (optionInfo.valueType === "int") {
    if (!/^-?\d+$/.test(trimmed)) {
      return {
        severity: "warning" as const,
        code: "invalid-int",
        message: `Expected integer value, got "${value}"`,
        explanation: "This option expects an integer value.",
      };
    }
  }

  if (optionInfo.valueType === "float") {
    if (!/^-?\d+(?:\.\d+)?$/.test(trimmed)) {
      return {
        severity: "warning" as const,
        code: "invalid-float",
        message: `Expected numeric value, got "${value}"`,
        explanation: "This option expects a numeric value.",
      };
    }
  }

  if (optionInfo.valueType === "enum" && optionInfo.values && optionInfo.values.length > 0) {
    if (!optionInfo.values.includes(trimmed)) {
      return {
        severity: "warning" as const,
        code: "invalid-enum",
        message: `Unexpected value "${value}"`,
        explanation: `Expected one of: ${optionInfo.values.join(", ")}.`,
      };
    }
  }

  return null;
}
