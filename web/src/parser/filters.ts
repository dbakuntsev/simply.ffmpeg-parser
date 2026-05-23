import type { OptionBinding } from "../types";
import { splitStreamSpecifier } from "./streamSpecifier";

export function splitChainFilters(chain: string) {
  const segments: string[] = [];
  let current = "";
  let inSingleQuote = false;
  let inDoubleQuote = false;
  let depth = 0;
  let escape = false;

  for (let i = 0; i < chain.length; i += 1) {
    const ch = chain[i];
    if (escape) {
      current += ch;
      escape = false;
      continue;
    }
    if (ch === "\\") {
      escape = true;
      current += ch;
      continue;
    }
    if (!inDoubleQuote && ch === "'") {
      inSingleQuote = !inSingleQuote;
      current += ch;
      continue;
    }
    if (!inSingleQuote && ch === '"') {
      inDoubleQuote = !inDoubleQuote;
      current += ch;
      continue;
    }
    if (!inSingleQuote && !inDoubleQuote) {
      if (ch === "(") {
        depth += 1;
      } else if (ch === ")" && depth > 0) {
        depth -= 1;
      }
      if (ch === "," && depth === 0) {
        segments.push(current.trim());
        current = "";
        continue;
      }
    }
    current += ch;
  }

  if (current.trim()) {
    segments.push(current.trim());
  }

  return segments;
}

export function splitFilterArgs(segment: string): {
  name: string;
  args: { key: string; value: string }[];
} {
  const args: { key: string; value: string }[] = [];
  const firstEq = segment.indexOf("=");
  if (firstEq === -1) {
    return { name: segment.trim(), args };
  }

  const name = segment.slice(0, firstEq).trim();
  const rest = segment.slice(firstEq + 1);
  const parts: string[] = [];
  let current = "";
  let inSingleQuote = false;
  let inDoubleQuote = false;
  let depth = 0;
  let escape = false;

  for (let i = 0; i < rest.length; i += 1) {
    const ch = rest[i];
    if (escape) {
      current += ch;
      escape = false;
      continue;
    }
    if (ch === "\\") {
      escape = true;
      current += ch;
      continue;
    }
    if (!inDoubleQuote && ch === "'") {
      inSingleQuote = !inSingleQuote;
      current += ch;
      continue;
    }
    if (!inSingleQuote && ch === '"') {
      inDoubleQuote = !inDoubleQuote;
      current += ch;
      continue;
    }
    if (!inSingleQuote && !inDoubleQuote) {
      if (ch === "(") {
        depth += 1;
      } else if (ch === ")" && depth > 0) {
        depth -= 1;
      }
      if (ch === ":" && depth === 0) {
        parts.push(current.trim());
        current = "";
        continue;
      }
    }
    current += ch;
  }
  if (current.trim()) {
    parts.push(current.trim());
  }

  parts.forEach((part, index) => {
    if (!part) return;
    const eqIndex = part.indexOf("=");
    if (eqIndex === -1) {
      args.push({ key: index === 0 ? "args" : `arg${index + 1}`, value: part });
      return;
    }
    args.push({ key: part.slice(0, eqIndex).trim(), value: part.slice(eqIndex + 1).trim() });
  });

  return { name, args };
}

export function parseFilterComplex(expression: string) {
  const chains = expression
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean);

  return chains.map((chain, index) => {
    const bracketMatches = Array.from(chain.matchAll(/\[[^\]]+\]/g)).map((m) => m[0]);
    const bracketLabel = bracketMatches.join("");
    const cleaned = chain.replace(/\[[^\]]+\]/g, "");
    const filterSegments = splitChainFilters(cleaned);

    const filterSteps = filterSegments
      .map((segment) => segment.trim())
      .filter(Boolean)
      .map((segment) => splitFilterArgs(segment));

    const filterNames = filterSteps.map((step) => step.name).filter(Boolean);

    return {
      id: `fc_${index}`,
      label: bracketLabel ? `${bracketLabel} ${filterNames.join(" → ")}`.trim() : filterNames.join(" → "),
      filters: filterSteps,
    };
  });
}

export function isFilterComplexBinding(binding: OptionBinding) {
  const { base } = splitStreamSpecifier(binding.flag.toLowerCase());
  return base === "-filter_complex";
}
