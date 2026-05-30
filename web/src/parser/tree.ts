import type { OptionBinding, SemanticCommand } from "../types";
import type { TreeNode } from "../components/TreeList";
import { isFilterComplexBinding } from "./filters";
import { CODEC_SELECTOR_BASES, splitStreamSpecifier } from "./streamSpecifier";

function isCodecSelector(opt: OptionBinding): boolean {
  const { base } = splitStreamSpecifier(opt.flag.toLowerCase());
  return CODEC_SELECTOR_BASES.has(base) && opt.values.length > 0;
}

function isFormatSelector(opt: OptionBinding): boolean {
  return opt.flag.toLowerCase() === "-f" && opt.values.length > 0;
}

function optionLabel(opt: OptionBinding): string {
  return `${opt.flag} ${opt.values.join(" ")}`.trim();
}

function optionNode(opt: OptionBinding, children?: TreeNode[]): TreeNode {
  return {
    id: opt.id,
    kind: "option",
    label: optionLabel(opt),
    children,
  };
}

/** Group an input/output's flat option list into a tree: codec-private
 * bindings become children of the matching ``-c[:T]`` selector, format-private
 * bindings become children of the matching ``-f`` selector. Generic AVCodec /
 * AVFormat options and driver options stay at the top level — they aren't
 * tied to a specific codec/format and have no natural parent.
 *
 * Document order is preserved across the top level; children appear in their
 * original document order under their respective parents.
 */
function nestOptions(options: OptionBinding[]): TreeNode[] {
  // Index selectors by the value they select. When the same codec/format is
  // selected multiple times (rare; e.g. ``-c:v libx264 ... -c:v libx264 ...``),
  // the last selector in document order wins — subsequent private options
  // logically belong to the most recent declaration. We pre-index in two
  // passes so a private option appearing *before* its selector in some
  // unusual ordering still attaches correctly.
  const codecSelectorByName = new Map<string, OptionBinding>();
  const formatSelectorByName = new Map<string, OptionBinding>();
  for (const opt of options) {
    if (isCodecSelector(opt)) {
      codecSelectorByName.set(opt.values[0].toLowerCase(), opt);
    } else if (isFormatSelector(opt)) {
      formatSelectorByName.set(opt.values[0].toLowerCase(), opt);
    }
  }

  // Bucket children by parent binding id.
  const childrenByParent = new Map<string, OptionBinding[]>();
  const topLevel: OptionBinding[] = [];

  for (const opt of options) {
    if (isCodecSelector(opt) || isFormatSelector(opt)) {
      topLevel.push(opt);
      continue;
    }

    if (opt.resolutionSource === "codec-private" && opt.matchedCodec) {
      const parent = codecSelectorByName.get(opt.matchedCodec.toLowerCase());
      if (parent) {
        const list = childrenByParent.get(parent.id) ?? [];
        list.push(opt);
        childrenByParent.set(parent.id, list);
        continue;
      }
    }

    if (opt.resolutionSource === "format-private" && opt.matchedFormat) {
      const parent = formatSelectorByName.get(opt.matchedFormat.toLowerCase());
      if (parent) {
        const list = childrenByParent.get(parent.id) ?? [];
        list.push(opt);
        childrenByParent.set(parent.id, list);
        continue;
      }
    }

    topLevel.push(opt);
  }

  return topLevel.map((opt) => {
    const children = childrenByParent.get(opt.id);
    return optionNode(opt, children?.map((c) => optionNode(c)));
  });
}

/** Single-shot placeholder used as a child of always-present top-level
 * sections when they have nothing real to show. Non-interactive, italic,
 * lighter gray — see ``TreeList`` for the rendering. */
function placeholderChild(parentId: string): TreeNode {
  return {
    id: `${parentId}_empty`,
    kind: "placeholder",
    label: "(none)",
  };
}

function withPlaceholder(parentId: string, children: TreeNode[]): TreeNode[] {
  return children.length ? children : [placeholderChild(parentId)];
}

export function buildTreeNodes(semantic: SemanticCommand): TreeNode[] {
  return [
    {
      id: "globals",
      label: "Global Options",
      kind: "globals",
      children: withPlaceholder(
        "globals",
        nestOptions(
          semantic.globals.filter((opt) => !isFilterComplexBinding(opt))
        )
      ),
    },
    {
      id: "inputs",
      label: "Inputs",
      kind: "inputs",
      children: withPlaceholder(
        "inputs",
        semantic.inputs.map((input) => ({
          id: input.id,
          kind: "input",
          label: input.source,
          children: nestOptions(input.options),
        }))
      ),
    },
    {
      id: "filters",
      label: "Filters",
      kind: "filters",
      children: withPlaceholder(
        "filters",
        semantic.filters.map((filter) => ({
          id: filter.id,
          kind: "filter",
          label: filter.expression,
          children: filter.chains?.map((chain) => ({
            id: chain.id,
            kind: "chain",
            label: chain.label || "Filter Chain",
            children: chain.filters.map((step, index) => ({
              id: `${chain.id}_step_${index}`,
              kind: "step",
              label: step.name,
              children: step.args.map((arg, argIndex) => ({
                id: `${chain.id}_step_${index}_arg_${argIndex}`,
                kind: "arg",
                label: `${arg.key} = ${arg.value}`,
              })),
            })),
          })),
        }))
      ),
    },
    {
      id: "outputs",
      label: "Outputs",
      kind: "outputs",
      children: withPlaceholder(
        "outputs",
        semantic.outputs.map((output) => ({
          id: output.id,
          kind: "output",
          label: output.target,
          children: nestOptions(output.options),
        }))
      ),
    },
  ];
}
