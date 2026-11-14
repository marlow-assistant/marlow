// What the composer stages before a send (docs/tasks/share-and-files.md,
// Phase 2): photos and files in one list, one cap. The client checks count and
// size and never type: anything it can decode as an image is a photo (the
// existing downscaler, sent as `images`), everything else is a file chip (sent
// as `files`). The server sniffs the bytes and decides how Marlow reads them,
// so no client carries a type list.

import { downscaleToJpeg, ImageUnsupportedError } from "./imageDownscale";

// The server is the authority for all three (server/app.py); these only let
// the composer refuse before the bytes are encoded and sent. Change them
// there first.
/** Photos and files together per message. */
export const MAX_ITEMS = 6;
/** Per file, decoded. Photos keep their own, smaller, limit server-side and
 * are downscaled long before it matters. */
export const MAX_FILE_BYTES = 20 * 1024 * 1024;
/** Per message, decoded, photos and files together. */
export const MAX_TOTAL_BYTES = 25 * 1024 * 1024;

export interface StagedItem {
  id: string;
  kind: "photo" | "file";
  /** A photo: the downscaled JPEG. A file: the File as picked, with its own
   * type, which is sent as the (decisionless) `mime`. */
  blob: Blob;
  name: string;
}

export const NOTICE_FILE_TOO_BIG = "That file is over 20MB.";
export const NOTICE_TOTAL_TOO_BIG = "That would put the message over 25MB.";

function nameOf(file: Blob): string {
  return file instanceof File && file.name ? file.name : "file";
}

/** Stages `files` into what `held()` returns, one at a time through `add`
 * (so the composer can show each as it lands, and a second pick that arrives
 * meanwhile still counts it). Returns the notice to show, or null.
 *
 * Over the cap, the first ones that fit are kept and the notice says the rest
 * were left out; a refused file is skipped and the others still stage. */
export async function stageBatch(
  files: (File | Blob)[],
  held: () => StagedItem[],
  add: (item: StagedItem) => void,
  downscale: (file: Blob) => Promise<Blob> = downscaleToJpeg,
  newId: () => string = () => crypto.randomUUID(),
): Promise<string | null> {
  const room = Math.max(0, MAX_ITEMS - held().length);
  // Kept so a per-file refusal below doesn't replace it: the user must still
  // learn the rest were left out.
  let capNote = "";
  if (files.length > room) {
    capNote = room === 0 ? `${MAX_ITEMS} items at most.` : `Only the first ${room} added: ${MAX_ITEMS} items at most.`;
  }
  let refusal = "";
  for (const file of files.slice(0, room)) {
    // A second pick that lands while this one decodes may have taken the room.
    if (held().length >= MAX_ITEMS) break;
    const total = () => held().reduce((n, i) => n + i.blob.size, 0);

    let item: StagedItem;
    try {
      const jpeg = await downscale(file);
      if (total() + jpeg.size > MAX_TOTAL_BYTES) {
        refusal = NOTICE_TOTAL_TOO_BIG;
        continue;
      }
      item = { id: newId(), kind: "photo", blob: jpeg, name: nameOf(file) };
    } catch (e) {
      // Not an image this browser can decode (or a canvas that failed): a
      // file, whatever it is. Only a real surprise is refused.
      if (!(e instanceof ImageUnsupportedError)) {
        refusal = "Couldn't read that file.";
        continue;
      }
      if (file.size > MAX_FILE_BYTES) {
        refusal = NOTICE_FILE_TOO_BIG;
        continue;
      }
      if (total() + file.size > MAX_TOTAL_BYTES) {
        refusal = NOTICE_TOTAL_TOO_BIG;
        continue;
      }
      item = { id: newId(), kind: "file", blob: file, name: nameOf(file) };
    }
    // The ref moves on at once, so a second pick landing before React renders
    // still sees this one when it counts.
    add(item);
  }
  return [refusal, capNote].filter(Boolean).join(" ") || null;
}
