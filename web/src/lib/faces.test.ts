import { existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { DEFAULT_FACE, FACES, faceNotificationIcon, faceSrc, parseFace } from "./faces";

describe("faces", () => {
  it("defaults to face 1, the original icon", () => {
    expect(DEFAULT_FACE.id).toBe(1);
    expect(parseFace(undefined)).toBe(DEFAULT_FACE);
  });

  it("reads a stored id back as that face", () => {
    expect(parseFace(7).id).toBe(7);
  });

  // A future build's face, or a damaged record, must not leave an empty avatar.
  it("falls back to the default for anything it doesn't know", () => {
    expect(parseFace(42)).toBe(DEFAULT_FACE);
    expect(parseFace("7")).toBe(DEFAULT_FACE);
    expect(parseFace(null)).toBe(DEFAULT_FACE);
  });

  // A missing or misnamed file is a broken image in the header, and nothing
  // else notices.
  it("every face has its image and notification icon in public/", () => {
    for (const f of FACES) {
      // vitest runs from web/, where public/ is.
      expect(existsSync(join(process.cwd(), "public", faceSrc(f))), faceSrc(f)).toBe(true);
      expect(existsSync(join(process.cwd(), "public", faceNotificationIcon(f))), faceNotificationIcon(f)).toBe(true);
    }
  });
});
