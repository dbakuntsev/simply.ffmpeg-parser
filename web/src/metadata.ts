import type {
  CacheTokens,
  MetadataBundle,
  OptionsMetadata,
  VersionCacheTokens,
  VersionsIndex,
} from "./types";

export interface VersionsCatalog {
  versions: string[];
  tokens: CacheTokens;
}

export async function loadMetadata(
  version: string,
  tokens?: VersionCacheTokens
): Promise<MetadataBundle> {
  const base = `./metadata/ffmpeg/${version}`;
  const metadataTokens = tokens?.metadata;
  const fetchVersioned = (filename: string) =>
    fetchJson(withVersionQuery(`${base}/${filename}`, metadataTokens?.[filename]));

  const index = await fetchVersioned("index.json");
  const options = await fetchVersioned(index.options);
  // codecs.json and muxers.json grew sibling option arrays (``codec_options``,
  // ``format_options``) in the S3 extractor pass. Older bundles on disk lack
  // those keys; the type definitions mark them optional so consumers must
  // tolerate absence — no normalization needed here.
  const codecs = await fetchVersioned(index.codecs);
  const filters = await fetchVersioned(index.filters);
  // The value-lookup bundles are optional: bundles produced before the
  // extractor learned to emit them won't advertise the keys in index.json.
  // Fall back to empty catalogs so the SPA can still render and the popover
  // simply doesn't surface a value-level description for those versions.
  const demuxers = index.demuxers
    ? await fetchVersioned(index.demuxers)
    : { demuxers: [] };
  const muxers = index.muxers
    ? await fetchVersioned(index.muxers)
    : { muxers: [] };
  const protocols = index.protocols
    ? await fetchVersioned(index.protocols)
    : { protocols: [] };
  const bitstreamFilters = index.bitstream_filters
    ? await fetchVersioned(index.bitstream_filters)
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

export async function loadVersionsCatalog(): Promise<VersionsCatalog> {
  // The top-level index carries the cache-buster token map for every other
  // file under metadata/ and doc/. Fetch it uncached so a stale CDN copy
  // can't keep us pointing at hashes that no longer exist on disk; every
  // hashed file it references is then safe to serve from the cache.
  const data = (await fetchJson("./metadata/ffmpeg/index.json", {
    cache: "no-store",
  })) as VersionsIndex;
  const versions = (data.versions ?? []).slice().sort((a, b) => compareVersions(b, a));
  return { versions, tokens: data.tokens ?? {} };
}

/** @deprecated retained for callers that only need the version list. */
export function listAvailableVersions(): Promise<string[]> {
  return loadVersionsCatalog().then((c) => c.versions);
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

/** Append ``?v=<token>`` to a URL when a token is provided. Falls through to
 * the bare URL otherwise, which keeps the SPA working against older bundles
 * (or local dev) that ship without a token map. */
export function withVersionQuery(url: string, token: string | undefined): string {
  if (!token) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}v=${encodeURIComponent(token)}`;
}

async function fetchJson(path: string, init?: RequestInit): Promise<any> {
  const response = await fetch(path, init);
  if (!response.ok) {
    throw new Error(`Failed to load ${path}`);
  }
  return response.json();
}

function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map((n) => parseInt(n, 10));
  const pb = b.split(".").map((n) => parseInt(n, 10));
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}
