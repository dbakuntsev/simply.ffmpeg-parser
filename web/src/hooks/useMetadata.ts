import { useEffect, useMemo, useState } from "react";
import { loadMetadata, loadVersionsCatalog } from "../metadata";
import { buildMetadataLookups, MetadataLookups } from "../parser";
import type { CacheTokens, MetadataBundle, VersionCacheTokens } from "../types";

export function useMetadata() {
  const [versions, setVersions] = useState<string[]>([]);
  const [version, setVersion] = useState<string>("");
  const [tokens, setTokens] = useState<CacheTokens>({});
  const [metadata, setMetadata] = useState<MetadataBundle | null>(null);

  useEffect(() => {
    loadVersionsCatalog()
      .then(({ versions: list, tokens: tokenMap }) => {
        setVersions(list);
        setTokens(tokenMap);
        setVersion(list[0] ?? "");
      })
      .catch(() => {
        setVersions([]);
        setTokens({});
        setVersion("");
      });
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
