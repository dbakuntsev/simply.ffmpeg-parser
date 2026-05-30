import type {
  FiltersMetadata,
  MetadataBundle,
  NamedEntry,
} from "../types";

export type NamedLookup = Map<string, NamedEntry>;
export type FilterInfo = FiltersMetadata["filters"][number];

/** Name + alias → entry map for muxers/demuxers/protocols/bsfs. Used by the
 * selection popover (to look up entries for enrichment) and by diagnostics
 * (via ``.has()`` for existence checks). Keys are lowercased. */
export interface CatalogLookups {
  demuxers: NamedLookup;
  muxers: NamedLookup;
  protocols: NamedLookup;
  bsfs: NamedLookup;
}

/** Everything we precompute once per loaded metadata bundle. Built in
 * ``useMetadata`` and threaded through ``analyzeCommand`` and
 * ``buildSelectionInfo`` so the diagnostics pass and selection pass don't
 * each rebuild four ~500-entry Maps on every keystroke. */
export interface MetadataLookups {
  catalog: CatalogLookups;
  /** Lowercased codec names + aliases — separate from ``catalog`` because
   * codecs carry a different shape (typed encoder/decoder rather than
   * ``NamedEntry``). Used only for ``unknown-codec`` validation. */
  codecNames: Set<string>;
  /** Filter info by lowercased name or alias — consumed by the selection
   * popover when describing a filter step. */
  filtersByName: Map<string, FilterInfo>;
}

function buildNamedLookup(entries: NamedEntry[] | undefined): NamedLookup {
  const map: NamedLookup = new Map();
  for (const entry of entries ?? []) {
    map.set(entry.name.toLowerCase(), entry);
    for (const alias of entry.aliases || []) {
      const key = alias.toLowerCase();
      // Aliases never override a real name entry — preserves the semantics
      // both old call sites relied on (``buildLookup`` in selection.ts,
      // ``buildNameSet`` in diagnostics.ts).
      if (!map.has(key)) map.set(key, entry);
    }
  }
  return map;
}

function buildCodecNameSet(metadata: MetadataBundle): Set<string> {
  const set = new Set<string>();
  for (const codec of metadata.codecs?.codecs ?? []) {
    set.add(codec.name.toLowerCase());
    for (const alias of codec.aliases || []) set.add(alias.toLowerCase());
  }
  return set;
}

function buildFiltersByName(metadata: MetadataBundle): Map<string, FilterInfo> {
  const map = new Map<string, FilterInfo>();
  for (const filter of metadata.filters?.filters ?? []) {
    map.set(filter.name.toLowerCase(), filter);
    for (const alias of filter.aliases || []) {
      const key = alias.toLowerCase();
      if (!map.has(key)) map.set(key, filter);
    }
  }
  return map;
}

export function buildMetadataLookups(metadata: MetadataBundle): MetadataLookups {
  return {
    catalog: {
      demuxers: buildNamedLookup(metadata.demuxers?.demuxers),
      muxers: buildNamedLookup(metadata.muxers?.muxers),
      protocols: buildNamedLookup(metadata.protocols?.protocols),
      bsfs: buildNamedLookup(metadata.bitstreamFilters?.bitstream_filters),
    },
    codecNames: buildCodecNameSet(metadata),
    filtersByName: buildFiltersByName(metadata),
  };
}
