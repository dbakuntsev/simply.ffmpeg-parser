// Scroll a textarea's *content* so a given character offset is in view. There
// is no native API for this (``setSelectionRange`` doesn't scroll), so we mirror
// the textarea's text layout in a hidden element, measure where the offset
// renders, and adjust ``scrollTop``. The mirror copies every property that
// affects wrapping/line height, so a soft-wrapped textarea measures correctly.

const MIRROR_PROPS = [
  "boxSizing",
  "width",
  "paddingTop",
  "paddingRight",
  "paddingBottom",
  "paddingLeft",
  "borderTopWidth",
  "borderRightWidth",
  "borderBottomWidth",
  "borderLeftWidth",
  "fontStyle",
  "fontVariant",
  "fontWeight",
  "fontStretch",
  "fontSize",
  "fontFamily",
  "lineHeight",
  "textAlign",
  "textTransform",
  "textIndent",
  "letterSpacing",
  "wordSpacing",
  "tabSize",
  "whiteSpace",
  "wordBreak",
  "overflowWrap",
] as const;

/** Vertical offset (in the textarea's scroll coordinate space) and line height
 * of the character at ``index``. */
function caretMetrics(el: HTMLTextAreaElement, index: number): { top: number; lineHeight: number } {
  const doc = el.ownerDocument;
  const computed = getComputedStyle(el);
  const mirror = doc.createElement("div");
  const style = mirror.style;
  for (const prop of MIRROR_PROPS) {
    style[prop as any] = computed[prop as any];
  }
  style.position = "absolute";
  style.visibility = "hidden";
  style.top = "0";
  style.left = "-9999px";
  style.height = "auto";
  style.overflow = "hidden";
  // Force wrapping behavior to match a textarea regardless of inherited rules.
  style.whiteSpace = "pre-wrap";
  style.overflowWrap = "break-word";

  mirror.textContent = el.value.slice(0, index);
  const marker = doc.createElement("span");
  // Anchor span; needs non-empty content to have a box at the offset position.
  marker.textContent = el.value.slice(index) || ".";
  mirror.appendChild(marker);
  doc.body.appendChild(mirror);

  const top = marker.offsetTop;
  let lineHeight = parseFloat(computed.lineHeight);
  if (!Number.isFinite(lineHeight)) lineHeight = parseFloat(computed.fontSize) * 1.2;

  doc.body.removeChild(mirror);
  return { top, lineHeight };
}

/** If the character at ``index`` is outside the textarea's visible area,
 * scroll the content vertically so it's shown with a small margin. No-op when
 * it's already visible, so it never fights a position the user can already see. */
export function scrollSelectionIntoView(el: HTMLTextAreaElement, index: number, pad = 12) {
  const { top, lineHeight } = caretMetrics(el, index);
  const viewTop = el.scrollTop;
  const viewBottom = el.scrollTop + el.clientHeight;
  if (top < viewTop + pad) {
    el.scrollTop = Math.max(0, top - pad);
  } else if (top + lineHeight > viewBottom - pad) {
    el.scrollTop = top + lineHeight - el.clientHeight + pad;
  }
}
