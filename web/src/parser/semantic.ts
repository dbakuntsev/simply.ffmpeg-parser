import type { MetadataBundle, OptionBinding, SemanticCommand, Token } from "../types";
import { buildOptionLookup, resolveOptionInfo, shouldExpectValue } from "./fallbacks";
import { parseFilterComplex } from "./filters";
import { nextBindingId } from "./ids";
import { splitStreamSpecifier } from "./streamSpecifier";

export function buildSemantic(tokens: Token[], metadata: MetadataBundle): SemanticCommand {
  const globals: OptionBinding[] = [];
  const inputs: OptionBinding[] = [];
  const outputs: OptionBinding[] = [];
  const inputNodes: { id: string; source: string; options: OptionBinding[] }[] = [];
  const outputNodes: { id: string; target: string; options: OptionBinding[] }[] = [];
  const filterGraphs: {
    id: string;
    expression: string;
    chains?: { id: string; label: string; filters: { name: string; args: { key: string; value: string }[] }[] }[];
  }[] = [];

  const optionLookup = buildOptionLookup(metadata);

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

  const assignPendingToInput = (source: string) => {
    inputIndex += 1;
    inputNodes.push({ id: `input_${inputIndex}`, source, options: [] });
    for (const opt of pendingInputs) {
      attachToInput(opt, inputIndex);
    }
    pendingInputs = [];
  };

  const assignPendingToOutput = (target: string) => {
    outputIndex += 1;
    outputNodes.push({ id: `output_${outputIndex}`, target, options: [] });
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
        assignPendingToInput(value.text);
        i += 2;
        continue;
      }
    }

    if (token.type === "flag") {
      const optionInfo = resolveOptionInfo(token, optionLookup);
      const expectsValue = shouldExpectValue(optionInfo, token.normalizedText);
      const binding: OptionBinding = {
        id: nextBindingId(),
        flag: token.text,
        values: [],
        scope: "global",
        inputIndex: null,
        outputIndex: null,
        tokenIds: [token.id],
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
          filterGraphs.push({ id: `filter_${filterGraphs.length}`, expression: value.text, chains });
          value.type = "filter";
        }
        i += 2;
      } else {
        i += 1;
      }

      const { specifier } = splitStreamSpecifier(token.normalizedText);
      const hasSpecifier = !!specifier;
      const scope = optionInfo?.scope ?? "global";

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
      assignPendingToOutput(token.text);
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
