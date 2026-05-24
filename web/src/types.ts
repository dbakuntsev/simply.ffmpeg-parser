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
}

export interface InputNode {
  id: string;
  source: string;
  options: OptionBinding[];
}

export interface OutputNode {
  id: string;
  target: string;
  options: OptionBinding[];
}

export interface FilterArgument {
  key: string;
  value: string;
}

export interface FilterStep {
  name: string;
  args: FilterArgument[];
}

export interface FilterChain {
  id: string;
  label: string;
  filters: FilterStep[];
}

export interface FilterGraph {
  id: string;
  expression: string;
  chains?: FilterChain[];
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
}

export interface OptionsMetadata {
  options: Array<{
    name: string;
    aliases: string[];
    scope: "global" | "input" | "output";
    valueType: string;
    values: string[];
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

export interface CodecsMetadata {
  codecs: Array<{
    name: string;
    type: string;
    aliases: string[];
    encoder: boolean;
    decoder: boolean;
    /** HTML anchor for the codec's section in ``ffmpeg-all.html``. May be
     * empty for entries that only exist in ``allcodecs.c``. */
    anchor?: string;
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
}

export interface DemuxersMetadata {
  demuxers: NamedEntry[];
}

export interface MuxersMetadata {
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


