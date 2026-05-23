import type { CodecsMetadata, FiltersMetadata, SemanticCommand } from "../types";
import type { TreeNode } from "../components/TreeList";
import { isFilterComplexBinding } from "./filters";

export function buildTreeNodes(semantic: SemanticCommand): TreeNode[] {
  return [
    {
      id: "globals",
      label: "Global Options",
      kind: "globals",
      children: semantic.globals
        .filter((opt) => !isFilterComplexBinding(opt))
        .map((opt) => ({
          id: opt.id,
          kind: "option",
          label: `${opt.flag} ${opt.values.join(" ")}`.trim(),
        })),
    },
    {
      id: "inputs",
      label: "Inputs",
      kind: "inputs",
      children: semantic.inputs.map((input) => ({
        id: input.id,
        kind: "input",
        label: input.source,
        children: input.options.map((opt) => ({
          id: opt.id,
          kind: "option",
          label: `${opt.flag} ${opt.values.join(" ")}`.trim(),
        })),
      })),
    },
    {
      id: "filters",
      label: "Filters",
      kind: "filters",
      children: semantic.filters.map((filter) => ({
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
      })),
    },
    {
      id: "outputs",
      label: "Outputs",
      kind: "outputs",
      children: semantic.outputs.map((output) => ({
        id: output.id,
        kind: "output",
        label: output.target,
        children: output.options.map((opt) => ({
          id: opt.id,
          kind: "option",
          label: `${opt.flag} ${opt.values.join(" ")}`.trim(),
        })),
      })),
    },
  ];
}

export function buildFlowNodes(semantic: SemanticCommand) {
  const nodes: { id: string; label: string; group: "input" | "filter" | "output" }[] = [];
  const links: { source: string; target: string }[] = [];

  semantic.inputs.forEach((input, index) => {
    const id = `input_${index}`;
    nodes.push({ id, label: input.source, group: "input" });
  });

  const filterNodes: { id: string; label: string }[] = [];
  semantic.filters.forEach((filter) => {
    if (filter.chains && filter.chains.length > 0) {
      filter.chains.forEach((chain, index) => {
        filterNodes.push({ id: `${filter.id}_chain_${index}`, label: chain.label || "Filter Chain" });
      });
      return;
    }
    filterNodes.push({ id: filter.id, label: filter.expression });
  });

  filterNodes.forEach((filter) => {
    nodes.push({ id: filter.id, label: filter.label, group: "filter" });
  });

  semantic.outputs.forEach((output, index) => {
    const id = `output_${index}`;
    nodes.push({ id, label: output.target, group: "output" });
  });

  const inputIds = nodes.filter((n) => n.group === "input").map((n) => n.id);
  const filterIds = nodes.filter((n) => n.group === "filter").map((n) => n.id);
  const outputIds = nodes.filter((n) => n.group === "output").map((n) => n.id);

  const midTargets = filterIds.length ? filterIds : outputIds;
  inputIds.forEach((id) => {
    midTargets.forEach((target) => links.push({ source: id, target }));
  });

  if (filterIds.length) {
    filterIds.forEach((id) => {
      outputIds.forEach((target) => links.push({ source: id, target }));
    });
  }

  return { nodes, links };
}

export function summarizeCommand(semantic: SemanticCommand, _codecs: CodecsMetadata, _filters: FiltersMetadata) {
  const inputs = semantic.inputs.length;
  const outputs = semantic.outputs.length;
  const filterCount = semantic.filters.length;
  return `Inputs: ${inputs}, Outputs: ${outputs}, Filters: ${filterCount}.`;
}
