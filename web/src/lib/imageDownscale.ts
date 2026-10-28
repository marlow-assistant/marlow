// CMP-3: downscale in the client before the image touches the network. Long
// edge 1568px (where the model's own tiling stops gaining detail), JPEG
// quality 0.6 (about 300KB for a phone photo), EXIF orientation applied,
// never upscaled. iOS: `ImageDownscaler`.

/** Images per message; the server refuses more (MAX_IMAGES in server/app.py). */
export const MAX_IMAGES = 6;
export const LONG_EDGE = 1568;
export const JPEG_QUALITY = 0.6;

/** The output size: scaled so the long edge is at most LONG_EDGE, never up. */
export function targetSize(width: number, height: number): { width: number; height: number } {
  if (!(width > 0) || !(height > 0)) return { width: 0, height: 0 };
  const scale = Math.min(1, LONG_EDGE / Math.max(width, height));
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

export class ImageUnsupportedError extends Error {
  constructor() {
    // WEB-6: a HEIC a desktop browser cannot decode is refused with a message
    // rather than sent as something Marlow cannot read.
    super("This browser can't read that image. Try a JPEG or PNG.");
    this.name = "ImageUnsupportedError";
  }
}

export async function downscaleToJpeg(file: Blob): Promise<Blob> {
  let bitmap: ImageBitmap;
  try {
    // `from-image` applies the EXIF orientation, so a portrait phone photo is
    // not sent sideways.
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    throw new ImageUnsupportedError();
  }
  const { width, height } = targetSize(bitmap.width, bitmap.height);
  if (width === 0) throw new ImageUnsupportedError();
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new ImageUnsupportedError();
  // WEB-6: a transparent PNG drawn straight to JPEG comes out black.
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
  if (!blob) throw new ImageUnsupportedError();
  return blob;
}
