import type { Issue } from "../types";

type Props = {
  issues: Issue[];
  onIssueClick?: (issue: Issue) => void;
};

type SeverityStyle = {
  label: string;
  icon: string;
  badgeClass: string;
  borderClass: string;
};

const SEVERITY_STYLES: Record<Issue["severity"], SeverityStyle> = {
  error: {
    label: "Error",
    icon: "✕",
    badgeClass: "bg-red-50 text-red-700 border-red-200",
    borderClass: "border-edge",
  },
  warning: {
    label: "Warning",
    icon: "!",
    badgeClass: "bg-amber-50 text-amber-800 border-amber-200",
    borderClass: "border-edge",
  },
  info: {
    label: "Info",
    icon: "i",
    badgeClass: "bg-sky-50 text-sky-700 border-sky-200",
    borderClass: "border-edge",
  },
};

export function DiagnosticsPanel({ issues, onIssueClick }: Props) {
  return (
    <section className="rounded-[3px] border border-edge bg-panel p-5 shadow-panel">
      <div className="text-xs font-semibold uppercase tracking-wider text-muted">Diagnostics</div>
      <div className="mt-3 flex flex-col gap-2">
        {issues.length === 0 ? (
          <div className="flex items-center gap-2 rounded-[3px] border border-dashed border-edge bg-transparent px-3 py-2 text-sm text-muted">
            <span
              aria-hidden="true"
              className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-edge text-[11px] font-semibold text-muted"
            >
              ✓
            </span>
            No issues detected.
          </div>
        ) : (
          issues.map((issue) => {
            const style = SEVERITY_STYLES[issue.severity];
            const interactive = Boolean(onIssueClick && issue.tokenIds.length > 0);
            const Tag: "button" | "div" = interactive ? "button" : "div";
            return (
              <Tag
                key={issue.id}
                type={interactive ? "button" : undefined}
                onClick={interactive ? () => onIssueClick?.(issue) : undefined}
                className={`flex items-start gap-3 rounded-[3px] border ${style.borderClass} bg-white p-3 text-left text-sm ${
                  interactive
                    ? "cursor-pointer hover:bg-blue-50 focus:bg-blue-50 focus:outline-none focus:ring-2 focus:ring-blue-200"
                    : ""
                }`}
                title={interactive ? "Click to locate in the command" : undefined}
              >
                <span
                  aria-hidden="true"
                  className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[12px] font-bold ${style.badgeClass}`}
                >
                  {style.icon}
                </span>
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-block rounded-[3px] border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${style.badgeClass}`}
                    >
                      {style.label}
                    </span>
                    <strong className="text-sm font-semibold text-ink">{issue.message}</strong>
                  </div>
                  <div className="mt-1 text-sm text-ink/80">{issue.explanation}</div>
                </div>
              </Tag>
            );
          })
        )}
      </div>
    </section>
  );
}
