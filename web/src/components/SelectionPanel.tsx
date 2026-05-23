import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import type { SelectionInfo } from "../selection";

marked.setOptions({ gfm: true, breaks: false });

function renderDescriptionHtml(paragraphs: string[]): string {
  // Each entry is its own Markdown paragraph/block; join with a blank line so
  // they parse independently (lists and fenced code don't bleed together).
  return marked.parse(paragraphs.join("\n\n"), { async: false }) as string;
}

// Drawer mode kicks in at the Tailwind `lg` breakpoint. Kept in sync with
// the matching `lg:` utilities in App.tsx that push content aside.
const LG_QUERY = "(min-width: 1024px)";

function useIsLg() {
  const [isLg, setIsLg] = useState(() =>
    typeof window !== "undefined" && window.matchMedia(LG_QUERY).matches
  );
  useEffect(() => {
    const mq = window.matchMedia(LG_QUERY);
    const handler = (event: MediaQueryListEvent) => setIsLg(event.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isLg;
}

type Props = {
  selection: SelectionInfo;
  onClose: () => void;
};

export function SelectionPanel({ selection, onClose }: Props) {
  const isLg = useIsLg();
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  // Modal mode locks the body so the page can't scroll out from under it.
  // Drawer mode coexists with the page, so we leave scrolling intact.
  useEffect(() => {
    if (isLg) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [isLg]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    closeButtonRef.current?.focus();
  }, [selection]);

  const hasFields = !!(selection.fields && selection.fields.length > 0);
  const hasDescription = !!(selection.description && selection.description.length > 0);
  const hasExtraDocs = !!(selection.extraDocs && selection.extraDocs.length > 0);
  const useRichLayout = hasFields || hasDescription || selection.docsUrl || hasExtraDocs;
  const descriptionHtml = useMemo(
    () => (hasDescription ? renderDescriptionHtml(selection.description!) : ""),
    [hasDescription, selection.description]
  );

  const content = (
    <>
      <div className="flex flex-shrink-0 items-start justify-between gap-2 border-b border-edge bg-panel px-4 py-3">
        <strong className="text-sm font-semibold text-ink">{selection.title}</strong>
        <button
          ref={closeButtonRef}
          type="button"
          aria-label="Close"
          className="inline-flex h-7 w-7 items-center justify-center rounded-[3px] text-muted hover:bg-edge focus:outline-none focus:ring-2 focus:ring-blue-200"
          onClick={onClose}
        >
          <span aria-hidden="true" className="text-lg leading-none">×</span>
        </button>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {useRichLayout ? (
          <>
            {hasFields && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px]">
                {selection.fields!.map((field) => (
                  <div key={field.label} className="contents">
                    <dt className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                      {field.label}
                    </dt>
                    <dd className="font-mono break-all whitespace-pre-line text-ink">{field.value}</dd>
                  </div>
                ))}
              </dl>
            )}
            {hasDescription && (
              <div
                className="popover-md text-[13px] text-ink/90"
                dangerouslySetInnerHTML={{ __html: descriptionHtml }}
              />
            )}
          </>
        ) : (
          <div className="whitespace-pre-line text-sm text-ink">{selection.detail}</div>
        )}
      </div>
      {(selection.docsUrl || hasExtraDocs) && (
        <footer className="flex flex-shrink-0 flex-wrap gap-x-4 gap-y-1 border-t border-edge bg-panel px-4 py-3">
          {selection.docsUrl && (
            <DocLink href={selection.docsUrl} label="FFmpeg docs" />
          )}
          {selection.extraDocs?.map((link) => (
            <DocLink key={link.url} href={link.url} label={link.label} />
          ))}
        </footer>
      )}
    </>
  );

  if (isLg) {
    return (
      <aside
        role="complementary"
        aria-label={selection.title}
        className="fixed inset-y-0 right-0 z-40 flex w-[480px] flex-col border-l border-edge bg-white shadow-panel"
        style={{ animation: "drawer-slide-in 100ms cubic-bezier(0, 0, 0.2, 1) forwards" }}
      >
        {content}
      </aside>
    );
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={selection.title}
      className="fixed inset-0 z-50"
    >
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
      />
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-3">
        <div className="pointer-events-auto flex w-full max-w-[560px] max-h-full flex-col rounded-[6px] border border-edge bg-white shadow-panel">
          {content}
        </div>
      </div>
    </div>
  );
}

function DocLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="ffmpeg-docs"
      rel="noreferrer"
      onClick={(event) => {
        // Drive the navigation via window.open so the named target
        // ("ffmpeg-docs") actually reuses the previously opened tab.
        // Plain <a target="..."> with rel="noopener" gets routed into
        // an isolated browsing-context group by Chromium/Firefox and
        // ends up opening a fresh tab every click.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
        event.preventDefault();
        const win = window.open(href, "ffmpeg-docs");
        win?.focus();
      }}
      className="inline-flex items-center gap-1 text-[12px] font-semibold text-blue-700 hover:underline focus:outline-none focus:ring-2 focus:ring-blue-200"
    >
      {label} <span aria-hidden="true">↗</span>
    </a>
  );
}
