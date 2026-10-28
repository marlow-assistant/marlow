import { describe, expect, test } from "vitest";
import { LONG_EDGE, targetSize } from "./imageDownscale";

// The pixel work runs on a canvas, which jsdom does not have; the size rule is
// what ImageDownscalerTests pins on iOS, and it is pure.
describe("targetSize (CMP-3)", () => {
  test("a landscape photo is scaled so its long edge is 1568", () => {
    expect(targetSize(4032, 3024)).toEqual({ width: LONG_EDGE, height: 1176 });
  });

  test("a portrait photo is scaled on its height", () => {
    expect(targetSize(3024, 4032)).toEqual({ width: 1176, height: LONG_EDGE });
  });

  test("a small image is never upscaled", () => {
    expect(targetSize(800, 600)).toEqual({ width: 800, height: 600 });
  });

  test("exactly the long edge is left alone", () => {
    expect(targetSize(1568, 10)).toEqual({ width: 1568, height: 10 });
  });

  test("a degenerate image has no size", () => {
    expect(targetSize(0, 100)).toEqual({ width: 0, height: 0 });
  });

  test("an extreme panorama keeps at least one pixel of height", () => {
    expect(targetSize(100_000, 10).height).toBe(1);
  });
});
