import type { FilterArgument, FilterChain, FilterStep, OptionBinding } from "../types";
import { splitStreamSpecifier } from "./streamSpecifier";

// Offset-aware parsing: ``parseFilterComplex`` does its own splitting while
// tracking character offsets, so each chain / step / arg carries a ``range``
// (relative to the expression string). ``sourceRanges.ts`` maps those to
// absolute command positions to highlight the matching text in the command
// input.

type Segment = { text: string; start: number; end: number };

/** Split on a top-level separator (ignoring it inside quotes or parentheses),
 * returning each piece with its offset range relative to ``str``. */
function splitTopLevel(str: string, sep: string): Segment[] {
  const segs: Segment[] = [];
  let depth = 0;
  let inSingle = false;
  let inDouble = false;
  let escape = false;
  let segStart = 0;
  for (let i = 0; i < str.length; i += 1) {
    const ch = str[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (ch === "\\") {
      escape = true;
      continue;
    }
    if (!inDouble && ch === "'") {
      inSingle = !inSingle;
      continue;
    }
    if (!inSingle && ch === '"') {
      inDouble = !inDouble;
      continue;
    }
    if (!inSingle && !inDouble) {
      if (ch === "(") depth += 1;
      else if (ch === ")" && depth > 0) depth -= 1;
      else if (ch === sep && depth === 0) {
        segs.push({ text: str.slice(segStart, i), start: segStart, end: i });
        segStart = i + 1;
        continue;
      }
    }
  }
  segs.push({ text: str.slice(segStart), start: segStart, end: str.length });
  return segs;
}

/** Trim surrounding whitespace from a [start, end) range over ``s``. */
function trimRange(s: string, start: number, end: number) {
  let a = start;
  let b = end;
  while (a < b && /\s/.test(s[a])) a += 1;
  while (b > a && /\s/.test(s[b - 1])) b -= 1;
  return { start: a, end: b };
}

const padNamesOf = (run: string | undefined) =>
  run ? Array.from(run.matchAll(/\[([^\]]+)\]/g)).map((m) => m[1]) : [];

/** Parse one filter segment (``name=arg1:key=val:...``) into name + args,
 * recording each arg's offset range relative to the whole expression. */
function parseStep(expr: string, segStart: number, segEnd: number): { name: string; args: FilterArgument[] } {
  const segText = expr.slice(segStart, segEnd);
  const firstEq = segText.indexOf("=");
  if (firstEq === -1) {
    return { name: segText.trim(), args: [] };
  }
  const name = segText.slice(0, firstEq).trim();
  const restStart = segStart + firstEq + 1;
  const rest = expr.slice(restStart, segEnd);
  const args: FilterArgument[] = [];
  splitTopLevel(rest, ":").forEach((part, index) => {
    const tr = trimRange(expr, restStart + part.start, restStart + part.end);
    if (tr.start >= tr.end) return;
    const text = expr.slice(tr.start, tr.end);
    const eqIndex = text.indexOf("=");
    if (eqIndex === -1) {
      args.push({ key: index === 0 ? "args" : `arg${index + 1}`, value: text, range: tr });
    } else {
      args.push({ key: text.slice(0, eqIndex).trim(), value: text.slice(eqIndex + 1).trim(), range: tr });
    }
  });
  return { name, args };
}

export function parseFilterComplex(expression: string): FilterChain[] {
  const chains: FilterChain[] = [];

  splitTopLevel(expression, ";").forEach((seg) => {
    const ct = trimRange(expression, seg.start, seg.end);
    if (ct.start >= ct.end) return;
    const chainText = expression.slice(ct.start, ct.end);

    // Pad labels only appear as a run of [...] groups at the very start (inputs)
    // and very end (outputs) of a chain.
    const leadRun = chainText.match(/^(?:\s*\[[^\]]+\]\s*)+/)?.[0] ?? "";
    let trailRun = chainText.match(/(?:\s*\[[^\]]+\]\s*)+$/)?.[0] ?? "";
    if (leadRun.length + trailRun.length > chainText.length) {
      trailRun = chainText.slice(leadRun.length); // degenerate: all brackets
    }
    const inputPads = padNamesOf(leadRun);
    const outputPads = padNamesOf(trailRun);
    const bracketLabel = Array.from(chainText.matchAll(/\[[^\]]+\]/g)).map((m) => m[0]).join("");

    const coreStart = ct.start + leadRun.length;
    const coreEnd = ct.end - trailRun.length;
    const coreText = expression.slice(coreStart, coreEnd);

    const steps: FilterStep[] = [];
    splitTopLevel(coreText, ",").forEach((fs) => {
      const tr = trimRange(expression, coreStart + fs.start, coreStart + fs.end);
      if (tr.start >= tr.end) return;
      const { name, args } = parseStep(expression, tr.start, tr.end);
      if (!name) return;
      steps.push({ name, args, range: { start: tr.start, end: tr.end } });
    });

    const names = steps.map((s) => s.name).filter(Boolean);
    chains.push({
      id: `fc_${chains.length}`,
      label: bracketLabel ? `${bracketLabel} ${names.join(" → ")}`.trim() : names.join(" → "),
      filters: steps,
      inputPads,
      outputPads,
      range: { start: ct.start, end: ct.end },
    });
  });

  return chains;
}

export function isFilterComplexBinding(binding: OptionBinding) {
  const { base } = splitStreamSpecifier(binding.flag.toLowerCase());
  return base === "-filter_complex";
}
