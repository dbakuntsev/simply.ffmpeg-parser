import type { MetadataBundle, Token } from "../types";
import { splitStreamSpecifier } from "./streamSpecifier";

export type OptionInfo = MetadataBundle["options"]["options"][number];

type FallbackOption = {
  scope: "global" | "input" | "output";
  valueType: string;
  values?: string[];
};

export const FALLBACK_OPTIONS: Record<string, FallbackOption> = {
  "-crf": { scope: "output", valueType: "int" },
  "-preset": {
    scope: "output",
    valueType: "enum",
    values: [
      "ultrafast",
      "superfast",
      "veryfast",
      "faster",
      "fast",
      "medium",
      "slow",
      "slower",
      "veryslow",
      "placebo",
    ],
  },
  "-movflags": { scope: "output", valueType: "string" },
  "-b": { scope: "output", valueType: "string" },
  "-map": { scope: "output", valueType: "string" },
  "-r": { scope: "output", valueType: "int" },
  "-y": { scope: "global", valueType: "none" },
  "-n": { scope: "global", valueType: "none" },
  "-hide_banner": { scope: "global", valueType: "none" },
  "-nostdin": { scope: "global", valueType: "none" },
  "-stats": { scope: "global", valueType: "none" },
  "-vn": { scope: "output", valueType: "none" },
  "-an": { scope: "output", valueType: "none" },
  "-sn": { scope: "output", valueType: "none" },
  "-dn": { scope: "output", valueType: "none" },
  "-filter_complex": { scope: "global", valueType: "string" },
};

export const NO_VALUE_OVERRIDES = new Set([
  "-y",
  "-n",
  "-hide_banner",
  "-nostdin",
  "-stats",
  "-vn",
  "-an",
  "-sn",
  "-dn",
]);

export function buildOptionLookup(metadata: MetadataBundle) {
  const lookup = new Map<string, OptionInfo>();
  for (const opt of metadata.options.options) {
    lookup.set(opt.name, opt);
    for (const alias of opt.aliases || []) {
      lookup.set(alias, opt);
    }
  }
  return lookup;
}

export function buildFallbackOption(base: string): OptionInfo | null {
  const fallback = FALLBACK_OPTIONS[base];
  if (!fallback) {
    return null;
  }
  return {
    name: base,
    aliases: [],
    scope: fallback.scope,
    valueType: fallback.valueType,
    values: fallback.values ?? [],
    requires: [],
    conflicts: [],
    description: [],
  };
}

export function resolveOptionInfo(token: Token, optionLookup: Map<string, OptionInfo>) {
  const normalized = token.normalizedText;
  const { base } = splitStreamSpecifier(normalized);
  return (
    optionLookup.get(base) ??
    optionLookup.get(normalized) ??
    optionLookup.get(token.text) ??
    buildFallbackOption(base)
  );
}

export function shouldExpectValue(optionInfo: OptionInfo | null, flag: string) {
  const { base } = splitStreamSpecifier(flag);
  if (NO_VALUE_OVERRIDES.has(base)) {
    return false;
  }
  return optionInfo ? optionInfo.valueType !== "none" : true;
}
