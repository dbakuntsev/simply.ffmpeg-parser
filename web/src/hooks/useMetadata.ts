import { useEffect, useState } from "react";
import { listAvailableVersions, loadMetadata } from "../metadata";
import type { MetadataBundle } from "../types";

export function useMetadata() {
  const [versions, setVersions] = useState<string[]>([]);
  const [version, setVersion] = useState<string>("");
  const [metadata, setMetadata] = useState<MetadataBundle | null>(null);

  useEffect(() => {
    listAvailableVersions()
      .then((list) => {
        setVersions(list);
        setVersion(list[0] ?? "");
      })
      .catch(() => {
        setVersions([]);
        setVersion("");
      });
  }, []);

  useEffect(() => {
    if (!version) {
      return;
    }
    loadMetadata(version)
      .then(setMetadata)
      .catch(() => setMetadata(null));
  }, [version]);

  return { versions, version, setVersion, metadata };
}
