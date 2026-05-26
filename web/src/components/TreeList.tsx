import { useState } from "react";

export type TreeNodeKind =
  | "globals"
  | "inputs"
  | "filters"
  | "outputs"
  | "input"
  | "filter"
  | "output"
  | "option"
  | "chain"
  | "step"
  | "arg"
  | "placeholder";

export type TreeNode = {
  id: string;
  label: string;
  kind?: TreeNodeKind;
  children?: TreeNode[];
};

type Props = {
  nodes: TreeNode[];
  selected: string | null;
  onSelect: (id: string) => void;
};

const KIND_ICON: Record<string, string> = {
  globals: "⚙",
  inputs: "▶",
  input: "▶",
  filters: "ƒ",
  filter: "ƒ",
  chain: "ƒ",
  step: "ƒ",
  outputs: "⤓",
  output: "⤓",
  option: "·",
  arg: "·",
};

const KIND_COLOR: Record<string, string> = {
  globals: "text-muted",
  inputs: "text-muted",
  input: "text-muted",
  filters: "text-muted",
  filter: "text-muted",
  chain: "text-muted",
  step: "text-muted",
  outputs: "text-muted",
  output: "text-muted",
  option: "text-muted",
  arg: "text-muted",
};

export function TreeList({ nodes, selected, onSelect }: Props) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const toggle = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <ul className="space-y-0.5 text-sm text-ink" role="tree">
      {nodes.map((node) => (
        <TreeItem
          key={node.id}
          node={node}
          selected={selected}
          onSelect={onSelect}
          collapsed={collapsed}
          onToggle={toggle}
          depth={0}
        />
      ))}
    </ul>
  );
}

function TreeItem({
  node,
  selected,
  onSelect,
  collapsed,
  onToggle,
  depth,
}: {
  node: TreeNode;
  selected: string | null;
  onSelect: (id: string) => void;
  collapsed: Set<string>;
  onToggle: (id: string) => void;
  depth: number;
}) {
  const hasChildren = !!node.children && node.children.length > 0;
  const isCollapsed = collapsed.has(node.id);
  const isSelected = selected === node.id;
  const kind = node.kind ?? "option";
  const icon = KIND_ICON[kind] ?? "·";
  const iconColor = KIND_COLOR[kind] ?? "text-muted";
  const isPlaceholder = kind === "placeholder";

  return (
    <li role="treeitem" aria-expanded={hasChildren ? !isCollapsed : undefined} aria-selected={isPlaceholder ? undefined : isSelected}>
      <div className="flex items-center">
        {hasChildren ? (
          <button
            type="button"
            aria-label={isCollapsed ? "Expand" : "Collapse"}
            className="mr-1 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-[3px] text-muted hover:bg-edge focus:outline-none focus:ring-2 focus:ring-blue-200"
            onClick={(e) => {
              e.stopPropagation();
              onToggle(node.id);
            }}
          >
            <span aria-hidden="true" className="text-[14px] leading-none">{isCollapsed ? "⊞" : "⊟"}</span>
          </button>
        ) : (
          <span aria-hidden="true" className="mr-1 inline-block h-5 w-5 shrink-0" />
        )}
        {isPlaceholder ? (
          // Non-interactive: rendered as a span, not a button, so it doesn't
          // receive focus, doesn't get hover affordance, and can't be
          // selected. Italic + lighter gray to read as an absence-of-data
          // marker rather than a real tree entry. The text itself stays in
          // the accessibility tree — "(none)" is informational for screen
          // readers — only mouse-side interactivity is suppressed.
          <span className="flex flex-1 items-center gap-2 px-2 py-1 text-left">
            <span className="font-mono text-[13px] italic text-muted/60 break-all">
              {node.label}
            </span>
          </span>
        ) : (
          <button
            type="button"
            aria-current={isSelected ? "true" : undefined}
            className={`flex flex-1 items-center gap-2 rounded-[3px] px-2 py-1 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-blue-200 ${
              isSelected
                ? "bg-blue-50 text-blue-800"
                : "hover:bg-blue-50/60"
            }`}
            onClick={() => onSelect(node.id)}
          >
            <span aria-hidden="true" className={`inline-flex h-4 w-4 shrink-0 items-center justify-center text-[12px] ${iconColor}`}>
              {icon}
            </span>
            <span className="font-mono text-[13px] break-all">{node.label}</span>
          </button>
        )}
      </div>
      {hasChildren && !isCollapsed && (
        <ul className="ml-2.5 mt-0.5 space-y-0.5 border-l border-edge pl-3.5" role="group">
          {node.children!.map((child) => (
            <TreeItem
              key={child.id}
              node={child}
              selected={selected}
              onSelect={onSelect}
              collapsed={collapsed}
              onToggle={onToggle}
              depth={depth + 1}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
