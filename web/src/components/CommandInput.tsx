import { forwardRef, useEffect, useId, useRef, useState } from "react";

export type ExamplePreset = {
  id: string;
  label: string;
  description: string;
  command: string;
};

export const EXAMPLE_PRESETS: ExamplePreset[] = [
  {
    id: "transcode",
    label: "Transcode",
    description: "Re-encode H.264 + AAC with CRF 23",
    command:
      'ffmpeg -i input.mp4 -c:v libx264 -crf 23 -preset slow -c:a aac -b:a 192k output.mp4',
  },
  {
    id: "concat",
    label: "Concat",
    description: "Concatenate two clips via concat demuxer",
    command:
      'ffmpeg -f concat -i list.txt -c:v copy -c:a copy output.mp4',
  },
  {
    id: "overlay",
    label: "Overlay",
    description: "Overlay a second input with filter_complex",
    command:
      'ffmpeg -i base.mp4 -i logo.png -filter_complex "[0:v][1:v]overlay=10:10[v]" -map "[v]" -map 0:a? -c:v libx264 -c:a aac -b:a 192k output.mp4',
  },
  {
    id: "audio-only",
    label: "Audio only",
    description: "Strip video, encode audio as 192 kbps MP3",
    command:
      'ffmpeg -i input.mp4 -vn -c:a libmp3lame -b:a 192k output.mp3',
  },
];

type Props = {
  command: string;
  onCommandChange: (value: string) => void;
};

export const CommandInput = forwardRef<HTMLTextAreaElement, Props>(function CommandInput(
  { command, onCommandChange },
  ref
) {
  const labelId = useId();
  const [examplesOpen, setExamplesOpen] = useState(false);
  const examplesRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!examplesOpen) return;
    const onDocMouseDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target && examplesRef.current && !examplesRef.current.contains(target)) {
        setExamplesOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExamplesOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [examplesOpen]);

  return (
    <section className="rounded-[3px] border border-edge bg-panel p-5 shadow-panel">
      <div className="flex items-center justify-between gap-3">
        <label
          id={labelId}
          htmlFor={`${labelId}-textarea`}
          className="text-xs font-semibold uppercase tracking-wider text-muted"
        >
          Command
        </label>
        <div ref={examplesRef} className="relative">
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={examplesOpen}
            className="inline-flex items-center gap-1 rounded-[3px] border border-edge bg-white/70 px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-muted hover:bg-white"
            onClick={() => setExamplesOpen((v) => !v)}
          >
            Examples
            <span aria-hidden="true" className="text-[10px]">{examplesOpen ? "▴" : "▾"}</span>
          </button>
          {examplesOpen && (
            <div
              role="menu"
              aria-label="Example commands"
              className="absolute right-0 z-30 mt-1 w-64 rounded-[3px] border border-edge bg-white shadow-panel"
            >
              {EXAMPLE_PRESETS.map((preset) => (
                <button
                  key={preset.id}
                  type="button"
                  role="menuitem"
                  className="block w-full text-left px-3 py-2 text-sm hover:bg-blue-50 focus:bg-blue-50 focus:outline-none"
                  onClick={() => {
                    onCommandChange(preset.command);
                    setExamplesOpen(false);
                  }}
                >
                  <span className="block font-semibold text-ink">{preset.label}</span>
                  <span className="block text-xs text-muted">{preset.description}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      <textarea
        id={`${labelId}-textarea`}
        ref={ref}
        className="mt-3 w-full min-h-[160px] rounded-[3px] border border-edge bg-white/70 p-3 font-mono text-sm leading-relaxed focus:outline-none focus:ring-2 focus:ring-blue-200"
        value={command}
        spellCheck={false}
        onChange={(e) => onCommandChange(e.target.value)}
        aria-labelledby={labelId}
      />
    </section>
  );
});
