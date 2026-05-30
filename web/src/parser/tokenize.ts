import type { Token, TokenType } from "../types";
import { nextTokenId } from "./ids";

export function tokenize(command: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  let current = "";
  let start = 0;
  let inDouble = false;
  let inSingle = false;
  let escape = false;

  const pushToken = (text: string, startIndex: number, endIndex: number) => {
    if (!text) {
      return;
    }
    const normalized = text.trim();
    if (!normalized) {
      return;
    }

    if (normalized.startsWith("-") && normalized.includes("=")) {
      const eqIndex = normalized.indexOf("=");
      const flagText = normalized.slice(0, eqIndex);
      const valueText = normalized.slice(eqIndex + 1);
      const flagEnd = startIndex + eqIndex;
      tokens.push({
        id: nextTokenId(),
        type: "flag",
        text: flagText,
        normalizedText: flagText.toLowerCase(),
        position: tokens.length,
        sourceRange: { start: startIndex, end: flagEnd },
      });
      tokens.push({
        id: nextTokenId(),
        type: "value",
        text: valueText,
        normalizedText: valueText.toLowerCase(),
        position: tokens.length,
        sourceRange: { start: flagEnd + 1, end: endIndex },
      });
      return;
    }

    const type: TokenType = normalized.startsWith("-") ? "flag" : "value";
    tokens.push({
      id: nextTokenId(),
      type,
      text: normalized,
      normalizedText: normalized.toLowerCase(),
      position: tokens.length,
      sourceRange: { start: startIndex, end: endIndex },
    });
  };

  while (i < command.length) {
    const ch = command[i];
    if (inSingle) {
      if (ch === "'") {
        inSingle = false;
        i += 1;
        continue;
      }
      current += ch;
      i += 1;
      continue;
    }
    if (escape) {
      current += ch;
      escape = false;
      i += 1;
      continue;
    }
    if (ch === "\\") {
      escape = true;
      i += 1;
      continue;
    }
    if (ch === '"') {
      inDouble = !inDouble;
      i += 1;
      continue;
    }
    if (!inDouble && ch === "'") {
      inSingle = true;
      i += 1;
      continue;
    }
    if (!inDouble && /\s/.test(ch)) {
      pushToken(current, start, i);
      current = "";
      i += 1;
      start = i;
      continue;
    }
    current += ch;
    i += 1;
  }
  pushToken(current, start, i);

  if (tokens.length && tokens[0].normalizedText.includes("ffmpeg")) {
    tokens[0] = {
      ...tokens[0],
      type: "executable",
    };
  }

  return tokens;
}
