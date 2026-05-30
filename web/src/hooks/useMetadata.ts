import { useCallback, useEffect, useMemo, useState } from "react";
import { loadMetadata, loadVersionsCatalog } from "../metadata";
import { buildMetadataLookups, MetadataLookups } from "../parser";
import type { CacheTokens, MetadataBundle, VersionCacheTokens } from "../types";

const VERSION_STORAGE_KEY = "ffmpeg-parser:version";

function readStoredVersion(): string {
  try {
    return window.localStorage.getItem(VERSION_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function useMetadata() {
  const [versions, setVersions] = useState<string[]>([]);
  const [version, setVersionState] = useState<string>("");
  const [tokens, setTokens] = useState<CacheTokens>({});
  const [metadata, setMetadata] = useState<MetadataBundle | null>(null);

  useEffect(() => {
    loadVersionsCatalog()
      .then(({ versions: list, tokens: tokenMap }) => {
        setVersions(list);
        setTokens(tokenMap);
        const stored = readStoredVersion();
        setVersionState(stored && list.includes(stored) ? stored : list[0] ?? "");
      })
      .catch(() => {
        setVersions([]);
        setTokens({});
        setVersionState("");
      });
  }, []);

  const setVersion = useCallback((next: string) => {
    setVersionState(next);
    try {
      if (next) window.localStorage.setItem(VERSION_STORAGE_KEY, next);
      else window.localStorage.removeItem(VERSION_STORAGE_KEY);
    } catch {
      // ignore storage errors (private mode, quota, etc.)
    }
  }, []);

  useEffect(() => {
    if (!version) {
      return;
    }
    loadMetadata(version, tokens[version])
      .then(setMetadata)
      .catch(() => setMetadata(null));
  }, [version, tokens]);

  const versionTokens: VersionCacheTokens | undefined = useMemo(
    () => (version ? tokens[version] : undefined),
    [version, tokens]
  );

  // Precompute name/alias lookups once per loaded metadata bundle. Threaded
  // into ``analyzeCommand`` (for diagnostics' existence checks) and
  // ``buildSelectionInfo`` (for popover enrichment) so they don't rebuild
  // four ~500-entry Maps on every keystroke.
  const lookups: MetadataLookups | null = useMemo(
    () => (metadata ? buildMetadataLookups(metadata) : null),
    [metadata]
  );

  return { versions, version, setVersion, metadata, lookups, versionTokens };
}
