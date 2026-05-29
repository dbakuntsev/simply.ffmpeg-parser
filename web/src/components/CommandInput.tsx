import { forwardRef, useEffect, useId, useRef, useState } from "react";

export type ExamplePreset = {
  id: string;
  label: string;
  description: string;
  command: string;
};

export type ExampleCategory = {
  id: string;
  label: string;
  presets: ExamplePreset[];
};

export const EXAMPLE_CATEGORIES: ExampleCategory[] = [
  {
    id: "basics",
    label: "Basics",
    presets: [
      {
        id: "transcode",
        label: "Transcode",
        description: "Re-encode H.264 + AAC with CRF 23",
        command:
          "ffmpeg -i input.mp4 -c:v libx264 -crf 23 -preset slow -c:a aac -b:a 192k output.mp4",
      },
      {
        id: "concat",
        label: "Concat",
        description: "Concatenate two clips via concat demuxer",
        command: "ffmpeg -f concat -i list.txt -c:v copy -c:a copy output.mp4",
      },
    ],
  },
  {
    id: "compositing",
    label: "Compositing & overlays",
    presets: [
      {
        id: "watermark-corner",
        label: "Watermark (corner)",
        description: "Overlay a logo 10px from the bottom-right",
        command:
          'ffmpeg -i input.mp4 -i logo.png -filter_complex "overlay=main_w-overlay_w-10:main_h-overlay_h-10" output.mp4',
      },
      {
        id: "watermark-two",
        label: "Two watermarks",
        description: "Place logos in two corners via chained overlays",
        command:
          'ffmpeg -i input.mp4 -i logo1.png -i logo2.png -filter_complex "overlay=x=10:y=H-h-10,overlay=x=W-w-10:y=H-h-10" output.mp4',
      },
      {
        id: "chromakey",
        label: "Green screen",
        description: "Key out green and composite over a background clip",
        command:
          'ffmpeg -f lavfi -i color=c=black:s=1280x720 -i video.mp4 -shortest -filter_complex "[1:v]chromakey=0x70de77:0.1:0.2[ckout];[0:v][ckout]overlay[out]" -map "[out]" output.mkv',
      },
      {
        id: "split-crop-mirror",
        label: "Split / crop / mirror",
        description: "Mirror the bottom half of the frame",
        command:
          'ffmpeg -i input.mp4 -vf "split [main][tmp]; [tmp] crop=iw:ih/2:0:0, vflip [flip]; [main][flip] overlay=0:H/2" output.mp4',
      },
      {
        id: "side-by-side",
        label: "Side-by-side mosaic",
        description: "Scale two inputs onto one canvas",
        command:
          'ffmpeg -i left.avi -i right.avi -filter_complex "nullsrc=size=200x100 [background];[0:v] setpts=PTS-STARTPTS, scale=100x100 [left];[1:v] setpts=PTS-STARTPTS, scale=100x100 [right];[background][left] overlay=shortest=1 [background+left];[background+left][right] overlay=shortest=1:x=100 [left+right]" output.mp4',
      },
      {
        id: "xfade",
        label: "Crossfade transition",
        description: "Fade between two clips with a 2s overlap",
        command:
          "ffmpeg -i first.mp4 -i second.mp4 -filter_complex xfade=transition=fade:duration=2:offset=5 output.mp4",
      },
    ],
  },
  {
    id: "audio",
    label: "Audio",
    presets: [
      {
        id: "audio-only",
        label: "Audio only",
        description: "Strip video, encode audio as 192 kbps MP3",
        command: "ffmpeg -i input.mp4 -vn -c:a libmp3lame -b:a 192k output.mp3",
      },
      {
        id: "amix",
        label: "Mix two tracks",
        description: "Blend vocals + music with per-input weights",
        command:
          'ffmpeg -i vocals.wav -i music.wav -filter_complex amix=inputs=2:duration=longest:dropout_transition=0:weights="1 0.25":normalize=0 output.wav',
      },
      {
        id: "amerge",
        label: "Merge channels",
        description: "Combine 6 mono streams into one 5.1 track",
        command:
          'ffmpeg -i input.mkv -filter_complex "[0:1][0:2][0:3][0:4][0:5][0:6] amerge=inputs=6" -c:a pcm_s16le output.mkv',
      },
      {
        id: "acrossfade",
        label: "Audio crossfade",
        description: "Splice two clips with a 10s fade",
        command:
          "ffmpeg -i first.flac -i second.flac -filter_complex acrossfade=d=10:c1=exp:c2=exp output.flac",
      },
      {
        id: "sidechain",
        label: "Sidechain ducking",
        description: "Duck music under a sidechain signal",
        command:
          'ffmpeg -i main.flac -i sidechain.flac -filter_complex "[1:a]asplit=2[sc][mix];[0:a][sc]sidechaincompress[compr];[compr][mix]amerge" output.flac',
      },
      {
        id: "acrossover",
        label: "Split frequency bands",
        description: "Route low/high bands to separate files",
        command:
          "ffmpeg -i in.flac -filter_complex 'acrossover=split=1500[LOW][HIGH]' -map '[LOW]' low.wav -map '[HIGH]' high.wav",
      },
    ],
  },
  {
    id: "gif-analysis",
    label: "GIF, thumbnails & analysis",
    presets: [
      {
        id: "palettegen",
        label: "GIF palette generation",
        description: "Build an optimized palette for high-quality GIFs",
        command: "ffmpeg -i input.mkv -vf palettegen palette.png",
      },
      {
        id: "paletteuse",
        label: "GIF with palette",
        description: "Render a GIF using a precomputed palette",
        command: "ffmpeg -i input.mkv -i palette.png -lavfi paletteuse output.gif",
      },
      {
        id: "scene-contact-sheet",
        label: "Scene-change contact sheet",
        description: "Tile scene-cut frames into one preview image",
        command:
          "ffmpeg -i video.avi -vf select='gt(scene\\,0.4)',scale=160:120,tile -frames:v 1 preview.png",
      },
      {
        id: "thumbnail",
        label: "Smart thumbnail",
        description: "Pick a representative frame and resize it",
        command: "ffmpeg -i in.avi -vf thumbnail,scale=300:200 -frames:v 1 out.png",
      },
      {
        id: "extractplanes",
        label: "Extract Y/U/V planes",
        description: "Split a video into its three planes",
        command:
          "ffmpeg -i video.avi -filter_complex 'extractplanes=y+u+v[y][u][v]' -map '[y]' y.avi -map '[u]' u.avi -map '[v]' v.avi",
      },
    ],
  },
  {
    id: "hardware",
    label: "Hardware acceleration",
    presets: [
      {
        id: "gpu-transcode",
        label: "GPU transcode (NVENC/CUDA)",
        description: "Decode, pad, and encode entirely on GPU",
        command:
          'ffmpeg -hwaccel cuda -hwaccel_output_format cuda -i input.mp4 -vf "pad_cuda=w=iw+400:h=ih+400:x=200:y=200" -c:v h264_nvenc out.mp4',
      },
    ],
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
              className="absolute right-0 z-30 mt-1 max-h-[70vh] w-72 overflow-y-auto rounded-[3px] border border-edge bg-white shadow-panel"
            >
              {EXAMPLE_CATEGORIES.map((category) => (
                <div key={category.id} role="group" aria-label={category.label}>
                  <div className="sticky top-0 z-10 border-y border-edge bg-ink px-3 py-2 text-[11px] font-bold uppercase tracking-wider text-white">
                    {category.label}
                  </div>
                  {category.presets.map((preset) => (
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
