import { useEffect, useMemo, useState } from "react";
import { loadMetadata, loadVersionsCatalog } from "../metadata";
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

  return { versions, version, setVersion, metadata, versionTokens };
}
