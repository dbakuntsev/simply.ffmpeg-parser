import type { FilterGraph, MetadataBundle, OptionBinding, SemanticCommand, Token } from "../types";
import { parseFilterComplex } from "./filters";
import { nextBindingId } from "./ids";
import { ResolvedOption, shouldExpectValue } from "./resolver";
import { splitStreamSpecifier } from "./streamSpecifier";

export function buildSemantic(
  tokens: Token[],
  _metadata: MetadataBundle,
  resolved: Map<string, ResolvedOption | null>
): SemanticCommand {
  const globals: OptionBinding[] = [];
  const inputs: OptionBinding[] = [];
  const outputs: OptionBinding[] = [];
  const inputNodes: { id: string; source: string; options: OptionBinding[]; tokenId?: string }[] = [];
  const outputNodes: { id: string; target: string; options: OptionBinding[]; tokenId?: string }[] = [];
  const filterGraphs: FilterGraph[] = [];

  let inputIndex = -1;
  let outputIndex = -1;
  let pendingGlobals: OptionBinding[] = [];
  let pendingInputs: OptionBinding[] = [];
  let pendingOutputs: OptionBinding[] = [];

  const attachToInput = (binding: OptionBinding, index: number) => {
    binding.inputIndex = index;
    binding.scope = "input";
    inputNodes[index].options.push(binding);
    inputs.push(binding);
  };

  const attachToOutput = (binding: OptionBinding, index: number) => {
    binding.outputIndex = index;
    binding.scope = "output";
    outputNodes[index].options.push(binding);
    outputs.push(binding);
  };

  const assignPendingToInput = (source: string, tokenId?: string) => {
    inputIndex += 1;
    inputNodes.push({ id: `input_${inputIndex}`, source, options: [], tokenId });
    for (const opt of pendingInputs) {
      attachToInput(opt, inputIndex);
    }
    pendingInputs = [];
  };

  const assignPendingToOutput = (target: string, tokenId?: string) => {
    outputIndex += 1;
    outputNodes.push({ id: `output_${outputIndex}`, target, options: [], tokenId });
    for (const opt of pendingOutputs) {
      attachToOutput(opt, outputIndex);
    }
    pendingOutputs = [];
  };

  let i = 0;
  while (i < tokens.length) {
    const token = tokens[i];
    if (token.type === "executable") {
      i += 1;
      continue;
    }

    if (token.normalizedText === "-i") {
      const value = tokens[i + 1];
      if (value) {
        value.type = "input";
        assignPendingToInput(value.text, value.id);
        i += 2;
        continue;
      }
    }

    if (token.type === "flag") {
      const resolution = resolved.get(token.id) ?? null;
      const optionInfo = resolution?.info ?? null;
      const expectsValue = shouldExpectValue(optionInfo);
      const binding: OptionBinding = {
        id: nextBindingId(),
        flag: token.text,
        values: [],
        scope: "global",
        inputIndex: null,
        outputIndex: null,
        tokenIds: [token.id],
        resolutionSource: resolution?.source ?? "unknown",
        matchedCodec: resolution?.matchedCodec,
        matchedFormat: resolution?.matchedFormat,
        inferredStreamType: resolution?.inferredStreamType,
      };

      if (expectsValue && tokens[i + 1] && tokens[i + 1].type !== "flag") {
        const value = tokens[i + 1];
        binding.values.push(value.text);
        binding.tokenIds.push(value.id);
        if (
          token.normalizedText === "-vf" ||
          token.normalizedText === "-af" ||
          token.normalizedText === "-filter_complex"
        ) {
          const chains = token.normalizedText === "-filter_complex" ? parseFilterComplex(value.text) : undefined;
          filterGraphs.push({ id: `filter_${filterGraphs.length}`, expression: value.text, chains, valueTokenId: value.id });
          value.type = "filter";
        }
        i += 2;
      } else {
        i += 1;
      }

      const { base, specifier } = splitStreamSpecifier(token.normalizedText);
      const hasSpecifier = !!specifier;
      let scope = optionInfo?.scope ?? "global";

      // ``-f X`` is documented as global in ffmpeg.texi but behaves
      // positionally — it sets the format of the *next* file (demuxer before
      // ``-i``, muxer after). Route it into the input/output bucket so
      // format-private options like ``-movflags`` can nest under it in the
      // tree visualizer. The resolver already tracks the same positional
      // distinction via ``matchedFormat``, so this just aligns scope
      // attachment with how the option actually applies.
      if (base === "-f") {
        scope = inputIndex >= 0 ? "output" : "input";
      }

      if (scope === "input") {
        if (hasSpecifier && inputIndex >= 0) {
          attachToInput(binding, inputIndex);
        } else {
          pendingInputs.push(binding);
        }
      } else if (scope === "output") {
        if (hasSpecifier && outputIndex >= 0) {
          attachToOutput(binding, outputIndex);
        } else {
          pendingOutputs.push(binding);
        }
      } else {
        if (hasSpecifier && outputIndex >= 0) {
          attachToOutput(binding, outputIndex);
        } else if (hasSpecifier) {
          pendingOutputs.push(binding);
        } else {
          pendingGlobals.push(binding);
        }
      }
      continue;
    }

    if (token.type === "value") {
      assignPendingToOutput(token.text, token.id);
      token.type = "output";
      i += 1;
      continue;
    }

    i += 1;
  }

  if (pendingGlobals.length) {
    globals.push(...pendingGlobals.map((opt) => ({ ...opt, scope: "global" as const })));
  }
  if (pendingInputs.length) {
    globals.push(...pendingInputs.map((opt) => ({ ...opt, scope: "global" as const })));
  }
  if (pendingOutputs.length) {
    globals.push(...pendingOutputs.map((opt) => ({ ...opt, scope: "global" as const })));
  }

  const executable = tokens.find((t) => t.type === "executable")?.text ?? null;

  return {
    executable,
    globals,
    inputs: inputNodes,
    outputs: outputNodes,
    filters: filterGraphs,
  };
}
