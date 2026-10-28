// Marlow's chosen face, shared by every screen that shows it: the header, the
// greeting, pairing, Settings and the Marlow sheet.
import { useEffect, useRef } from "react";
import { cropOrigin, FACES, faceLabel, faceSrc, type CropSize, type Face } from "../lib/faces";

/** The chosen portrait zoomed toward its face (iOS `MarlowAvatar`). The
 * parent clips it round: at avatar size the whole portrait reads as a dark
 * smudge, and the face is what makes it Marlow. */
export function FaceImage({ face, size, crop }: { face: Face; size: number; crop: CropSize }) {
  const framing = face[crop];
  return (
    <img
      className="face-img"
      src={faceSrc(face)}
      alt=""
      width={size}
      height={size}
      style={{ transform: `scale(${framing.zoom})`, transformOrigin: cropOrigin(framing) }}
    />
  );
}

/** Every face, large, as a radio group (iOS `FacePicker`). Settings shows it
 * inline; the Marlow sheet opens it from the face at its top, and asks for
 * `scrollToCurrent`: a later face sat below the fold, so the picker opened
 * showing no selection (Mike, 2026-10-16). Settings doesn't, since there the
 * grid is one section of many. */
export function FaceGrid({ face, onFace, scrollToCurrent = false }: { face: Face; onFace: (f: Face) => void; scrollToCurrent?: boolean }) {
  const current = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    // Mount only: a pick must not scroll the grid under the finger.
    if (scrollToCurrent) current.current?.scrollIntoView?.({ block: "center" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="face-grid" role="radiogroup" aria-label="Appearance">
      {FACES.map((f) => (
        <button
          key={f.id}
          ref={face.id === f.id ? current : undefined}
          type="button"
          role="radio"
          aria-checked={face.id === f.id}
          aria-label={faceLabel(f)}
          className="face-tile"
          onClick={() => onFace(f)}
        >
          <span className="face-circle">
            <FaceImage face={f} size={120} crop="large" />
          </span>
        </button>
      ))}
    </div>
  );
}
