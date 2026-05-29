export type TokenType = "executable" | "flag" | "value" | "input" | "output" | "filter";

export interface Token {
  id: string;
  type: TokenType;
  text: string;
  normalizedText: string;
  position: number;
  sourceRange: { start: number; end: number };
}

export interface OptionBinding {
  id: string;
  flag: string;
  values: string[];
  scope: "global" | "input" | "output";
  inputIndex: number | null;
  outputIndex: number | null;
  tokenIds: string[];
  /** Where the resolver found this option's metadata. ``"unknown"`` when the
   * flag matched nothing in any layer (a diagnostic surfaces as
   * ``unknown-option``). Used by the selection popover to label whether the
   * binding came from a codec-private table vs the driver layer. */
  resolutionSource?:
    | "driver"
    | "codec-private"
    | "codec-generic"
    | "format-private"
    | "format-generic"
    | "unknown";
  /** When ``resolutionSource`` is ``codec-private`` / ``codec-generic``,
   * the codec name and/or stream type the resolver attributed the option to.
   * Same for format-private — the muxer/demuxer that owned the option. */
  matchedCodec?: string;
  matchedFormat?: string;
  inferredStreamType?: "v" | "a" | "s";
}

export interface InputNode {
  id: string;
  source: string;
  options: OptionBinding[];
  /** Token id of the source filename/URL (the value after ``-i``), used to
   * highlight it in the command input when the input is selected. */
  tokenId?: string;
}

export interface OutputNode {
  id: string;
  target: string;
  options: OptionBinding[];
  /** Token id of the output target, used to highlight it in the command input. */
  tokenId?: string;
}

/** Character offsets within the ``-filter_complex`` / ``-vf`` / ``-af``
 * expression string (i.e. relative to the value token's de-quoted text, not
 * the raw command). ``sourceRanges.ts`` maps these to absolute command
 * positions for textarea highlighting. */
export interface FilterRange {
  start: number;
  end: number;
}

export interface FilterArgument {
  key: string;
  value: string;
  /** Offset of this ``key=value`` (or positional value) within the expression. */
  range?: FilterRange;
}

export interface FilterStep {
  name: string;
  args: FilterArgument[];
  /** Offset of the whole filter step (name + args) within the expression. */
  range?: FilterRange;
}

export interface FilterChain {
  id: string;
  label: string;
  filters: FilterStep[];
  /** Pad labels consumed by this chain (the leading ``[...]`` groups), with
   * the brackets stripped — e.g. ``["0:v", "1:v"]`` for
   * ``[0:v][1:v]overlay...``. A file pad (``"0:v"``) routes from an input;
   * a named pad (``"tmp"``) routes from the chain that produced it. Used by
   * the pipeline visualization for real edge routing. */
  inputPads?: string[];
  /** Pad labels produced by this chain (the trailing ``[...]`` groups). */
  outputPads?: string[];
  /** Offset of the whole chain within the expression. */
  range?: FilterRange;
}

export interface FilterGraph {
  id: string;
  expression: string;
  chains?: FilterChain[];
  /** Token id of the expression value, so the absolute source positions of
   * chain/step/arg ranges can be recovered for textarea highlighting. */
  valueTokenId?: string;
}

export interface SemanticCommand {
  executable: string | null;
  globals: OptionBinding[];
  inputs: InputNode[];
  outputs: OutputNode[];
  filters: FilterGraph[];
}

export interface Issue {
  id: string;
  severity: "error" | "warning" | "info";
  code: string;
  message: string;
  explanation: string;
  tokenIds: string[];
  scope: "global" | "input" | "output" | "filter";
  relatedIds: string[];
}

export interface MetadataIndex {
  version: string;
  /** Exact upstream git tag this bundle was extracted from (e.g. `n8.1.1`).
   * `version` is rolled up to `major.minor`; this preserves the patch.
   * Optional — bundles produced before the extractor emitted it lack it. */
  tag?: string;
  released: string;
  options: string;
  codecs: string;
  filters: string;
  // The extractor advertises these only when the corresponding category was
  // produced for this version. Older bundles on disk predate them and the
  // SPA must tolerate their absence.
  demuxers?: string;
  muxers?: string;
  protocols?: string;
  bitstream_filters?: string;
  /** Web-relative path to the standalone x264 reference HTML, keyed
   * per x264 commit so multiple FFmpeg versions pinning the same x264
   * commit share one rendered file. Example:
   * ``"doc/x264/0480cb05fa18/x264-reference.html"``. Absent when the
   * extractor wasn't run with ``--x264-repo``. The SPA's inspector
   * uses this to deep-link from libx264 options to ``#option-<name>``. */
  x264_doc?: string;
  /** Same as ``x264_doc`` but for libx265, keyed per x265 release tag
   * (``"doc/x265/4.2/x265-reference.html"``). Absent without
   * ``--x265-repo``. */
  x265_doc?: string;
}

export interface OptionsMetadata {
  options: Array<{
    name: string;
    aliases: string[];
    scope: "global" | "input" | "output";
    valueType: string;
    values: string[];
    /** Same length as ``values``; carries C-source help text for each
     * named value when available. Driver options (from ``ffmpeg.texi``)
     * never get this — only options bridged from the AVOption layer via
     * the resolver populate it. Absent on bundles produced before the
     * extractor learned to emit C-source value descriptions. */
    valueDescriptions?: string[];
    requires: string[];
    conflicts: string[];
    description: string[];
    /** HTML anchor in ``ffmpeg-all.html`` (e.g. ``Main-options`` or
     * ``filter_005foption``). May be empty for very old bundles. */
    anchor?: string;
    /** Documented invocation form(s) with @var/@emph stripped — e.g.
     * ``["-map [-]input_file_id[:stream_specifier]... (output)"]``. One
     * entry per @item/@itemx in the texi. May be missing on older bundles. */
    signature?: string[];
  }>;
}

/** A generic AVCodec or AVFormat option, applicable on the command line
 * when a matching codec/format is selected upstream (e.g. ``-b 5M`` after
 * ``-c:v libx264``, or ``-fflags +genpts`` near an ``-i input.mp4``).
 *
 * Distinct from :class:`OptionsMetadata` entries because the option's scope
 * isn't ``global``/``input``/``output`` but is derived from ``roles``:
 *  - AVCodec ``roles`` are drawn from
 *    ``{encoding, decoding, audio, video, subtitle}``.
 *  - AVFormat ``roles`` are drawn from ``{input, output}``.
 * Empty ``roles`` means the documentation didn't tag the entry (option
 * applies broadly). */
export interface AVOptionEntry {
  name: string;
  aliases: string[];
  valueType: string;
  values: string[];
  /** Same length as ``values``; index i is a short help string for
   * ``values[i]``. Sourced from ``AV_OPT_TYPE_CONST`` help text in the
   * libavcodec / libavformat C source. ``""`` when the source had no help
   * for that value. Absent on bundles produced before the extractor
   * learned to emit C-source value descriptions. */
  valueDescriptions?: string[];
  description: string[];
  anchor?: string;
  signature?: string[];
  roles: string[];
}

export interface CodecsMetadata {
  /** Generic AVCodec options harvested from ``codecs.texi``'s
   * "Codec Options" chapter. Apply whenever any codec is in scope. Absent
   * (empty array) on bundles produced before the extractor learned to emit
   * this layer. */
  codec_options?: AVOptionEntry[];
  codecs: Array<{
    name: string;
    type: string;
    aliases: string[];
    encoder: boolean;
    decoder: boolean;
    /** HTML anchor for the codec's section in ``ffmpeg-all.html``. May be
     * empty for entries that only exist in ``allcodecs.c``. */
    anchor?: string;
    /** Private options for this codec, harvested from ``encoders.texi`` /
     * ``decoders.texi``. Each entry's ``roles`` field contains ``"encoder"``
     * and/or ``"decoder"`` so the SPA can pick the right set based on which
     * side of the pipeline ``-c[:type]`` selected it. Absent on bundles that
     * predate the per-codec extractor pass. */
    options?: AVOptionEntry[];
  }>;
}

export interface FiltersMetadata {
  filters: Array<{
    name: string;
    type: string;
    aliases: string[];
    params: string[];
    description: string[];
    args: Record<string, string[]>;
  }>;
}

export interface NamedEntry {
  name: string;
  aliases: string[];
  anchor: string;
  description: string[];
  /** Private options for this muxer / demuxer, harvested from
   * ``muxers.texi`` / ``demuxers.texi``. Each entry's ``roles`` field
   * contains ``"muxer"`` or ``"demuxer"`` so the SPA can pick the right
   * set per pipeline side. Currently only populated for muxers and
   * demuxers — protocols and bitstream filters reuse this type but always
   * emit an empty list. Absent on bundles that predate the per-format
   * extractor pass. */
  options?: AVOptionEntry[];
}

export interface DemuxersMetadata {
  demuxers: NamedEntry[];
}

export interface MuxersMetadata {
  /** Generic AVFormat options harvested from ``formats.texi``'s
   * "Format Options" chapter. Apply whenever any muxer/demuxer is in scope.
   * Lives on ``muxers.json`` (single home for both sides) — the SPA reads
   * it for both input-side and output-side lookups. Absent on bundles
   * produced before the extractor learned to emit this layer. */
  format_options?: AVOptionEntry[];
  muxers: NamedEntry[];
}

export interface ProtocolsMetadata {
  protocols: NamedEntry[];
}

export interface BitstreamFiltersMetadata {
  bitstream_filters: NamedEntry[];
}

/** Cache-buster tokens emitted by the deploy workflow. Keyed by file name
 * within the version's metadata or doc directory; the value is a short hash
 * appended as ``?v=<token>`` to defeat GitHub Pages' aggressive caching.
 * Missing keys / absent tokens fall back to a bare URL — older deployments
 * never carried these. */
export interface VersionCacheTokens {
  metadata?: Record<string, string>;
  doc?: Record<string, string>;
}

export type CacheTokens = Record<string, VersionCacheTokens>;

export interface VersionsIndex {
  versions: string[];
  tokens?: CacheTokens;
}

export interface MetadataBundle {
  index: MetadataIndex;
  options: OptionsMetadata;
  codecs: CodecsMetadata;
  filters: FiltersMetadata;
  // Value-level catalogs. Empty arrays when the bundle for this version
  // predates the extractor that produces them.
  demuxers: DemuxersMetadata;
  muxers: MuxersMetadata;
  protocols: ProtocolsMetadata;
  bitstreamFilters: BitstreamFiltersMetadata;
}


