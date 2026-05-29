import type { CodecsMetadata, FiltersMetadata, OptionBinding, SemanticCommand } from "../types";
import type { TreeNode } from "../components/TreeList";
import { isFilterComplexBinding } from "./filters";
import { splitStreamSpecifier } from "./streamSpecifier";

const CODEC_SELECTOR_BASES = new Set(["-c", "-codec", "-vcodec", "-acodec", "-scodec"]);

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

    // demuxer/chain → chain, via input pads
    chainBoxes.forEach((c) => {
      c.inputPads.forEach((pad) => {
        const file = FILE_PAD_RE.exec(pad);
        if (file) {
          const idx = Number(file[1]);
          if (idx < demuxerIds.length) edges.push({ source: demuxerIds[idx], target: c.id });
        } else if (producerByPad.has(pad)) {
          edges.push({ source: producerByPad.get(pad)!, target: c.id });
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
    // ``-map`` is documented as a global option (the resolver classifies it as
    // such), so look in both the output's own options and the globals pool.
    const mapBindings = (output: SemanticCommand["outputs"][number]) =>
      [...output.options, ...semantic.globals].filter(
        (opt) => splitStreamSpecifier(opt.flag.toLowerCase()).base === "-map"
      );
    semantic.outputs.forEach((output, j) => {
      const muxerId = `muxer_${j}`;
      const mapped: string[] = [];
      mapBindings(output).forEach((opt) =>
        opt.values.forEach((v) => {
          for (const m of v.matchAll(PAD_LABEL_RE)) {
            const src = producerByPad.get(m[1]);
            if (src) mapped.push(src);
          }
        })
      );
      if (mapped.length) {
        mapped.forEach((src) => edges.push({ source: src, target: muxerId }));
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

  return { boxes, edges };
}

export function summarizeCommand(semantic: SemanticCommand, _codecs: CodecsMetadata, _filters: FiltersMetadata) {
  const inputs = semantic.inputs.length;
  const outputs = semantic.outputs.length;
  const filterCount = semantic.filters.length;
  return `Inputs: ${inputs}, Outputs: ${outputs}, Filters: ${filterCount}.`;
}
