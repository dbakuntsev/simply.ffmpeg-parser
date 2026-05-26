import { useId } from "react";

type Props = {
  versions: string[];
  version: string;
  onChange: (value: string) => void;
};

export function VersionSelector({ versions, version, onChange }: Props) {
  const id = useId();
  return (
    <div className="flex items-center gap-2">
      <label
        htmlFor={id}
        className="text-xs font-semibold uppercase tracking-wider text-muted"
      >
        FFmpeg Version
      </label>
      <select
        id={id}
        className="rounded-[3px] border border-edge bg-white/70 px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-200"
        value={version}
        onChange={(e) => onChange(e.target.value)}
      >
        {versions.length === 0 && <option>No metadata found</option>}
        {versions.map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
    </div>
  );
}
