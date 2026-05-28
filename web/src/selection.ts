import type { MetadataBundle, NamedEntry, OptionBinding, VersionCacheTokens } from "./types";
import type { ParseResult, ResolvedOption } from "./parser";
import { splitStreamSpecifier } from "./parser";
import { withVersionQuery } from "./metadata";

const DOC_ENTRY_FILENAME = "ffmpeg-all.html";

export type SelectionDetailItem = { label: string; value: string };

export type SelectionDocLink = { label: string; url: string };

export type SelectionValueRow = { name: string; description: string };

export type SelectionInfo = {
  title: string;
  /** Backward-compatible plain text body. */
  detail: string;
  /** Structured fields rendered as a definition list. */
  fields?: SelectionDetailItem[];
  /** Free-form description paragraphs. */
  description?: string[];
  /** Documented named values (enum/flag tokens) plus the short help text
   * sourced from libavcodec / libavformat ``AV_OPT_TYPE_CONST`` rows.
   * Rendered as a definition-list table under the description. Omitted
   * when the option carries no enumerated values. */
  values?: SelectionValueRow[];
  /** Optional reference URL for the FFmpeg docs. */
  docsUrl?: string;
  /** Additional context-specific doc links (e.g. the demuxer section for
   * ``-f concat``). Rendered alongside the primary ``docsUrl``. */
  extraDocs?: SelectionDocLink[];
};

type OptionInfo = MetadataBundle["options"]["options"][number];
type FilterInfo = MetadataBundle["filters"]["filters"][number];

type OptionExplanation = {
  title: string;
  lines: string[];
};

const OPTION_EXPLANATIONS: Record<string, OptionExplanation> = {
  "-crf": {
    title: "Constant Rate Factor",
    lines: [
      "Controls perceptual video quality for libx264/libx265.",
      "Range: 0–51",
      "Lower = better quality",
      "Typical values: 18–28",
    ],
  },
  "-preset": {
    title: "Encoding Preset",
    lines: [
      "Controls encoder speed vs compression efficiency.",
      "Slower = better compression, larger CPU cost.",
    ],
  },
  "-b": {
    title: "Bitrate",
    lines: ["Sets target bitrate for the selected stream.", "Example: 192k, 2M."],
  },
  "-r": {
    title: "Frame Rate",
    lines: ["Sets output frame rate for the selected stream."],
  },
  "-movflags": {
    title: "MOV Flags",
    lines: ["Sets MOV/MP4 container flags.", "Example: +faststart moves moov atom to the front."],
  },
  "-c": {
    title: "Codec Selection",
    lines: ["Selects codec for the given stream.", "Use -c:v or -c:a for specific streams."],
  },
  "-map": {
    title: "Stream Mapping",
    lines: ["Selects which input streams go to which outputs."],
  },
  "-vf": {
    title: "Video Filters",
    lines: ["Applies a video filter chain to the selected stream."],
  },
  "-af": {
    title: "Audio Filters",
    lines: ["Applies an audio filter chain to the selected stream."],
  },
  "-filter_complex": {
    title: "Complex Filtergraph",
    lines: ["Defines a complex filtergraph with multiple inputs/outputs and labels."],
  },
};

// Texinfo encodes non-alphanumeric characters in anchor IDs as `_XXXX` where
// XXXX is the 4-digit hex ASCII code. Hyphens are preserved.
function texinfoAnchor(s: string) {
  return s.replace(/[^A-Za-z0-9-]/g, (c) => `_${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

function docsBase(version: string, docTokens?: Record<string, string>) {
  if (!version) return "";
  const url = `./doc/ffmpeg/${encodeURIComponent(version)}/${DOC_ENTRY_FILENAME}`;
  return withVersionQuery(url, docTokens?.[DOC_ENTRY_FILENAME]);
}

function docsUrlForOption(
  version: string,
  optionInfo: OptionInfo | undefined,
  docTokens?: Record<string, string>
) {
  const base = docsBase(version, docTokens);
  if (!base) return undefined;
  // The extractor records each option's section anchor (or the explicit
  // ``@anchor{}`` immediately preceding the @item, when present). Use it
  // directly: this lights up `#Main-options` for `-c`/`-i`/`-map`/…, and
  // `#filter_005foption` etc. for the few options with their own anchor.
  // Empty anchor means we couldn't resolve a section (older bundle, or an
  // option that came from a fallback path); fall back to the page itself.
  const anchor = optionInfo?.anchor;
  if (anchor) return `${base}#${anchor}`;
  return base;
}

function docsUrlForFilter(
  version: string,
  name: string,
  docTokens?: Record<string, string>
) {
  const base = docsBase(version, docTokens);
  if (!base) return undefined;
  return `${base}#${texinfoAnchor(name.toLowerCase())}`;
}

function formatScope(scope: string) {
  if (scope === "input") return "Input-level option";
  if (scope === "output") return "Output-level option";
  if (scope === "global") return "Global option";
  return scope;
}

function describeStreamSpecifier(specifier: string | null | undefined) {
  if (!specifier) return null;
  if (specifier.startsWith("v")) return "Applies to video streams.";
  if (specifier.startsWith("a")) return "Applies to audio streams.";
  if (specifier.startsWith("s")) return "Applies to subtitle streams.";
  if (specifier.startsWith("d")) return "Applies to data streams.";
  return `Applies to stream specifier: ${specifier}.`;
}

type ValueEnrichment = {
  /** Extra paragraphs to append to the option's description. */
  paragraphs: string[];
  /** Additional documentation link (different anchor than the option's own). */
  docLink?: SelectionDocLink;
};

function buildLookup(entries: NamedEntry[]): Map<string, NamedEntry> {
  const map = new Map<string, NamedEntry>();
  for (const entry of entries) {
    map.set(entry.name.toLowerCase(), entry);
    for (const alias of entry.aliases || []) {
      const key = alias.toLowerCase();
      if (!map.has(key)) map.set(key, entry);
    }
  }
  return map;
}

function describeNamedEntry(
  entry: NamedEntry,
  label: string,
  version: string,
  value: string,
  docTokens?: Record<string, string>
): ValueEnrichment {
  const docsBaseUrl = docsBase(version, docTokens);
  const docLink: SelectionDocLink | undefined = docsBaseUrl
    ? {
        label: `${label}: ${value}`,
        url: `${docsBaseUrl}#${entry.anchor}`,
      }
    : undefined;
  // Lead the appended block with a heading so the reader can see where the
  // popover transitions from "what this option does" to "what this value
  // means", then include the entry's description verbatim. Empty descriptions
  // still surface the doc link, which is the more important payload.
  const paragraphs: string[] = [`**${label}: \`${value}\`**`];
  if (entry.description.length) paragraphs.push(...entry.description);
  return { paragraphs, docLink };
}

const PROTOCOL_SCHEME_RE = /^([a-z][a-z0-9+.\-]*):(?:\/\/|[^/])/i;

function enrichOptionValue(
  flag: string,
  values: string[],
  scope: "global" | "input" | "output",
  metadata: MetadataBundle,
  lookups: CatalogLookups,
  version: string,
  docTokens?: Record<string, string>
): ValueEnrichment | undefined {
  if (!values.length) return undefined;
  const value = values[0];
  const lowerValue = value.toLowerCase();
  const { base } = splitStreamSpecifier(flag.toLowerCase());

  // -f <fmt>: demuxer on input, muxer on output (and either on globals when
  // the option somehow flushed to globals).
  if (base === "-f") {
    if (scope === "input") {
      const entry = lookups.demuxers.get(lowerValue);
      if (entry) return describeNamedEntry(entry, "Demuxer", version, value, docTokens);
    } else if (scope === "output") {
      const entry = lookups.muxers.get(lowerValue);
      if (entry) return describeNamedEntry(entry, "Muxer", version, value, docTokens);
    } else {
      const entry =
        lookups.demuxers.get(lowerValue) ?? lookups.muxers.get(lowerValue);
      if (entry)
        return describeNamedEntry(
          entry,
          lookups.demuxers.has(lowerValue) ? "Demuxer" : "Muxer",
          version,
          value,
          docTokens
        );
    }
  }

  if (base === "-bsf") {
    // The value can be a comma-separated list; describe only the first
    // entry, which keeps the popover focused. The remaining bsfs are
    // selectable in their own popovers once the tokenizer learns to split
    // them (not in this change).
    const first = value.split(",")[0].trim().split("=")[0];
    const entry = lookups.bsfs.get(first.toLowerCase());
    if (entry) return describeNamedEntry(entry, "Bitstream filter", version, first, docTokens);
  }

  if (base === "-c" || base === "-codec" || base === "-vcodec" || base === "-acodec" || base === "-scodec") {
    const codec = metadata.codecs.codecs.find(
      (c) => c.name.toLowerCase() === lowerValue
    );
    if (codec) {
      const role: string[] = [];
      if (codec.encoder) role.push("encoder");
      if (codec.decoder) role.push("decoder");
      const label = `Codec${role.length ? ` (${role.join("/")})` : ""}`;
      // Codecs don't have a free-form description in the bundle, so the link
      // is the real payload. The extractor records the section anchor that
      // matches the texinfo encoding makeinfo emits in ffmpeg-all.html —
      // including multi-name sections like "libx264, libx264rgb" ⇒
      // ``libx264_002c-libx264rgb``. Codecs that only exist in allcodecs.c
      // have an empty anchor; for those, link to the page itself.
      const docsBaseUrl = docsBase(version, docTokens);
      const url = docsBaseUrl
        ? codec.anchor
          ? `${docsBaseUrl}#${codec.anchor}`
          : docsBaseUrl
        : undefined;
      return {
        paragraphs: [`**${label}: \`${value}\`** (${codec.type})`],
        docLink: url
          ? { label: `Codec: ${codec.name}`, url }
          : undefined,
      };
    }
  }

  return undefined;
}

function enrichInputProtocol(
  source: string,
  lookups: CatalogLookups,
  version: string,
  docTokens?: Record<string, string>
): ValueEnrichment | undefined {
  const match = PROTOCOL_SCHEME_RE.exec(source);
  if (!match) return undefined;
  const scheme = match[1].toLowerCase();
  // ``file:`` is a real protocol with its own docs section; everything else
  // that doesn't resolve probably isn't documented (e.g. a custom device).
  const entry = lookups.protocols.get(scheme);
  if (!entry) return undefined;
  return describeNamedEntry(entry, "Protocol", version, scheme, docTokens);
}

type CatalogLookups = {
  demuxers: Map<string, NamedEntry>;
  muxers: Map<string, NamedEntry>;
  protocols: Map<string, NamedEntry>;
  bsfs: Map<string, NamedEntry>;
};

/** Human-readable label for the layer the resolver picked the option from.
 * Appears in the "Source" field of the selection popover so the user knows
 * whether they're looking at a driver option, a codec-private flag, etc. */
function formatResolutionSource(binding: OptionBinding): string | null {
  switch (binding.resolutionSource) {
    case "driver":
      return "Driver option (ffmpeg.texi)";
    case "codec-private":
      return binding.matchedCodec
        ? `Codec-private (${binding.matchedCodec})`
        : "Codec-private option";
    case "codec-generic":
      return "Generic AVCodec option";
    case "format-private":
      return binding.matchedFormat
        ? `Format-private (${binding.matchedFormat})`
        : "Format-private option";
    case "format-generic":
      return "Generic AVFormat option";
    case "unknown":
      return null;
    default:
      return null;
  }
}

function buildOptionSelection(
  scopeLabel: string,
  scope: "global" | "input" | "output",
  binding: OptionBinding,
  resolved: Map<string, ResolvedOption | null>,
  metadata: MetadataBundle,
  lookups: CatalogLookups,
  version: string,
  docTokens?: Record<string, string>
): SelectionInfo {
  const flag = binding.flag;
  const values = binding.values;
  const { base, specifier } = splitStreamSpecifier(flag.toLowerCase());
  // Prefer the layered-resolver result keyed by the flag's token id; this is
  // what carries codec-private / format-private metadata that the
  // metadata.options pool by itself doesn't surface. Falls back to undefined
  // when the resolver couldn't classify the flag (unknown-option case).
  const resolution = resolved.get(binding.tokenIds[0]) ?? null;
  const optionInfo = resolution?.info;
  const explanation = OPTION_EXPLANATIONS[base];

  const valueStr = values.length ? values.join(" ") : "(no value)";
  const fields: SelectionDetailItem[] = [
    // "As written" is what's in the command, "Signature" is the documented
    // grammar (e.g. ``-map [-]input_file_id[:stream_specifier]...``). The
    // description text only makes sense alongside the grammar — without it,
    // the reader sees prose about ``input_file_id``/``stream_specifier``
    // tokens with no place to anchor them to.
    { label: "As written", value: `${flag} ${valueStr}`.trim() },
  ];
  if (optionInfo?.signature && optionInfo.signature.length) {
    fields.push({
      label: "Signature",
      value: optionInfo.signature.join("\n"),
    });
  }
  fields.push(
    { label: "Scope", value: optionInfo ? formatScope(optionInfo.scope) : scopeLabel },
    {
      label: "Value type",
      value: optionInfo?.valueType && optionInfo.valueType !== "none" ? optionInfo.valueType : values.length ? "string" : "none",
    },
  );
  const sourceLabel = formatResolutionSource(binding);
  if (sourceLabel) {
    fields.push({ label: "Source", value: sourceLabel });
  }

  const specLine = describeStreamSpecifier(specifier ?? binding.inferredStreamType ?? null);
  if (specLine) fields.push({ label: "Stream", value: specLine });

  const description: string[] = [];
  if (optionInfo && optionInfo.description.length) description.push(...optionInfo.description);
  else if (explanation) description.push(...explanation.lines);

  const enrichment = enrichOptionValue(flag, values, scope, metadata, lookups, version, docTokens);
  if (enrichment) description.push(...enrichment.paragraphs);

  const title = optionInfo?.name ?? explanation?.title ?? flag;
  const extraDocs: SelectionDocLink[] = [];
  if (enrichment?.docLink) extraDocs.push(enrichment.docLink);

  // x264 reference link — surfaces only when the resolver attributed
  // this option to a libx264-family codec AND the bundle advertises a
  // rendered x264 reference page. The page's per-option anchors come
  // from the same bare names we use elsewhere ( ``crf`` from ``-crf`` ),
  // so the deep link works without any further coordination.
  const x264DocPath = metadata.index?.x264_doc;
  if (
    x264DocPath &&
    binding.resolutionSource === "codec-private" &&
    binding.matchedCodec &&
    /^libx264/.test(binding.matchedCodec)
  ) {
    extraDocs.push({
      label: "x264 reference",
      url: `./${x264DocPath}#option-${encodeURIComponent(base.slice(1))}`,
    });
  }

  // Surface the option's documented values (e.g. ``-fflags`` flag names,
  // ``-nal-hrd`` enum tokens) paired with their C-source help text where
  // available. Empty for options whose value is a free-form string/number.
  let valueRows: SelectionValueRow[] | undefined;
  if (optionInfo?.values && optionInfo.values.length > 0) {
    const descs = optionInfo.valueDescriptions ?? [];
    valueRows = optionInfo.values.map((name, i) => ({
      name,
      description: descs[i] ?? "",
    }));
  }

  return {
    title,
    detail: [`${flag} ${valueStr}`.trim(), ...description].join("\n"),
    fields,
    description,
    values: valueRows,
    docsUrl: docsUrlForOption(version, optionInfo, docTokens),
    extraDocs: extraDocs.length ? extraDocs : undefined,
  };
}

export function buildSelectionInfo(
  analysis: ParseResult,
  metadata: MetadataBundle,
  version: string,
  versionTokens?: VersionCacheTokens
) {
  const docTokens = versionTokens?.doc;
  const info = new Map<string, SelectionInfo>();
  const resolved = analysis.resolved;
  const filterLookup = new Map<string, FilterInfo>();
  metadata.filters.filters.forEach((filter) => {
    filterLookup.set(filter.name, filter);
    filter.aliases.forEach((alias) => filterLookup.set(alias, filter));
  });

  const lookups: CatalogLookups = {
    demuxers: buildLookup(metadata.demuxers?.demuxers ?? []),
    muxers: buildLookup(metadata.muxers?.muxers ?? []),
    protocols: buildLookup(metadata.protocols?.protocols ?? []),
    bsfs: buildLookup(metadata.bitstreamFilters?.bitstream_filters ?? []),
  };

  analysis.semantic.inputs.forEach((input, index) => {
    const fields: SelectionDetailItem[] = [
      { label: "Source", value: input.source },
      { label: "Index", value: String(index) },
    ];
    const description: string[] = [];
    let extraDocs: SelectionDocLink[] | undefined;
    const protocolEnrichment = enrichInputProtocol(input.source, lookups, version, docTokens);
    if (protocolEnrichment) {
      description.push(...protocolEnrichment.paragraphs);
      if (protocolEnrichment.docLink) extraDocs = [protocolEnrichment.docLink];
    }
    const sel: SelectionInfo = {
      title: `Input ${index + 1}`,
      detail: `Source: ${input.source}`,
      fields,
      description,
      extraDocs,
    };
    info.set(input.id, sel);
    info.set(`input_${index}`, sel);
    input.options.forEach((opt) => {
      info.set(
        opt.id,
        buildOptionSelection(
          "Input-level option",
          "input",
          opt,
          resolved,
          metadata,
          lookups,
          version,
          docTokens
        )
      );
    });
  });

  analysis.semantic.outputs.forEach((output, index) => {
    const sel: SelectionInfo = {
      title: `Output ${index + 1}`,
      detail: `Target: ${output.target}`,
      fields: [
        { label: "Target", value: output.target },
        { label: "Index", value: String(index) },
      ],
    };
    info.set(output.id, sel);
    info.set(`output_${index}`, sel);
    output.options.forEach((opt) => {
      info.set(
        opt.id,
        buildOptionSelection(
          "Output-level option",
          "output",
          opt,
          resolved,
          metadata,
          lookups,
          version,
          docTokens
        )
      );
    });
  });

  analysis.semantic.globals.forEach((opt) => {
    info.set(
      opt.id,
      buildOptionSelection(
        "Global option",
        "global",
        opt,
        resolved,
        metadata,
        lookups,
        version,
        docTokens
      )
    );
  });

  analysis.semantic.filters.forEach((filter, filterIndex) => {
    const graphSel: SelectionInfo = {
      title: "Filter Graph",
      detail: buildFilterGraphExplanation(filterIndex + 1, filter),
      fields: [
        { label: "Chains", value: String(filter.chains?.length ?? 1) },
        { label: "Expression length", value: `${filter.expression.length} chars` },
      ],
      description: ["Complex filtergraph."],
    };
    info.set(filter.id, graphSel);

    if (filter.chains) {
      filter.chains.forEach((chain, chainIndex) => {
        const chainSel: SelectionInfo = {
          title: `Filter Chain ${chainIndex + 1}`,
          detail: buildFilterChainExplanation(chain),
          fields: [
            { label: "Steps", value: String(chain.filters.length) },
            ...(chain.label ? [{ label: "Labels", value: chain.label }] : []),
          ],
        };
        info.set(chain.id, chainSel);
        info.set(`${filter.id}_chain_${chainIndex}`, chainSel);

        chain.filters.forEach((step, stepIndex) => {
          const stepId = `${chain.id}_step_${stepIndex}`;
          const filterInfo = filterLookup.get(step.name.toLowerCase());
          const fields: SelectionDetailItem[] = [
            { label: "Filter", value: step.name },
            { label: "Arguments", value: String(step.args.length) },
          ];
          if (filterInfo) {
            fields.push({ label: "Type", value: filterInfo.type });
          }
          const description = filterInfo?.description?.length ? [...filterInfo.description] : [];
          info.set(stepId, {
            title: filterInfo?.name ?? step.name,
            detail: buildFilterStepExplanation(step.name, step.args, filterInfo),
            fields,
            description,
            docsUrl: docsUrlForFilter(version, step.name, docTokens),
          });
          step.args.forEach((arg, argIndex) => {
            const argDescription = buildFilterArgExplanation(arg.key, filterInfo);
            info.set(`${stepId}_arg_${argIndex}`, {
              title: `${step.name} · ${arg.key}`,
              detail: argDescription ? `${arg.value}\n${argDescription}` : arg.value,
              fields: [
                { label: "Key", value: arg.key },
                { label: "Value", value: arg.value },
              ],
              description: argDescription ? [argDescription] : [],
              docsUrl: docsUrlForFilter(version, step.name, docTokens),
            });
          });
        });
      });
    }
  });

  return info;
}

function buildFilterGraphExplanation(
  _index: number,
  filter: { expression: string; chains?: { id: string; label: string; filters: any[] }[] }
) {
  const lines = ["Complex filtergraph."];
  if (filter.chains && filter.chains.length > 0) {
    lines.push(`Chains: ${filter.chains.length}`);
  }
  lines.push(`Expression length: ${filter.expression.length} characters.`);
  return lines.join("\n");
}

function buildFilterChainExplanation(chain: {
  label: string;
  filters: { name: string; args: { key: string; value: string }[] }[];
}) {
  const lines = ["Filter chain within the complex graph."];
  if (chain.label) {
    lines.push(`Labels: ${chain.label}`);
  }
  lines.push(`Steps: ${chain.filters.length}`);
  return lines.join("\n");
}

function buildFilterStepExplanation(
  name: string,
  args: { key: string; value: string }[],
  filterInfo: FilterInfo | undefined
) {
  const lines: string[] = [];
  if (filterInfo && filterInfo.description.length > 0) {
    lines.push(filterInfo.name);
    lines.push(...filterInfo.description);
  } else {
    lines.push(`Filter: ${name}.`);
  }
  if (args.length === 0) {
    lines.push("No arguments provided.");
    return lines.join("\n");
  }
  lines.push(`Arguments: ${args.length}`);
  args.forEach((arg) => {
    const argDetail = buildFilterArgExplanation(arg.key, filterInfo);
    lines.push(argDetail ? `${arg.key} = ${arg.value} (${argDetail})` : `${arg.key} = ${arg.value}`);
  });
  return lines.join("\n");
}

function buildFilterArgExplanation(argKey: string, filterInfo: FilterInfo | undefined) {
  if (!filterInfo) return "";
  const detail = filterInfo.args[argKey];
  if (!detail || detail.length === 0) return "";
  return detail.join(" ");
}
