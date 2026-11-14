import type { ReactNode } from "react";
import type { FileKind } from "../lib/fileInfo";

/** The voice icons, after the SF Symbols iOS uses. Drawn rather than typed:
 * as text they came from each device's emoji font, a colour picture on one
 * phone and a squiggle on another (Mike, 2026-11-06). `size` may be "1em", to
 * follow a button's font size. */
function Icon({ name, size, strokeWidth = 1.6, children }: { name: string; size: number | string; strokeWidth?: number; children: ReactNode }) {
  return (
    <svg
      data-icon={name}
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

/** `mic` */
export function MicIcon({ size = 22 }: { size?: number | string }) {
  return (
    <Icon name="mic" size={size}>
      <rect x="9" y="2.5" width="6" height="12" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3.5M9 21.5h6" />
    </Icon>
  );
}

/** `waveform`: uneven, as speech is. */
export function WaveformIcon({ size = 24 }: { size?: number | string }) {
  return (
    <Icon name="waveform" size={size} strokeWidth={1.8}>
      <path d="M3 11v2M6 8.5v7M9 5v14M12 9v6M15 3v18M18 7v10M21 10.5v3" />
    </Icon>
  );
}

/** `stop.circle` */
export function StopIcon({ size = 30 }: { size?: number | string }) {
  return (
    <Icon name="stop" size={size} strokeWidth={1.2}>
      <circle cx="12" cy="12" r="10.5" />
      <rect x="8" y="8" width="8" height="8" rx="1.5" fill="currentColor" stroke="none" />
    </Icon>
  );
}

/** `speaker.wave.2` */
export function SpeakerIcon({ size = 24 }: { size?: number | string }) {
  return (
    <Icon name="speaker" size={size}>
      <path d="M3.5 9.5v5h3.5l4.5 4v-13l-4.5 4z" />
      <path d="M15 9a4.2 4.2 0 0 1 0 6M17.8 6.5a8 8 0 0 1 0 11" />
    </Icon>
  );
}

/** `paperclip`: the "Choose file" item of the attach menu. */
export function PaperclipIcon({ size = 20 }: { size?: number | string }) {
  return (
    <Icon name="paperclip" size={size}>
      <path d="m20 11.5-8.2 8.2a5 5 0 0 1-7.1-7.1l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8" />
    </Icon>
  );
}

/** A page with a mark that says roughly what is on it, from the name's
 * extension (`fileKind`). Display only: it never decides what may be
 * attached. */
export function FileIcon({ kind, size = 24 }: { kind: FileKind; size?: number | string }) {
  return (
    <Icon name={`file-${kind}`} size={size} strokeWidth={1.5}>
      <path d="M6 2.5h8.5L19 7v13a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 20V4A1.5 1.5 0 0 1 6 2.5z" />
      <path d="M14.5 2.5V7H19" />
      {FILE_MARKS[kind]}
    </Icon>
  );
}

const FILE_MARKS: Record<FileKind, ReactNode> = {
  pdf: <path d="M8 17h8M8 14h3" />,
  document: <path d="M8 11h8M8 14h8M8 17h5" />,
  text: <path d="M8 11h8M8 14h8M8 17h5" />,
  sheet: <path d="M8 11h8M8 14h8M8 17h8M11 11v6" />,
  slides: <rect x="8" y="11" width="8" height="5.5" rx="1" />,
  archive: <path d="M11 9h2M11 12h2M11 15h2M11 18h2" />,
  audio: <path d="M10 17.5v-5.5l5-1.2v5.5M10 17.5a1.4 1.4 0 1 1-2.8 0 1.4 1.4 0 0 1 2.8 0zM15 16.3a1.4 1.4 0 1 1-2.8 0 1.4 1.4 0 0 1 2.8 0z" />,
  video: <path d="M10 11.5v5l4.5-2.5z" />,
  image: <path d="m8 17 2.7-3 2 2 1.3-1.5L16 17M9.5 11.5h.01" />,
  other: <path d="M9 14h6" />,
};
