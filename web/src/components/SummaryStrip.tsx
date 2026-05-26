import type { SemanticCommand } from "../types";

type Props = {
  semantic: SemanticCommand | null;
};

type Chip = {
  label: string;
  tone: "neutral" | "input" | "filter" | "output" | "codec";
};

const TONE_CLASSES: Record<Chip["tone"], string> = {
  neutral: "bg-white text-ink border-edge",
  input: "bg-white text-ink border-edge",
  filter: "bg-white text-ink border-edge",
  output: "bg-white text-ink border-edge",
  codec: "bg-[#f6f8fa] text-muted border-edge",
};

function collectCodecs(semantic: SemanticCommand): { video: string[]; audio: string[] } {
  const video = new Set<string>();
  const audio = new Set<string>();
  semantic.outputs.forEach((out) => {
    out.options.forEach((opt) => {
      const flag = opt.flag.toLowerCase();
      const value = opt.values[0];
      if (!value) return;
      if (flag === "-c:v" || flag === "-vcodec") video.add(value);
      else if (flag === "-c:a" || flag === "-acodec") audio.add(value);
      else if (flag === "-c") {
        // unspecified -c applies to both
        video.add(value);
        audio.add(value);
      }
    });
  });
  return { video: [...video], audio: [...audio] };
}

export function SummaryStrip({ semantic }: Props) {
  if (!semantic) return null;
  const inputCount = semantic.inputs.length;
  const filterCount = semantic.filters.reduce(
    (acc, f) => acc + (f.chains?.length ?? 1),
    0
  );
  const outputCount = semantic.outputs.length;
  const { video, audio } = collectCodecs(semantic);

  const chips: Chip[] = [
    { label: `${inputCount} input${inputCount === 1 ? "" : "s"}`, tone: "input" },
    { label: `${filterCount} filter${filterCount === 1 ? "" : "s"}`, tone: "filter" },
    { label: `${outputCount} output${outputCount === 1 ? "" : "s"}`, tone: "output" },
  ];

  video.forEach((v) => chips.push({ label: `video: ${v}`, tone: "codec" }));
  audio.forEach((a) => chips.push({ label: `audio: ${a}`, tone: "codec" }));

  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="Command summary">
      {chips.map((chip, index) => (
        <span
          key={`${chip.label}-${index}`}
          className={`inline-flex items-center rounded-[3px] border px-2 py-0.5 text-xs ${TONE_CLASSES[chip.tone]}`}
        >
          {chip.label}
        </span>
      ))}
    </div>
  );
}
