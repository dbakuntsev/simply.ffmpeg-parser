import { useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";

export type FlowNode = { id: string; label: string; group: "input" | "filter" | "output" };
export type FlowLink = { source: string; target: string };

type Props = {
  nodes: FlowNode[];
  links: FlowLink[];
  selectedNode: string | null;
  onSelect: (id: string) => void;
};

const ROW_HEIGHT = 64;
const MIN_HEIGHT = 280;
const TOP_PADDING = 64;
const BOTTOM_PADDING = 32;

export function FlowChart({ nodes, links, selectedNode, onSelect }: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [resizeTick, setResizeTick] = useState(0);

  // Redraw when the SVG width changes (e.g. drawer opening/closing pushes the
  // main column). Without this, the chart layout stays frozen at its initial
  // width until something else in `analysis` changes.
  useEffect(() => {
    const svgEl = svgRef.current;
    if (!svgEl || typeof ResizeObserver === "undefined") return;
    let last = svgEl.clientWidth;
    const observer = new ResizeObserver(() => {
      const next = svgEl.clientWidth;
      if (next !== last) {
        last = next;
        setResizeTick((t) => t + 1);
      }
    });
    observer.observe(svgEl);
    return () => observer.disconnect();
  }, []);

  const grouped = useMemo(() => {
    const map = { input: [] as FlowNode[], filter: [] as FlowNode[], output: [] as FlowNode[] };
    nodes.forEach((n) => map[n.group].push(n));
    return map;
  }, [nodes]);

  const maxRows = Math.max(grouped.input.length, grouped.filter.length, grouped.output.length, 1);
  const height = Math.max(MIN_HEIGHT, TOP_PADDING + BOTTOM_PADDING + maxRows * ROW_HEIGHT);

  useEffect(() => {
    const svgEl = svgRef.current;
    if (!svgEl) return;
    const svg = d3.select(svgEl);
    svg.selectAll("*").remove();

    const width = svgEl.clientWidth;

    const xScale = d3
      .scalePoint<string>()
      .domain(["input", "filter", "output"])
      .range([80, width - 80]);

    const yScaleFor = (count: number) =>
      d3
        .scalePoint<number>()
        .domain(d3.range(count))
        .range([TOP_PADDING, height - BOTTOM_PADDING])
        .padding(0.5);

    const yByGroup = {
      input: yScaleFor(Math.max(grouped.input.length, 1)),
      filter: yScaleFor(Math.max(grouped.filter.length, 1)),
      output: yScaleFor(Math.max(grouped.output.length, 1)),
    };

    const indexById = new Map<string, { group: FlowNode["group"]; index: number }>();
    (Object.keys(grouped) as Array<FlowNode["group"]>).forEach((g) => {
      grouped[g].forEach((n, i) => indexById.set(n.id, { group: g, index: i }));
    });

    const posOf = (id: string) => {
      const ref = indexById.get(id);
      if (!ref) return { x: 0, y: 0 };
      return {
        x: xScale(ref.group) ?? 0,
        y: yByGroup[ref.group](ref.index) ?? 0,
      };
    };

    // Edges
    svg
      .append("g")
      .selectAll("line")
      .data(links)
      .enter()
      .append("line")
      .attr("x1", (d) => posOf(d.source).x)
      .attr("x2", (d) => posOf(d.target).x)
      .attr("y1", (d) => posOf(d.source).y)
      .attr("y2", (d) => posOf(d.target).y)
      .attr("stroke", "#b8b1a8")
      .attr("stroke-width", 1.2);

    const nodeGroup = svg
      .append("g")
      .selectAll("g")
      .data(nodes)
      .enter()
      .append("g")
      .attr("data-node-id", (d) => d.id)
      .attr("transform", (d) => {
        const p = posOf(d.id);
        return `translate(${p.x}, ${p.y})`;
      })
      .attr("tabindex", 0)
      .attr("role", "button")
      .attr("aria-label", (d) => `${d.group} ${d.label}`)
      .attr("aria-current", (d) => (d.id === selectedNode ? "true" : null))
      .style("cursor", "pointer")
      .style("outline", "none")
      .on("click", (_event, d) => {
        onSelect(d.id);
      })
      .on("keydown", (event, d) => {
        const ke = event as unknown as KeyboardEvent;
        if (ke.key === "Enter" || ke.key === " ") {
          ke.preventDefault();
          onSelect(d.id);
        }
      })
      .on("focus", function () {
        d3.select(this).select("circle").attr("stroke-width", 2.5);
      })
      .on("blur", function () {
        d3.select(this).select("circle").attr("stroke-width", 1);
      });

    nodeGroup
      .append("circle")
      .attr("r", 18)
      .attr("fill", (d) => colorFor(d.group, d.id === selectedNode))
      .attr("stroke", "#1e1a1f")
      .attr("stroke-width", 1);

    nodeGroup
      .append("text")
      .text((d) => d.label)
      .attr("y", -28)
      .attr("text-anchor", "middle")
      .attr("font-size", 12)
      .attr("font-weight", 500)
      .attr("fill", "#1e1a1f")
      .style("pointer-events", "none")
      .call(wrap, 140);

    // Column headers
    (["input", "filter", "output"] as const).forEach((g) => {
      svg
        .append("text")
        .text(g.toUpperCase())
        .attr("x", xScale(g) ?? 0)
        .attr("y", 24)
        .attr("text-anchor", "middle")
        .attr("font-size", 10)
        .attr("font-weight", 600)
        .attr("letter-spacing", "0.2em")
        .attr("fill", "#5e5760");
    });
  }, [nodes, links, selectedNode, onSelect, grouped, height, resizeTick]);

  return (
    <div className="rounded-[3px] border border-edge bg-white/70">
      <div className="flex items-center justify-end gap-3 px-3 pt-2 text-[11px] text-muted">
        <Legend swatch="#5f9ed6" label="Input" />
        <Legend swatch="#8d6cab" label="Filter" />
        <Legend swatch="#2f855a" label="Output" />
        <Legend swatch="#ffb347" label="Selected" />
      </div>
      <svg
        ref={svgRef}
        id="flow-chart"
        className="w-full"
        style={{ height: `${height}px` }}
        role="img"
        aria-label="Flow chart of inputs, filters, and outputs"
      />
    </div>
  );
}

function Legend({ swatch, label }: { swatch: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        aria-hidden="true"
        className="inline-block h-2.5 w-2.5 rounded-full"
        style={{ backgroundColor: swatch, border: "1px solid #1e1a1f" }}
      />
      {label}
    </span>
  );
}

function colorFor(group: string, selected: boolean) {
  if (selected) return "#ffb347";
  if (group === "input") return "#5f9ed6";
  if (group === "filter") return "#8d6cab";
  return "#2f855a";
}

function wrap(textSelection: d3.Selection<SVGTextElement, any, any, any>, width: number) {
  textSelection.each(function () {
    const text = d3.select(this);
    const words = text.text().split(/\s+/).reverse();
    let word;
    let line: string[] = [];
    let lineNumber = 0;
    const lineHeight = 1.1;
    const y = text.attr("y");
    const dy = 0;
    let tspan = text
      .text(null)
      .append("tspan")
      .attr("x", 0)
      .attr("y", y)
      .attr("dy", `${dy}em`);

    while ((word = words.pop())) {
      line.push(word);
      tspan.text(line.join(" "));
      if ((tspan.node() as SVGTextElement).getComputedTextLength() > width) {
        line.pop();
        tspan.text(line.join(" "));
        line = [word];
        tspan = text
          .append("tspan")
          .attr("x", 0)
          .attr("y", y)
          .attr("dy", `${++lineNumber * lineHeight + dy}em`)
          .text(word);
      }
    }
  });
}
