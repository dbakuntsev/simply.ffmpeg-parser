import { useCallback, useState } from "react";

export function useSelection() {
  const [selectedNode, setSelectedNode] = useState<string | null>(null);

  const select = useCallback((id: string) => {
    setSelectedNode(id);
  }, []);

  const clear = useCallback(() => {
    setSelectedNode(null);
  }, []);

  return { selectedNode, select, clear };
}
