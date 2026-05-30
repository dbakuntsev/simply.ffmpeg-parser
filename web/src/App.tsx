import { useEffect, useMemo, useRef, useState } from "react";
import { CommandInput } from "./components/CommandInput";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { PipelineChart } from "./components/PipelineChart";
import { SelectionPanel } from "./components/SelectionPanel";
import { SummaryStrip } from "./components/SummaryStrip";
import { TreeList } from "./components/TreeList";
import { VersionSelector } from "./components/VersionSelector";
import { useMetadata } from "./hooks/useMetadata";
import { useSelection } from "./hooks/useSelection";
import { analyzeCommand, buildPipelineModel, buildTreeNodes } from "./parser";
import { buildSelectionInfo } from "./selection";
import { buildSourceRanges } from "./sourceRanges";
import { scrollSelectionIntoView } from "./textareaCaret";
import type { Issue } from "./types";

const SAMPLE = `ffmpeg -i input.mp4 -vf "scale=1280:-1" -c:v libx264 -c:a aac output.mp4`;
const ANALYZE_DEBOUNCE_MS = 500;

export default function App() {
  const [command, setCommand] = useState(SAMPLE);
  const [submitted, setSubmitted] = useState(SAMPLE);
  const { versions, version, setVersion, metadata, lookups, versionTokens } = useMetadata();
  const { selectedNode, select, clear } = useSelection();

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Debounced auto-analyze
  useEffect(() => {
    const handle = window.setTimeout(() => {
      setSubmitted(command.trim());
    }, ANALYZE_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [command]);

  const analysis = useMemo(() => {
    if (!metadata) return null;
    return analyzeCommand(submitted, metadata, lookups ?? undefined);
  }, [submitted, metadata, lookups]);

  const issues = analysis?.issues ?? [];
  const pipeline = useMemo(
    () => (analysis ? buildPipelineModel(analysis.semantic) : { boxes: [], edges: [] }),
    [analysis]
  );
  const treeNodes = useMemo(() => (analysis ? buildTreeNodes(analysis.semantic) : []), [analysis]);

  const selectionInfo = useMemo(
    () =>
      analysis && metadata && lookups
        ? buildSelectionInfo(analysis, metadata, lookups, version, versionTokens)
        : new Map(),
    [analysis, metadata, lookups, version, versionTokens]
  );
  const selection = selectedNode ? selectionInfo.get(selectedNode) ?? null : null;

  // Map node ids → command text spans so selecting a chart/tree node highlights
  // the matching text in the textarea.
  const sourceRanges = useMemo(
    () => (analysis ? buildSourceRanges(submitted, analysis) : new Map()),
    [analysis, submitted]
  );

  useEffect(() => {
    if (!selectedNode) return;
    const el = textareaRef.current;
    if (!el) return;
    // Ranges are computed against the analyzed command; skip if the textarea has
    // since been edited (debounce not yet flushed) to avoid highlighting stale
    // offsets.
    if (el.value !== submitted) return;
    const range = sourceRanges.get(selectedNode);
    if (!range) return;
    // ``preventScroll`` keeps Firefox from scrolling the *page* so the textarea
    // is visible — it otherwise reveals the textarea via the document scroll
    // when focusing, which we don't want. We do our own scrolling of the
    // textarea's own content below.
    el.focus({ preventScroll: true });
    el.setSelectionRange(range.start, range.end);
    // Scroll the textarea's content so the highlighted span is visible (the
    // command may be long enough to scroll). setSelectionRange doesn't do this.
    scrollSelectionIntoView(el, range.start);
  }, [selectedNode, sourceRanges, submitted]);

  const handleIssueClick = (issue: Issue) => {
    if (!analysis || !textareaRef.current) return;
    const tokenId = issue.tokenIds[0];
    const token = analysis.tokens.find((t) => t.id === tokenId);
    if (!token) return;
    const { start, end } = token.sourceRange;
    const el = textareaRef.current;
    el.focus();
    el.setSelectionRange(start, end);
    el.scrollIntoView({ block: "center", behavior: "smooth" });
  };

  return (
    <div
      className={`lg:h-screen lg:overflow-y-auto ${selection ? "lg:mr-[30rem]" : ""}`}
      style={{ transition: "margin 100ms cubic-bezier(0, 0, 0.2, 1)" }}
    >
    <div className="relative mx-auto flex w-full max-w-6xl flex-col gap-6 px-6 py-8 overflow-x-hidden">
      <header className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div className="flex flex-col gap-3">
          <h1 className="text-xl font-semibold tracking-tight text-ink">Simply FFmpeg Parser</h1>
          <p className="max-w-2xl text-sm text-muted">
            Explore FFmpeg commands without running the binary. This SPA tokenizes, resolves scope, detects issues, and
            visualizes the flow.
          </p>
        </div>
        <VersionSelector versions={versions} version={version} onChange={setVersion} />
      </header>

      <CommandInput ref={textareaRef} command={command} onCommandChange={setCommand} />

      <DiagnosticsPanel issues={issues} onIssueClick={handleIssueClick} />

      <section className="rounded-[3px] border border-edge bg-panel p-5 shadow-panel">
        <div className="flex items-center justify-between gap-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted">Visualization</div>
        </div>
        <div className="mt-3">
          <SummaryStrip semantic={analysis?.semantic ?? null} />
        </div>
        <div className="mt-4 grid gap-4">
          <PipelineChart
            model={pipeline}
            selectedNode={selectedNode}
            onSelect={select}
          />
          <div className="max-h-[520px] overflow-auto rounded-[3px] border border-edge bg-white/70 p-3">
            <TreeList
              nodes={treeNodes}
              selected={selectedNode}
              onSelect={select}
            />
          </div>
        </div>
      </section>

      {selection && (
        <SelectionPanel selection={selection} onClose={clear} />
      )}

      <footer className="mt-2 border-t border-edge pt-4 text-xs text-muted">
        <a className="underline hover:text-ink" href={`${import.meta.env.BASE_URL}THIRD-PARTY-NOTICES.html`}>
          Licenses &amp; attribution
        </a>
      </footer>
    </div>
    </div>
  );
}
