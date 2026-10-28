// Which portrait Marlow wears in the header (port of iOS `MarlowFace`; keep the
// two tables in step). Box-wide since 2026-10-16 (lib/preferenceSync.ts). The
// home-screen icon and Android's launch splash follow it through the manifest,
// which the box serves with the chosen face's icons (server/web_static.py);
// Chrome picks up the change on a later launch, not at once. Notifications
// follow it too (sw.ts). After adding a face, run scripts/face-app-icons.sh;
// after changing a header crop, rerun scripts/face-notification-icons.sh.

export type FaceId = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11;

/** How one avatar size frames a portrait: an aspect-fill square scaled by
 * `zoom` about `anchor` (fractions of the portrait), then clipped round. Zoom 1
 * is the whole portrait. Per face because the newer portraits frame the head
 * about a fifth larger than face 1, so one zoom read as too tight on them; per
 * size because a crop that keeps a 52px face legible is too tight at 120px
 * (2026-10-16). One shared anchor cut most of the new faces at the chin. */
export interface FaceCrop {
  zoom: number;
  anchor: [number, number];
}

export type CropSize = "header" | "large";

export interface Face {
  id: FaceId;
  prop: "monocle" | "glasses";
  /** The 52px header avatar. */
  header: FaceCrop;
  /** Big circles: the picker, the empty chat's greeting. Looser than
   * `header`, since a bigger circle has room for more than the face. */
  large: FaceCrop;
}

export const FACES: Face[] = [
  { id: 1, prop: "monocle", header: { zoom: 1.49, anchor: [0.43, 0] }, large: { zoom: 1, anchor: [0.47, 0.36] } },
  { id: 2, prop: "monocle", header: { zoom: 1.12, anchor: [0.89, 0] }, large: { zoom: 1, anchor: [0.9, 0.78] } },
  { id: 3, prop: "monocle", header: { zoom: 1.5, anchor: [0.39, 0] }, large: { zoom: 1, anchor: [0.68, 0.3] } },
  { id: 4, prop: "glasses", header: { zoom: 1.39, anchor: [0.48, 0] }, large: { zoom: 1, anchor: [0.5, 0.55] } },
  { id: 5, prop: "glasses", header: { zoom: 1.29, anchor: [0.56, 0] }, large: { zoom: 1, anchor: [0.52, 0.64] } },
  { id: 6, prop: "monocle", header: { zoom: 1.32, anchor: [0.56, 0.12] }, large: { zoom: 1, anchor: [0.8, 0.62] } },
  { id: 7, prop: "monocle", header: { zoom: 1.2, anchor: [0.65, 0.05] }, large: { zoom: 1, anchor: [0.84, 0.55] } },
  { id: 8, prop: "monocle", header: { zoom: 1.35, anchor: [0.57, 0.03] }, large: { zoom: 1, anchor: [0.8, 0.55] } },
  { id: 9, prop: "monocle", header: { zoom: 1.45, anchor: [0.61, 0] }, large: { zoom: 1, anchor: [0.74, 0.43] } },
  { id: 10, prop: "monocle", header: { zoom: 1.38, anchor: [0.5, 0] }, large: { zoom: 1, anchor: [0.41, 0.55] } },
  { id: 11, prop: "monocle", header: { zoom: 1.39, anchor: [0.52, 0.22] }, large: { zoom: 1, anchor: [0.62, 0.68] } },
];

/** The original icon. An untouched install looks as it did before faces. */
export const DEFAULT_FACE: Face = FACES[0];

/** Whatever is stored, a face to show: an unknown value (a future build's
 * face, a damaged record) falls back rather than leaving an empty avatar. */
export function parseFace(v: unknown): Face {
  return FACES.find((f) => f.id === v) ?? DEFAULT_FACE;
}

export function faceSrc(face: Face): string {
  return `/faces/marlow-${face.id}.jpg`;
}

/** The face on a notification: the header crop, pre-cut, since a notification
 * takes an image URL and can't crop (scripts/face-notification-icons.sh). */
export function faceNotificationIcon(face: Face): string {
  return `/faces/icon-${face.id}.jpg`;
}

/** The CSS `transform-origin` for a crop's zoom. */
export function cropOrigin(crop: FaceCrop): string {
  return `${Math.round(crop.anchor[0] * 100)}% ${Math.round(crop.anchor[1] * 100)}%`;
}

export function faceLabel(face: Face): string {
  return `Face ${face.id}, ${face.prop}`;
}
