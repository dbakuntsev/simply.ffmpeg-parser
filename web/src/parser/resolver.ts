/**
 * Layered option resolver — replaces the old FALLBACK_OPTIONS table.
 *
 * Resolution order, applied per-token in document position:
 *
 *   1. If a codec is active for the token's stream type (or for any type when
 *      no specifier is given), and that codec's per-codec options table
 *      contains this flag, use it. This is the "shadow override" — e.g.
 *      ``-preset`` resolves to libx264's preset rather than the image2 muxer
 *      preset whenever ``-c:v libx264`` is active.
 *   2. Otherwise, the driver-level options.json (the canonical pool).
 *   3. Otherwise, generic AVCodec options from codecs.texi's "Codec Options"
 *      chapter — applies whenever any codec is active (most commands have
 *      one implicit or explicit).
 *   4. Otherwise, format-private options for the active muxer/demuxer (set
 *      via ``-f`` or inferred from the output extension).
 *   5. Otherwise, generic AVFormat options from formats.texi's "Format
 *      Options" chapter — applies whenever a file is being read or written.
 *   6. Otherwise, ``null`` — diagnostics will surface ``unknown-option``.
 *
 * Active-codec / active-format tracking is positional: a ``-c:v libx264``
 * affects only flags that come after it in the token stream, matching how
 * the ffmpeg CLI itself binds codec choices per-file.
 */

import type { AVOptionEntry, MetadataBundle, Token } from "../types";
import { CODEC_SELECTOR_BASES, splitStreamSpecifier } from "./streamSpecifier";

export type OptionInfo = MetadataBundle["options"]["options"][number];

/** Where a resolved option came from in the layered lookup. */
export type ResolutionSource =
  | "driver"
  | "codec-private"
  | "codec-generic"
  | "format-private"
  | "format-generic";

export interface ResolvedOption {
  info: OptionInfo;
  source: ResolutionSource;
  /** For codec-private and codec-generic hits, the stream type this option
   * was inferred to apply to (v/a/s). Empty when the option isn't tied to a
   * specific stream type. */
  inferredStreamType?: "v" | "a" | "s";
  /** Codec name that contributed a codec-private match. Empty otherwise. */
  matchedCodec?: string;
  /** Format name that contributed a format-private match. Empty otherwise. */
  matchedFormat?: string;
}

/** Lookup index built once per metadata bundle. */
export interface OptionIndex {
  driver: Map<string, OptionInfo>;
  codecOptionsByName: Map<string, AVOptionEntry[]>;
  codecGeneric: Map<string, AVOptionEntry>;
  muxerOptionsByName: Map<string, AVOptionEntry[]>;
  demuxerOptionsByName: Map<string, AVOptionEntry[]>;
  formatGeneric: Map<string, AVOptionEntry>;
  /** Reverse index: option name → list of muxer/demuxer names that own it.
   * Used to resolve format-private options when ``-f`` wasn't given (a very
   * common case — users rely on the output extension to pick the muxer). */
  formatPrivateByOption: Map<string, { name: string; side: "muxer" | "demuxer" }[]>;
}

/** Build the index once per metadata bundle. The result is cheap to share
 * across the semantic and diagnostics passes. */
export function buildOptionIndex(metadata: MetadataBundle): OptionIndex {
  const driver = new Map<string, OptionInfo>();
  for (const opt of metadata.options.options) {
    driver.set(opt.name, opt);
    for (const alias of opt.aliases || []) driver.set(alias, opt);
  }

  const codecOptionsByName = new Map<string, AVOptionEntry[]>();
  for (const codec of metadata.codecs.codecs) {
    const opts = codec.options ?? [];
    if (!opts.length) continue;
    codecOptionsByName.set(codec.name.toLowerCase(), opts);
    for (const alias of codec.aliases || []) {
      codecOptionsByName.set(alias.toLowerCase(), opts);
    }
  }

  const codecGeneric = new Map<string, AVOptionEntry>();
  for (const opt of metadata.codecs.codec_options ?? []) {
    codecGeneric.set(opt.name, opt);
    for (const alias of opt.aliases || []) codecGeneric.set(alias, opt);
  }

  const muxerOptionsByName = new Map<string, AVOptionEntry[]>();
  for (const m of metadata.muxers.muxers ?? []) {
    const opts = m.options ?? [];
    if (!opts.length) continue;
    muxerOptionsByName.set(m.name.toLowerCase(), opts);
    for (const alias of m.aliases || []) {
      muxerOptionsByName.set(alias.toLowerCase(), opts);
    }
  }

  const demuxerOptionsByName = new Map<string, AVOptionEntry[]>();
  for (const d of metadata.demuxers.demuxers ?? []) {
    const opts = d.options ?? [];
    if (!opts.length) continue;
    demuxerOptionsByName.set(d.name.toLowerCase(), opts);
    for (const alias of d.aliases || []) {
      demuxerOptionsByName.set(alias.toLowerCase(), opts);
    }
  }

  const formatGeneric = new Map<string, AVOptionEntry>();
  for (const opt of metadata.muxers.format_options ?? []) {
    formatGeneric.set(opt.name, opt);
    for (const alias of opt.aliases || []) formatGeneric.set(alias, opt);
  }

  // Reverse index — used as a last-resort lookup when no -f is given.
  // Example: ``-movflags`` is only in mov/mp4/3gp/etc. options; finding any
  // entry tells us which family it belongs to.
  const formatPrivateByOption = new Map<
    string,
    { name: string; side: "muxer" | "demuxer" }[]
  >();
  const recordReverse = (
    bag: Map<string, AVOptionEntry[]>,
    side: "muxer" | "demuxer"
  ) => {
    for (const [name, opts] of bag) {
      for (const o of opts) {
        const names = [o.name, ...(o.aliases || [])];
        for (const n of names) {
          const list = formatPrivateByOption.get(n) ?? [];
          if (!list.some((x) => x.name === name && x.side === side)) {
            list.push({ name, side });
            formatPrivateByOption.set(n, list);
          }
        }
      }
    }
  };
  recordReverse(muxerOptionsByName, "muxer");
  recordReverse(demuxerOptionsByName, "demuxer");

  return {
    driver,
    codecOptionsByName,
    codecGeneric,
    muxerOptionsByName,
    demuxerOptionsByName,
    formatGeneric,
    formatPrivateByOption,
  };
}

/** Position-aware context the resolver consults for each token. */
interface ResolutionContext {
  /** Most recently selected codec per stream type (from ``-c[:T]`` etc.). */
  codec: { v?: string; a?: string; s?: string };
  /** Most recently selected format and its side (input vs output). */
  format?: { name: string; side: "muxer" | "demuxer" };
}

function inferStreamTypeFromCodecFlag(
  flag: string,
  specifier: string | null
): "v" | "a" | "s" | null {
  if (flag === "-vcodec") return "v";
  if (flag === "-acodec") return "a";
  if (flag === "-scodec") return "s";
  if (specifier) {
    const c = specifier[0].toLowerCase();
    if (c === "v" || c === "a" || c === "s") return c;
  }
  return null;
}

/** Synthesize an OptionInfo from an AVOptionEntry, filling in the
 * ``scope``/``requires``/``conflicts`` fields the SPA expects. The scope is
 * derived from where the resolver decided the option lives. */
function asOptionInfo(
  av: AVOptionEntry,
  scope: "global" | "input" | "output"
): OptionInfo {
  return {
    name: av.name,
    aliases: av.aliases,
    scope,
    valueType: av.valueType,
    values: av.values,
    valueDescriptions: av.valueDescriptions,
    requires: [],
    conflicts: [],
    description: av.description,
    anchor: av.anchor,
    signature: av.signature,
  };
}

function findInCodec(
  codecName: string,
  base: string,
  flag: string,
  index: OptionIndex
): AVOptionEntry | null {
  const opts = index.codecOptionsByName.get(codecName.toLowerCase());
  if (!opts) return null;
  for (const o of opts) {
    if (o.name === base || o.name === flag) return o;
    const aliases = o.aliases || [];
    if (aliases.includes(base) || aliases.includes(flag)) return o;
  }
  return null;
}

function findInAVMap(
  map: Map<string, AVOptionEntry>,
  base: string,
  flag: string
): AVOptionEntry | undefined {
  return map.get(base) ?? map.get(flag);
}

function resolveOne(
  flag: string,
  ctx: ResolutionContext,
  index: OptionIndex
): ResolvedOption | null {
  const normalized = flag.toLowerCase();
  const { base, specifier } = splitStreamSpecifier(normalized);

  // 1. Codec-private (active codec has the option). Wins over driver.
  const streamType = specifier
    ? (specifier[0] as "v" | "a" | "s" | undefined)
    : undefined;
  const candidateTypes: ("v" | "a" | "s")[] =
    streamType && (streamType === "v" || streamType === "a" || streamType === "s")
      ? [streamType]
      : ["v", "a", "s"];
  for (const t of candidateTypes) {
    const codec = ctx.codec[t];
    if (!codec) continue;
    const hit = findInCodec(codec, base, normalized, index);
    if (hit) {
      return {
        info: asOptionInfo(hit, "output"),
        source: "codec-private",
        inferredStreamType: t,
        matchedCodec: codec,
      };
    }
  }

  // 2. Driver options.
  const driverHit =
    index.driver.get(base) ?? index.driver.get(normalized) ?? index.driver.get(flag);
  if (driverHit) {
    return { info: driverHit, source: "driver" };
  }

  // 3. Generic AVCodec options — applies whenever a codec is active for any
  // stream type (which is essentially always in real commands).
  const anyCodec = ctx.codec.v || ctx.codec.a || ctx.codec.s;
  if (anyCodec) {
    const genericCodec = findInAVMap(index.codecGeneric, base, normalized);
    if (genericCodec) {
      return {
        info: asOptionInfo(genericCodec, "output"),
        source: "codec-generic",
        inferredStreamType: streamType,
      };
    }
  }

  // 4. Format-private. Try active format first; if none, fall back to the
  // reverse index (which side / which muxer the option belongs to,
  // disambiguating by the input/output position the resolver doesn't know
  // here — caller can later refine).
  if (ctx.format) {
    const bag =
      ctx.format.side === "muxer"
        ? index.muxerOptionsByName
        : index.demuxerOptionsByName;
    const opts = bag.get(ctx.format.name.toLowerCase());
    if (opts) {
      for (const o of opts) {
        if (
          o.name === base ||
          o.name === normalized ||
          (o.aliases || []).includes(base)
        ) {
          return {
            info: asOptionInfo(o, ctx.format.side === "muxer" ? "output" : "input"),
            source: "format-private",
            matchedFormat: ctx.format.name,
          };
        }
      }
    }
  } else {
    const reverse = index.formatPrivateByOption.get(base) ?? index.formatPrivateByOption.get(normalized);
    if (reverse && reverse.length) {
      // Pick the first match. When the same option name appears on both
      // muxer and demuxer sides (e.g. some format options leak both ways),
      // prefer the muxer entry — the bulk of these options are output-side.
      const preferred = reverse.find((r) => r.side === "muxer") ?? reverse[0];
      const bag =
        preferred.side === "muxer"
          ? index.muxerOptionsByName
          : index.demuxerOptionsByName;
      const opts = bag.get(preferred.name.toLowerCase());
      if (opts) {
        const hit = opts.find(
          (o) => o.name === base || o.name === normalized || (o.aliases || []).includes(base)
        );
        if (hit) {
          return {
            info: asOptionInfo(hit, preferred.side === "muxer" ? "output" : "input"),
            source: "format-private",
            matchedFormat: preferred.name,
          };
        }
      }
    }
  }

  // 5. Generic AVFormat options.
  const genericFormat = findInAVMap(index.formatGeneric, base, normalized);
  if (genericFormat) {
    // Side: prefer output unless format is known to be input.
    const scope: "global" | "input" | "output" =
      ctx.format?.side === "demuxer" ? "input" : "output";
    return {
      info: asOptionInfo(genericFormat, scope),
      source: "format-generic",
    };
  }

  return null;
}

/** Walk the token stream once, building per-token resolution results. */
export function resolveAll(
  tokens: Token[],
  index: OptionIndex
): Map<string, ResolvedOption | null> {
  const out = new Map<string, ResolvedOption | null>();
  const ctx: ResolutionContext = { codec: {} };
  let seenInput = false;

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token.type !== "flag") continue;

    const { base, specifier } = splitStreamSpecifier(token.normalizedText);

    // Track active codec selection BEFORE resolving — so a codec set on this
    // token can affect the same token's resolution (rare but happens for
    // self-referential aliases).
    if (CODEC_SELECTOR_BASES.has(base)) {
      const value = tokens[i + 1];
      if (value && value.type !== "flag") {
        const t = inferStreamTypeFromCodecFlag(token.normalizedText, specifier);
        if (t) {
          ctx.codec[t] = value.text;
        } else {
          // Bare ``-c X`` with no specifier — apply to all three so any
          // subsequent codec-private flag resolves regardless of stream type.
          ctx.codec.v = value.text;
          ctx.codec.a = value.text;
          ctx.codec.s = value.text;
        }
      }
    }

    // Track active format selection.
    if (base === "-f") {
      const value = tokens[i + 1];
      if (value && value.type !== "flag") {
        // ``-f X`` before any ``-i`` declares the input demuxer; after,
        // declares the output muxer for the next output file.
        ctx.format = {
          name: value.text,
          side: seenInput ? "muxer" : "demuxer",
        };
      }
    }

    out.set(token.id, resolveOne(token.normalizedText, ctx, index));

    if (token.normalizedText === "-i") {
      seenInput = true;
    }
  }

  return out;
}

/** Replaces the old ``shouldExpectValue`` — relies solely on metadata's
 * ``valueType`` field, no special-case ``NO_VALUE_OVERRIDES`` set needed
 * now that flags like ``-nostdin`` carry an explicit entry. */
export function shouldExpectValue(info: OptionInfo | null): boolean {
  if (!info) return true; // unknown flags assume "consumes next non-flag"
  return info.valueType !== "none";
}
