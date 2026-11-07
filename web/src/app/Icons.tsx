import type { ReactNode } from "react";

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
