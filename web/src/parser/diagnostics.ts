import type { Issue, MetadataBundle, SemanticCommand, Token } from "../types";
import { nextIssueId } from "./ids";
import { ResolvedOption, shouldExpectValue } from "./resolver";
import { splitStreamSpecifier } from "./streamSpecifier";

export function detectIssues(
  tokens: Token[],
  semantic: SemanticCommand,
  metadata: MetadataBundle,
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

  issues.push(...validateNamedValues(tokens, metadata));

  return issues;
}

const CODEC_VALUE_BASES = new Set(["-c", "-codec", "-vcodec", "-acodec", "-scodec"]);

function buildNameSet(entries: { name: string; aliases?: string[] }[] | undefined): Set<string> {
  const set = new Set<string>();
  for (const e of entries ?? []) {
    set.add(e.name.toLowerCase());
    for (const a of e.aliases || []) set.add(a.toLowerCase());
  }
  return set;
}

function validateNamedValues(tokens: Token[], metadata: MetadataBundle): Issue[] {
  const issues: Issue[] = [];

  const codecs = buildNameSet(metadata.codecs?.codecs);
  const muxers = buildNameSet(metadata.muxers?.muxers);
  const demuxers = buildNameSet(metadata.demuxers?.demuxers);
  const bsfs = buildNameSet(metadata.bitstreamFilters?.bitstream_filters);

  let seenInput = false;
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token.type !== "flag") continue;

    if (token.normalizedText === "-i") {
      seenInput = true;
      continue;
    }

    const valueToken = tokens[i + 1];
    if (!valueToken || valueToken.type === "flag") continue;
    const value = valueToken.text;
    if (!value) continue;

    const { base } = splitStreamSpecifier(token.normalizedText);

    if (CODEC_VALUE_BASES.has(base)) {
      // ``copy`` is a passthrough pseudo-codec, always valid.
      if (value.toLowerCase() === "copy") continue;
      if (codecs.size === 0) continue;
      if (!codecs.has(value.toLowerCase())) {
        issues.push({
          id: nextIssueId(),
          severity: "warning",
          code: "unknown-codec",
          message: `Unknown codec "${value}"`,
          explanation: `"${value}" is not a codec known to this FFmpeg version. Check for typos or select a different version.`,
          tokenIds: [token.id, valueToken.id],
          scope: seenInput ? "output" : "input",
          relatedIds: [],
        });
      }
      continue;
    }

    if (base === "-f") {
      const side: "muxer" | "demuxer" = seenInput ? "muxer" : "demuxer";
      const set = side === "muxer" ? muxers : demuxers;
      if (set.size === 0) continue;
      if (!set.has(value.toLowerCase())) {
        issues.push({
          id: nextIssueId(),
          severity: "warning",
          code: side === "muxer" ? "unknown-muxer" : "unknown-demuxer",
          message: `Unknown ${side} "${value}"`,
          explanation: `"${value}" is not a ${side} known to this FFmpeg version. Check for typos or select a different version.`,
          tokenIds: [token.id, valueToken.id],
          scope: side === "muxer" ? "output" : "input",
          relatedIds: [],
        });
      }
      continue;
    }

    if (base === "-bsf") {
      if (bsfs.size === 0) continue;
      // Value form: ``name1[=args],name2[=args],...``. Args may themselves
      // contain ``=``, so only split off the first ``=`` on each segment.
      for (const segment of value.split(",")) {
        const trimmed = segment.trim();
        if (!trimmed) continue;
        const eq = trimmed.indexOf("=");
        const name = (eq === -1 ? trimmed : trimmed.slice(0, eq)).trim();
        if (!name) continue;
        if (!bsfs.has(name.toLowerCase())) {
          issues.push({
            id: nextIssueId(),
            severity: "warning",
            code: "unknown-bitstream-filter",
            message: `Unknown bitstream filter "${name}"`,
            explanation: `"${name}" is not a bitstream filter known to this FFmpeg version. Check for typos or select a different version.`,
            tokenIds: [token.id, valueToken.id],
            scope: "output",
            relatedIds: [],
          });
        }
      }
      continue;
    }
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

  if (
    (optionInfo.valueType === "enum" || optionInfo.valueType === "string") &&
    optionInfo.values &&
    optionInfo.values.length > 0
  ) {
    if (!optionInfo.values.includes(trimmed)) {
      return {
        severity: "warning" as const,
        code: "invalid-enum",
        message: `Unexpected value "${value}"`,
        explanation: `Expected one of: ${formatValueList(optionInfo.values)}.`,
      };
    }
  }

  if (optionInfo.valueType === "flags" && optionInfo.values && optionInfo.values.length > 0) {
    // Numeric bitmasks like ``0x40`` or ``42`` bypass the named-flag check.
    if (!/^(?:0[xX][0-9a-fA-F]+|-?\d+)$/.test(trimmed)) {
      const unknown: string[] = [];
      const allowed = new Set(optionInfo.values);
      // Tokens are separated by ``+`` or ``-`` (and a leading sign is optional).
      // Split, drop empties, check each.
      for (const tok of trimmed.split(/[+\-]/)) {
        const name = tok.trim();
        if (!name) continue;
        if (!allowed.has(name)) unknown.push(name);
      }
      if (unknown.length > 0) {
        return {
          severity: "warning" as const,
          code: "invalid-flag",
          message: `Unknown flag${unknown.length > 1 ? "s" : ""} ${unknown.map((n) => `"${n}"`).join(", ")}`,
          explanation: `Expected ${unknown.length > 1 ? "names" : "a name"} from: ${formatValueList(optionInfo.values)}.`,
        };
      }
    }
  }

  return null;
}

function formatValueList(values: string[]): string {
  const MAX = 12;
  if (values.length <= MAX) return values.join(", ");
  return `${values.slice(0, MAX).join(", ")}, … (${values.length - MAX} more)`;
}
