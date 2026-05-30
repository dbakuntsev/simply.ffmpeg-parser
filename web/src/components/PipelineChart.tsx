import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PipelineBox, PipelineEdge, PipelineModel, PipelineStage } from "../parser";

type Props = {
  model: PipelineModel;
  selectedNode: string | null;
  onSelect: (id: string) => void;
};

const STAGE_ORDER: PipelineStage[] = ["input", "demuxer", "transform", "muxer", "output"];
const STAGE_LABEL: Record<PipelineStage, string> = {
  input: "INPUT",
  demuxer: "DEMUXER",
  transform: "TRANSFORM",
  muxer: "MUXER",
  output: "OUTPUT",
};
// Header tint per stage. Selected state is handled separately on the box itself.
const STAGE_TINT: Record<PipelineStage, string> = {
  input: "#ffffff",
  demuxer: "#eff6ff",
  transform: "#fffbeb",
  muxer: "#eff6ff",
  output: "#f1f5f9",
};

const BOX_W = 210;
const COL_GAP = 110; // wide enough to route several orthogonal rail lanes between columns
const ROW_GAP = 18;
const MARGIN = 24;
const HEADER_BAND = 26; // column-title strip at the top
const TITLE_H = 40; // box header (title up to 2 lines + optional subtitle)
const ROW_H = 18;
const BODY_PAD = 8;
// Viewport height is derived from the natural (zoom-independent) content height,
// then enlarged by 50% per request, and clamped. The user can drag-resize it
// vertically (native ``resize: vertical`` grip at the bottom-right corner);
// MIN_VIEWPORT_H is the enforced floor.
const HEIGHT_FACTOR = 1.5;
const MIN_VIEWPORT_H = 200;
const MAX_VIEWPORT_H = 840;

type Placed = PipelineBox & { x: number; y: number; w: number; h: number };
type Pt = [number, number];

/** Build an SVG path through orthogonal waypoints with rounded corners. The
 * straight segments keep the rails inside the box-free channels between
 * columns; the arcs give the railroad/syntax-diagram look. Corner radius is
 * clamped to half the shorter adjoining segment so tight turns stay valid. */
function ortho(points: Pt[], radius: number): string {
  const pts: Pt[] = points.filter(
    (p, i) => i === 0 || p[0] !== points[i - 1][0] || p[1] !== points[i - 1][1]
  );
  if (pts.length < 2) return "";
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i += 1) {
    const [ax, ay] = pts[i - 1];
    const [bx, by] = pts[i];
    const [cx, cy] = pts[i + 1];
    const d1 = Math.hypot(bx - ax, by - ay) || 1;
    const d2 = Math.hypot(cx - bx, cy - by) || 1;
    const r = Math.max(0, Math.min(radius, d1 / 2, d2 / 2));
    const p1x = bx - ((bx - ax) / d1) * r;
    const p1y = by - ((by - ay) / d1) * r;
    const p2x = bx + ((cx - bx) / d2) * r;
    const p2y = by + ((cy - by) / d2) * r;
    d += ` L ${p1x} ${p1y} Q ${bx} ${by} ${p2x} ${p2y}`;
  }
  const last = pts[pts.length - 1];
  d += ` L ${last[0]} ${last[1]}`;
  return d;
}

function boxHeight(box: PipelineBox): number {
  const body = box.rows.length ? box.rows.length * ROW_H + BODY_PAD * 2 : 0;
  return TITLE_H + body;
}

type RoutedEdge = {
  key: string;
  d: string;
  source: string;
  target: string;
  label?: string;
  /** Anchor for the pad-label text — midpoint of the last horizontal segment
   * (the one entering the target box from the left). */
  labelX?: number;
  labelY?: number;
};

const CORNER_R = 9;

function buildGeometry(model: PipelineModel) {
  const cols = STAGE_ORDER.filter((stage) => model.boxes.some((b) => b.stage === stage));

  // Column heights so we can vertically center each column.
  const colHeights = cols.map((stage) => {
    const boxes = model.boxes.filter((b) => b.stage === stage);
    return boxes.reduce((sum, b) => sum + boxHeight(b), 0) + Math.max(0, boxes.length - 1) * ROW_GAP;
  });
  const maxColHeight = Math.max(1, ...colHeights);

  const placed = new Map<string, Placed>();
  cols.forEach((stage, ci) => {
    const boxes = model.boxes.filter((b) => b.stage === stage);
    const x = MARGIN + ci * (BOX_W + COL_GAP);
    let y = MARGIN + HEADER_BAND + (maxColHeight - colHeights[ci]) / 2;
    for (const b of boxes) {
      const h = boxHeight(b);
      placed.set(b.id, { ...b, x, y, w: BOX_W, h });
      y += h + ROW_GAP;
    }
  });

  const width = MARGIN * 2 + cols.length * BOX_W + Math.max(0, cols.length - 1) * COL_GAP;

  // ---- edge routing ----
  // Convention: every rail leaves a box from its RIGHT edge (output) and enters
  // the next from its LEFT edge (input). Forward edges (target in a later
  // column) route straight through the gap. Same-column edges (one filter chain
  // feeding another) can't go straight right→left, so they U-route: out the
  // right, down through a clear cross-over lane below the boxes, and back up
  // into the target's left edge. All vertical/horizontal runs stay in box-free
  // channels, so no rail ever passes under a box.
  //
  // To avoid horizontal-segment overlap when a single box has multiple rails on
  // the same side, we fan out attachment points along the box edge (outgoing
  // along the right edge, incoming along the left). Slot order is chosen so the
  // rail ends roughly match the vertical order of the *other* endpoints — i.e.
  // a topmost target receives from a topmost slot — which minimizes crossings.
  const colOf = (stage: PipelineStage) => cols.indexOf(stage);
  type Item = { e: PipelineEdge; s: Placed; t: Placed; y1: number; y2: number };
  const rawItems = model.edges
    .map((e) => ({ e, s: placed.get(e.source)!, t: placed.get(e.target)! }))
    .filter((it) => it.s && it.t);

  // Fan-out / fan-in slot assignment per box.
  const outBy = new Map<string, typeof rawItems>();
  const inBy = new Map<string, typeof rawItems>();
  for (const it of rawItems) {
    (outBy.get(it.s.id) ?? outBy.set(it.s.id, []).get(it.s.id)!).push(it);
    (inBy.get(it.t.id) ?? inBy.set(it.t.id, []).get(it.t.id)!).push(it);
  }
  for (const arr of outBy.values()) {
    arr.sort((a, b) => a.t.y + a.t.h / 2 - (b.t.y + b.t.h / 2));
  }
  for (const arr of inBy.values()) {
    arr.sort((a, b) => a.s.y + a.s.h / 2 - (b.s.y + b.s.h / 2));
  }
  const outSlot = new Map<(typeof rawItems)[number], { idx: number; count: number }>();
  const inSlot = new Map<(typeof rawItems)[number], { idx: number; count: number }>();
  for (const arr of outBy.values()) arr.forEach((it, idx) => outSlot.set(it, { idx, count: arr.length }));
  for (const arr of inBy.values()) arr.forEach((it, idx) => inSlot.set(it, { idx, count: arr.length }));

  const ATTACH_PAD = 10; // keep attach points clear of box corners
  const attachY = (box: Placed, slot: { idx: number; count: number }) => {
    if (slot.count <= 1) return box.y + box.h / 2;
    const top = box.y + ATTACH_PAD;
    const bot = box.y + box.h - ATTACH_PAD;
    return top + ((bot - top) * slot.idx) / (slot.count - 1);
  };

  const items: Item[] = rawItems.map((it) => ({
    ...it,
    y1: attachY(it.s, outSlot.get(it)!),
    y2: attachY(it.t, inSlot.get(it)!),
  }));

  const forward = new Map<number, Item[]>();
  const side = new Map<number, Item[]>();
  for (const it of items) {
    const bucket = colOf(it.t.stage) > colOf(it.s.stage) ? forward : side;
    (bucket.get(it.s.x) ?? bucket.set(it.s.x, []).get(it.s.x)!).push(it);
  }

  const edges: RoutedEdge[] = [];

  for (const group of forward.values()) {
    // Order lane assignment by the rail's mean y so adjacent lanes carry
    // adjacent rails — minimizes crossings within the column gap.
    group.sort((a, b) => a.y1 + a.y2 - (b.y1 + b.y2));
    const n = group.length;
    group.forEach((it, i) => {
      const { s, t, y1, y2 } = it;
      const x1 = s.x + s.w;
      const x2 = t.x;
      const gap = Math.max(20, x2 - x1);
      const laneX = x1 + (gap * (i + 1)) / (n + 1);
      const pts: Pt[] = [
        [x1, y1],
        [laneX, y1],
        [laneX, y2],
        [x2, y2],
      ];
      edges.push({
        key: `${it.e.source}->${it.e.target}`,
        d: ortho(pts, CORNER_R),
        source: it.e.source,
        target: it.e.target,
        label: it.e.label,
        labelX: (laneX + x2) / 2,
        labelY: y2,
      });
    });
  }

  const maxBottom = Math.max(MARGIN + HEADER_BAND, ...[...placed.values()].map((b) => b.y + b.h));
  for (const group of side.values()) {
    // Split by direction so down-going and up-going U-routes are laned (and
    // cross-over-stacked) independently.
    const down = group.filter((it) => it.y2 >= it.y1);
    const up = group.filter((it) => it.y2 < it.y1);
    const routeBundle = (bundle: Item[], dir: 1 | -1) => {
      const n = bundle.length;
      bundle.forEach((it, j) => {
        const { s, t, y1: ySrc, y2: yTgt } = it;
        const colLeft = s.x; // same column ⇒ s.x === t.x
        const colRight = s.x + s.w;
        const frac = (j + 1) / (n + 1);
        const busR = colRight + COL_GAP * frac;
        const busL = Math.max(MARGIN / 2, colLeft - COL_GAP * frac);
        // Stack the cross-over levels in the row-gap next to the source so
        // multiple U-routes never share the same horizontal line.
        const base = dir > 0 ? s.y + s.h : s.y;
        const crossY = base + dir * ((ROW_GAP * 0.35) + (ROW_GAP * 0.4 * j) / Math.max(1, n - 1 || 1));
        const pts: Pt[] = [
          [colRight, ySrc], // leave source from the RIGHT
          [busR, ySrc],
          [busR, crossY],
          [busL, crossY],
          [busL, yTgt],
          [colLeft, yTgt], // enter target from the LEFT
        ];
        edges.push({
          key: `${it.e.source}->${it.e.target}`,
          d: ortho(pts, CORNER_R),
          source: it.e.source,
          target: it.e.target,
          label: it.e.label,
          labelX: (busL + colLeft) / 2,
          labelY: yTgt,
        });
      });
    };
    routeBundle(down, 1);
    routeBundle(up, -1);
  }

  const height = maxBottom + MARGIN;
  return { placed, cols, width, height, edges };
}

export function PipelineChart({ model, selectedNode, onSelect }: Props) {
  const [zoom, setZoom] = useState(1);
  const { placed, cols, width, height, edges } = useMemo(() => buildGeometry(model), [model]);

  // Resolve which box's rails to highlight. A selection may be a box id or a
  // row id inside a box; either way we light up the owning box's connections.
  const activeBox = useMemo(() => {
    if (!selectedNode) return null;
    if (placed.has(selectedNode)) return selectedNode;
    for (const b of model.boxes) {
      if (b.rows.some((r) => r.id === selectedNode)) return b.id;
    }
    return null;
  }, [selectedNode, placed, model.boxes]);

  const isEmpty = model.boxes.length === 0;

  // Default viewport height: content height + 50%, clamped. Applied imperatively
  // so the browser's native vertical resize is never overwritten by a re-render;
  // once the user drags the grip we stop auto-resetting on content changes.
  const defaultH = Math.max(MIN_VIEWPORT_H, Math.min(Math.round(height * HEIGHT_FACTOR), MAX_VIEWPORT_H));
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const userResizedRef = useRef(false);

  useLayoutEffect(() => {
    const el = viewportRef.current;
    if (el && !userResizedRef.current) el.style.height = `${defaultH}px`;
  }, [defaultH]);

  const handleResizeEnd = () => {
    const el = viewportRef.current;
    if (el && Math.abs(el.clientHeight - defaultH) > 2) userResizedRef.current = true;
  };

  // Keep the highlighted box on screen. Opening the inspector drawer at lg+ adds
  // a right margin to the page, which narrows this scroll container — so a box
  // that was visible when clicked can scroll out of view. Re-run on selection
  // change AND whenever the container resizes (the drawer's slide is animated,
  // so the width settles over several frames). Only scrolls when the box is
  // actually outside the viewport, so it never fights manual scrolling.
  const ensureVisibleRef = useRef<() => void>(() => {});
  ensureVisibleRef.current = () => {
    const el = viewportRef.current;
    if (!el || !activeBox) return;
    const b = placed.get(activeBox);
    if (!b) return;
    const PAD = 24;
    const left = b.x * zoom;
    const right = (b.x + b.w) * zoom;
    const top = b.y * zoom;
    const bottom = (b.y + b.h) * zoom;
    if (right - left >= el.clientWidth - 2 * PAD || left < el.scrollLeft + PAD) {
      el.scrollLeft = Math.max(0, left - PAD); // align left (title) edge
    } else if (right > el.scrollLeft + el.clientWidth - PAD) {
      el.scrollLeft = right - el.clientWidth + PAD;
    }
    if (top < el.scrollTop + PAD) {
      el.scrollTop = Math.max(0, top - PAD);
    } else if (bottom > el.scrollTop + el.clientHeight - PAD) {
      el.scrollTop = bottom - el.clientHeight + PAD;
    }
  };

  useEffect(() => {
    ensureVisibleRef.current();
  }, [activeBox, zoom]);

  // Keyed on isEmpty so the observer (re-)attaches when the viewport element
  // actually mounts — at first render the chart may be empty (metadata still
  // loading) and the ref would be null.
  useEffect(() => {
    const el = viewportRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const obs = new ResizeObserver(() => ensureVisibleRef.current());
    obs.observe(el);
    return () => obs.disconnect();
  }, [isEmpty]);

  return (
    <div className="min-w-0 overflow-hidden rounded-[3px] border border-edge bg-white/70">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-3 pt-2 pb-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
          {STAGE_ORDER.map((stage) => (
            <span key={stage} className="inline-flex items-center gap-1">
              <span
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 rounded-[2px]"
                style={{ backgroundColor: STAGE_TINT[stage], border: "1px solid #656d76" }}
              />
              {STAGE_LABEL[stage].charAt(0) + STAGE_LABEL[stage].slice(1).toLowerCase()}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <ZoomButton label="Zoom out" onClick={() => setZoom((z) => clampZoom(z / 1.2))}>
            −
          </ZoomButton>
          <span className="w-10 text-center text-[11px] tabular-nums text-muted">{Math.round(zoom * 100)}%</span>
          <ZoomButton label="Zoom in" onClick={() => setZoom((z) => clampZoom(z * 1.2))}>
            +
          </ZoomButton>
          <ZoomButton label="Reset zoom" onClick={() => setZoom(1)}>
            ⤢
          </ZoomButton>
        </div>
      </div>

      {isEmpty ? (
        <div className="px-4 py-10 text-center text-sm text-muted">No command to visualize.</div>
      ) : (
        <div
          ref={viewportRef}
          className="overflow-auto"
          style={{ minHeight: MIN_VIEWPORT_H, maxHeight: MAX_VIEWPORT_H, resize: "vertical" }}
          onMouseUp={handleResizeEnd}
        >
          <svg
            id="pipeline-chart"
            role="img"
            aria-label="FFmpeg processing pipeline: input, demuxer, transformations, muxer, output"
            viewBox={`0 0 ${width} ${height}`}
            width={width * zoom}
            height={height * zoom}
            style={{ display: "block" }}
          >
            {/* column headers */}
            {cols.map((stage, ci) => (
              <text
                key={stage}
                x={MARGIN + ci * (BOX_W + COL_GAP) + BOX_W / 2}
                y={MARGIN + 12}
                textAnchor="middle"
                fontSize={10}
                fontWeight={600}
                letterSpacing="0.1em"
                fill="#656d76"
              >
                {STAGE_LABEL[stage]}
              </text>
            ))}

            {/* rails — dimmed when a box is active so the highlighted ones stand out */}
            <g fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round">
              {edges
                .filter((p) => !(activeBox && (p.source === activeBox || p.target === activeBox)))
                .map((p) => (
                  <path key={p.key} d={p.d} opacity={activeBox ? 0.3 : 1} />
                ))}
            </g>
            {/* highlighted rails: the active box's inputs (from the left) and outputs (to the right) */}
            <g fill="none" stroke="#0969da" strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round">
              {edges
                .filter((p) => activeBox && (p.source === activeBox || p.target === activeBox))
                .map((p) => (
                  <path key={p.key} d={p.d} />
                ))}
            </g>

            {/* pad labels on edges (e.g. ``LOW``, ``HIGH``, ``0:a``) — anchored
                at the segment entering the target. White stroke halo so the
                text remains readable when it sits above a rail line. */}
            <g
              fontSize={10}
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
              textAnchor="middle"
              style={{ paintOrder: "stroke" }}
              stroke="#ffffff"
              strokeWidth={3}
              strokeLinejoin="round"
            >
              {edges
                .filter((p) => p.label && p.labelX !== undefined && p.labelY !== undefined)
                .map((p) => {
                  const isActive = !!(activeBox && (p.source === activeBox || p.target === activeBox));
                  return (
                    <text
                      key={`${p.key}-label`}
                      x={p.labelX}
                      y={p.labelY! - 4}
                      fill={isActive ? "#0969da" : "#475569"}
                      opacity={activeBox && !isActive ? 0.3 : 1}
                    >
                      {p.label}
                    </text>
                  );
                })}
            </g>

            {/* boxes */}
            {[...placed.values()].map((b) => (
              <BoxNode
                key={b.id}
                box={b}
                selected={selectedNode}
                onSelect={onSelect}
              />
            ))}
          </svg>
        </div>
      )}
    </div>
  );
}

function BoxNode({
  box,
  selected,
  onSelect,
}: {
  box: Placed;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const boxSelected = selected === box.id;
  const selectBox = box.selectable ? () => onSelect(box.id) : undefined;

  return (
    <foreignObject x={box.x} y={box.y} width={box.w} height={box.h}>
      <div
        className={`flex h-full w-full flex-col overflow-hidden rounded-[4px] border ${
          boxSelected ? "border-blue-500 ring-2 ring-blue-300" : "border-edge"
        }`}
        style={{ background: "#ffffff" }}
      >
        <div
          role={selectBox ? "button" : undefined}
          tabIndex={selectBox ? 0 : undefined}
          aria-current={boxSelected ? "true" : undefined}
          onClick={selectBox}
          onKeyDown={(e) => {
            if (selectBox && (e.key === "Enter" || e.key === " ")) {
              e.preventDefault();
              selectBox();
            }
          }}
          className={`px-2 py-1 ${selectBox ? "cursor-pointer" : "cursor-default"} focus:outline-none focus:ring-2 focus:ring-blue-200`}
          style={{ background: boxSelected ? "#dbeafe" : STAGE_TINT[box.stage], height: TITLE_H }}
        >
          <div className="truncate font-mono text-[12px] font-semibold leading-tight text-ink" title={box.title}>
            {box.title}
          </div>
          {box.subtitle && <div className="truncate text-[10px] italic text-muted">{box.subtitle}</div>}
        </div>
        {box.rows.length > 0 && (
          <div className="flex-1 overflow-hidden border-t border-edge" style={{ padding: BODY_PAD }}>
            {box.rows.map((row) => {
              const rowSelected = selected === row.id;
              return (
                <div
                  key={row.id}
                  role="button"
                  tabIndex={0}
                  aria-current={rowSelected ? "true" : undefined}
                  title={row.text}
                  onClick={() => onSelect(row.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelect(row.id);
                    }
                  }}
                  className={`cursor-pointer truncate rounded-[2px] font-mono text-[11px] leading-[18px] focus:outline-none focus:ring-1 focus:ring-blue-300 ${
                    rowSelected ? "bg-blue-50 text-blue-800" : "text-muted hover:bg-blue-50/60 hover:text-ink"
                  }`}
                  style={{ paddingLeft: row.indent ? 12 : 2, paddingRight: 2 }}
                >
                  {row.text}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </foreignObject>
  );
}

function ZoomButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      className="inline-flex h-6 w-6 items-center justify-center rounded-[3px] border border-edge text-sm text-muted hover:bg-edge focus:outline-none focus:ring-2 focus:ring-blue-200"
    >
      {children}
    </button>
  );
}

function clampZoom(z: number) {
  return Math.min(2.5, Math.max(0.4, z));
}
