import type { CodecsMetadata, FiltersMetadata, OptionBinding, SemanticCommand } from "../types";
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

export type PipelineStage = "input" | "demuxer" | "transform" | "muxer" | "output";

export interface PipelineRow {
  /** Existing selection id (option/step/arg) so clicking opens the inspector. */
  id: string;
  text: string;
  /** Render one level indented — used for filter-step arguments. */
  indent?: boolean;
}

export interface PipelineBox {
  id: string;
  stage: PipelineStage;
  title: string;
  subtitle?: string;
  /** False for synthetic boxes that have no entry in ``buildSelectionInfo``. */
  selectable: boolean;
  rows: PipelineRow[];
}

export interface PipelineEdge {
  source: string;
  target: string;
  /** Pad label (e.g. ``LOW``, ``0:a``) when the edge carries a specific named
   * or file-pad stream — used to disambiguate which output of a multi-output
   * chain feeds which downstream box. Absent for auto-assigned/fan-out edges. */
  label?: string;
}

export interface PipelineModel {
  boxes: PipelineBox[];
  edges: PipelineEdge[];
}

/** Which pipeline stage an input/output option belongs to. ``-f`` and any
 * format-resolved option describe the (de)muxer; codec selectors and
 * codec-resolved options describe the decoder/encoder (shown in the
 * demuxer/muxer box); everything else stays on the file box. */
function optionStage(opt: OptionBinding): "format" | "codec" | "file" {
  const { base } = splitStreamSpecifier(opt.flag.toLowerCase());
  if (base === "-f" || opt.resolutionSource === "format-private" || opt.resolutionSource === "format-generic") {
    return "format";
  }
  if (
    CODEC_SELECTOR_BASES.has(base) ||
    opt.resolutionSource === "codec-private" ||
    opt.resolutionSource === "codec-generic"
  ) {
    return "codec";
  }
  return "file";
}

function rowsFor(options: OptionBinding[], stages: Array<"format" | "codec" | "file">): PipelineRow[] {
  return options
    .filter((opt) => stages.includes(optionStage(opt)))
    .map((opt) => ({ id: opt.id, text: optionLabel(opt) }));
}

function formatName(options: OptionBinding[]): string | null {
  const fmt = options.find((opt) => splitStreamSpecifier(opt.flag.toLowerCase()).base === "-f");
  return fmt && fmt.values.length ? fmt.values[0] : null;
}

const FILE_PAD_RE = /^(\d+)(?::|$)/;
const PAD_LABEL_RE = /\[([^\]]+)\]/g;

export function buildPipelineModel(semantic: SemanticCommand): PipelineModel {
  const boxes: PipelineBox[] = [];
  const edges: PipelineEdge[] = [];

  // input + demuxer columns
  semantic.inputs.forEach((input, i) => {
    boxes.push({
      id: `input_${i}`,
      stage: "input",
      title: input.source,
      selectable: true,
      rows: rowsFor(input.options, ["file"]),
    });
    const fmt = formatName(input.options);
    boxes.push({
      id: `demuxer_${i}`,
      stage: "demuxer",
      title: fmt ?? "auto",
      subtitle: fmt ? undefined : "(by extension)",
      selectable: true,
      rows: rowsFor(input.options, ["format", "codec"]),
    });
    edges.push({ source: `input_${i}`, target: `demuxer_${i}` });
  });

  // transform column
  const chainBoxes: Array<{ id: string; inputPads: string[]; outputPads: string[] }> = [];
  let hasFilters = false;
  semantic.filters.forEach((filter) => {
    hasFilters = true;
    if (filter.chains && filter.chains.length > 0) {
      filter.chains.forEach((chain) => {
        const stepRows: PipelineRow[] = [];
        chain.filters.forEach((step, k) => {
          const stepId = `${chain.id}_step_${k}`;
          stepRows.push({ id: stepId, text: step.name });
          step.args.forEach((arg, j) => {
            stepRows.push({ id: `${stepId}_arg_${j}`, text: `${arg.key} = ${arg.value}`, indent: true });
          });
        });
        const names = chain.filters.map((s) => s.name).filter(Boolean).join(" → ");
        boxes.push({
          id: chain.id,
          stage: "transform",
          title: names || chain.label || "Filter Chain",
          selectable: true,
          rows: stepRows,
        });
        chainBoxes.push({
          id: chain.id,
          inputPads: chain.inputPads ?? [],
          outputPads: chain.outputPads ?? [],
        });
      });
      return;
    }
    // -vf / -af graph: no parsed pad routing, fanned below.
    boxes.push({
      id: filter.id,
      stage: "transform",
      title: filter.expression,
      selectable: true,
      rows: [],
    });
  });
  if (!hasFilters) {
    boxes.push({
      id: "transform_passthrough",
      stage: "transform",
      title: "copy / passthrough",
      selectable: false,
      rows: [],
    });
  }

  // muxer + output columns
  semantic.outputs.forEach((output, j) => {
    const fmt = formatName(output.options);
    boxes.push({
      id: `muxer_${j}`,
      stage: "muxer",
      title: fmt ?? "auto",
      subtitle: fmt ? undefined : "(by extension)",
      selectable: true,
      rows: rowsFor(output.options, ["format", "codec"]),
    });
    boxes.push({
      id: `output_${j}`,
      stage: "output",
      title: output.target,
      selectable: true,
      rows: rowsFor(output.options, ["file"]),
    });
    edges.push({ source: `muxer_${j}`, target: `output_${j}` });
  });

  // ---- routing between demuxer → transform → muxer ----
  const transformBoxes = boxes.filter((b) => b.stage === "transform");
  const nonChainTransform = transformBoxes.filter((b) => !chainBoxes.some((c) => c.id === b.id));
  const demuxerIds = semantic.inputs.map((_, i) => `demuxer_${i}`);

  if (chainBoxes.length > 0) {
    const producerByPad = new Map<string, string>();
    chainBoxes.forEach((c) => c.outputPads.forEach((pad) => producerByPad.set(pad, c.id)));

    // demuxer/chain → chain, via input pads. A chain with no explicit input
    // pads relies on ffmpeg's automatic input assignment — fan in from all
    // demuxers so the box isn't visually orphaned (e.g. ``overlay=...`` with
    // no ``[0:v][1:v]`` labels).
    chainBoxes.forEach((c) => {
      if (c.inputPads.length === 0) {
        demuxerIds.forEach((src) => edges.push({ source: src, target: c.id }));
        return;
      }
      c.inputPads.forEach((pad) => {
        const file = FILE_PAD_RE.exec(pad);
        if (file) {
          const idx = Number(file[1]);
          if (idx < demuxerIds.length) edges.push({ source: demuxerIds[idx], target: c.id, label: pad });
        } else if (producerByPad.has(pad)) {
          edges.push({ source: producerByPad.get(pad)!, target: c.id, label: pad });
        }
      });
    });

    // chain → muxer, via -map [label]; otherwise fan terminal chains
    const consumedNamed = new Set<string>();
    chainBoxes.forEach((c) =>
      c.inputPads.forEach((pad) => {
        if (!FILE_PAD_RE.test(pad)) consumedNamed.add(pad);
      })
    );
    const terminalChains = chainBoxes.filter(
      (c) => c.outputPads.length === 0 || c.outputPads.some((p) => !consumedNamed.has(p))
    );
    // ``-map`` is special-cased to output scope in ``semantic.ts``, so each
    // ``-map`` already lives on its destination output and we only need to
    // inspect that output's own options here.
    const mapBindings = (output: SemanticCommand["outputs"][number]) =>
      output.options.filter(
        (opt) => splitStreamSpecifier(opt.flag.toLowerCase()).base === "-map"
      );
    semantic.outputs.forEach((output, j) => {
      const muxerId = `muxer_${j}`;
      const mapped: Array<{ source: string; label: string }> = [];
      mapBindings(output).forEach((opt) =>
        opt.values.forEach((v) => {
          for (const m of v.matchAll(PAD_LABEL_RE)) {
            const src = producerByPad.get(m[1]);
            if (src) mapped.push({ source: src, label: m[1] });
          }
        })
      );
      if (mapped.length) {
        mapped.forEach(({ source, label }) => edges.push({ source, target: muxerId, label }));
      } else {
        terminalChains.forEach((c) => edges.push({ source: c.id, target: muxerId }));
      }
    });
  }

  // Fan any non-chain transform boxes (vf/af graphs, passthrough) across all
  // demuxers and muxers — these carry no parsed pad routing.
  if (nonChainTransform.length > 0) {
    nonChainTransform.forEach((box) => {
      demuxerIds.forEach((src) => edges.push({ source: src, target: box.id }));
      semantic.outputs.forEach((_, j) => edges.push({ source: box.id, target: `muxer_${j}` }));
    });
  }

  // Dedupe parallel edges: a chain with input pads ``[0:1][0:2]…`` from the
  // same file would otherwise emit one edge per pad, all sharing the same
  // ``source->target`` key and confusing React's keyed reconciliation when the
  // command changes (stale <path>s linger with bad geometry). When pad labels
  // differ on collapsed edges (e.g. ``[0:v]`` + ``[0:a]`` from one demuxer),
  // join them into a single comma-separated label so a single rail can carry
  // the disambiguation without doubling up paths.
  const edgeMap = new Map<string, PipelineEdge>();
  for (const e of edges) {
    const key = `${e.source}->${e.target}`;
    const existing = edgeMap.get(key);
    if (!existing) {
      edgeMap.set(key, { ...e });
      continue;
    }
    if (e.label) {
      const parts = existing.label ? existing.label.split(", ") : [];
      if (!parts.includes(e.label)) {
        parts.push(e.label);
        existing.label = parts.join(", ");
      }
    }
  }
  const dedupedEdges = [...edgeMap.values()];

  return { boxes, edges: dedupedEdges };
}

export function summarizeCommand(semantic: SemanticCommand, _codecs: CodecsMetadata, _filters: FiltersMetadata) {
  const inputs = semantic.inputs.length;
  const outputs = semantic.outputs.length;
  const filterCount = semantic.filters.length;
  return `Inputs: ${inputs}, Outputs: ${outputs}, Filters: ${filterCount}.`;
}
