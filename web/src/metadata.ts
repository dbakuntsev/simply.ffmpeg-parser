import type { MetadataBundle, OptionsMetadata } from "./types";

export async function loadMetadata(version: string): Promise<MetadataBundle> {
  const base = `./metadata/ffmpeg/${version}`;
  const index = await fetchJson(`${base}/index.json`);
  const options = await fetchJson(`${base}/${index.options}`);
  const codecs = await fetchJson(`${base}/${index.codecs}`);
  const filters = await fetchJson(`${base}/${index.filters}`);
  // The value-lookup bundles are optional: bundles produced before the
  // extractor learned to emit them won't advertise the keys in index.json.
  // Fall back to empty catalogs so the SPA can still render and the popover
  // simply doesn't surface a value-level description for those versions.
  const demuxers = index.demuxers
    ? await fetchJson(`${base}/${index.demuxers}`)
    : { demuxers: [] };
  const muxers = index.muxers
    ? await fetchJson(`${base}/${index.muxers}`)
    : { muxers: [] };
  const protocols = index.protocols
    ? await fetchJson(`${base}/${index.protocols}`)
    : { protocols: [] };
  const bitstreamFilters = index.bitstream_filters
    ? await fetchJson(`${base}/${index.bitstream_filters}`)
    : { bitstream_filters: [] };
  return {
    index,
    options,
    codecs,
    filters,
    demuxers,
    muxers,
    protocols,
    bitstreamFilters,
  } as MetadataBundle;
}

export function listAvailableVersions(): Promise<string[]> {
  return fetchJson("./metadata/ffmpeg/index.json").then((data) => data.versions.toSorted((a, b) => b - a) as string[]);
}

export function buildOptionLookup(metadata: OptionsMetadata): Map<string, OptionsMetadata["options"][number]> {
  const map = new Map<string, OptionsMetadata["options"][number]>();
  for (const option of metadata.options) {
    map.set(option.name, option);
    for (const alias of option.aliases || []) {
      map.set(alias, option);
    }
  }
  return map;
}

async function fetchJson(path: string): Promise<any> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`Failed to load ${path}`);
  }
  return response.json();
}
