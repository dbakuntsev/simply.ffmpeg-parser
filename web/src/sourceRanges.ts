import type { ParseResult } from "./parser";
import { splitStreamSpecifier } from "./parser";
import type { FilterRange, OptionBinding, Token } from "./types";

export type SourceRange = { start: number; end: number };

/** Map each character index of ``token.text`` to its absolute index in the raw
 * command. ``token.sourceRange`` spans the raw (possibly quoted/escaped) text
 * while ``token.text`` is de-quoted, so we re-scan the raw span replicating the
 * tokenizer's quote/escape handling and record the source index of every
 * emitted character. ``map[k]`` is the command index of ``token.text[k]``. */
function tokenTextToSourceMap(command: string, token: Token): number[] {
  const map: number[] = [];
  const { start, end } = token.sourceRange;
  let escape = false;
  for (let p = start; p < end && p < command.length; p += 1) {
    const ch = command[p];
    if (escape) {
      map.push(p);
      escape = false;
      continue;
    }
    if (ch === "\\") {
      escape = true;
      continue;
    }
    if (ch === '"') continue; // quote is consumed, not part of the text
    map.push(p);
  }
  return map;
}

/** Build a lookup from selectable node id (option/box/chain/step/arg) to the
 * span of the command text it corresponds to, for highlighting in the command
 * input. Ids deliberately match those produced by ``buildSelectionInfo`` /
 * ``buildPipelineModel`` / ``buildTreeNodes``. */
export function buildSourceRanges(command: string, analysis: ParseResult): Map<string, SourceRange> {
  const ranges = new Map<string, SourceRange>();
  const tokenById = new Map(analysis.tokens.map((t) => [t.id, t] as const));
  const { semantic } = analysis;

  const unionRange = (tokenIds: string[]): SourceRange | null => {
    let s = Infinity;
    let e = -Infinity;
    for (const id of tokenIds) {
      const t = tokenById.get(id);
      if (!t) continue;
      s = Math.min(s, t.sourceRange.start);
      e = Math.max(e, t.sourceRange.end);
    }
    return e >= 0 ? { start: s, end: e } : null;
  };

  const addOption = (opt: OptionBinding) => {
    const r = unionRange(opt.tokenIds);
    if (r) ranges.set(opt.id, r);
  };

  const findFormatOption = (opts: OptionBinding[]) =>
    opts.find((o) => splitStreamSpecifier(o.flag.toLowerCase()).base === "-f");

  const tokenRange = (tokenId?: string): SourceRange | null => {
    const t = tokenId ? tokenById.get(tokenId) : undefined;
    return t ? { start: t.sourceRange.start, end: t.sourceRange.end } : null;
  };

  semantic.globals.forEach(addOption);

  semantic.inputs.forEach((input, i) => {
    const fileRange = tokenRange(input.tokenId);
    if (fileRange) {
      ranges.set(input.id, fileRange);
      ranges.set(`input_${i}`, fileRange);
    }
    // Demuxer box highlights its explicit ``-f`` option, else the source file.
    const fOpt = findFormatOption(input.options);
    const demux = fOpt ? unionRange(fOpt.tokenIds) : fileRange;
    if (demux) ranges.set(`demuxer_${i}`, demux);
    input.options.forEach(addOption);
  });

  semantic.outputs.forEach((output, j) => {
    const fileRange = tokenRange(output.tokenId);
    if (fileRange) {
      ranges.set(output.id, fileRange);
      ranges.set(`output_${j}`, fileRange);
    }
    const fOpt = findFormatOption(output.options);
    const mux = fOpt ? unionRange(fOpt.tokenIds) : fileRange;
    if (mux) ranges.set(`muxer_${j}`, mux);
    output.options.forEach(addOption);
  });

  semantic.filters.forEach((filter) => {
    const token = filter.valueTokenId ? tokenById.get(filter.valueTokenId) : undefined;
    if (!token) return;
    const map = tokenTextToSourceMap(command, token);
    if (!map.length) return;

    // Whole filtergraph = the de-quoted expression span (excludes the quotes).
    ranges.set(filter.id, { start: map[0], end: map[map.length - 1] + 1 });

    const toAbs = (rel?: FilterRange): SourceRange | null => {
      if (!rel) return null;
      const a = map[rel.start];
      const lastIdx = rel.end - 1;
      const b = lastIdx >= 0 && lastIdx < map.length ? map[lastIdx] + 1 : undefined;
      return a !== undefined && b !== undefined ? { start: a, end: b } : null;
    };

    filter.chains?.forEach((chain, ci) => {
      const cr = toAbs(chain.range);
      if (cr) {
        ranges.set(chain.id, cr);
        ranges.set(`${filter.id}_chain_${ci}`, cr);
      }
      chain.filters.forEach((step, k) => {
        const sr = toAbs(step.range);
        if (sr) ranges.set(`${chain.id}_step_${k}`, sr);
        step.args.forEach((arg, ai) => {
          const ar = toAbs(arg.range);
          if (ar) ranges.set(`${chain.id}_step_${k}_arg_${ai}`, ar);
        });
      });
    });
  });

  return ranges;
}
