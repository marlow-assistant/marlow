import { describe, expect, it } from "vitest";
import { capitalizedModelName } from "./modelName";

describe("capitalizedModelName (iOS parity)", () => {
  it("takes the tier after a leading claude", () => {
    expect(capitalizedModelName("claude-sonnet-5")).toBe("Sonnet");
    expect(capitalizedModelName("claude-opus-4-1")).toBe("Opus");
  });
  it("capitalizes a bare alias", () => {
    expect(capitalizedModelName("sonnet")).toBe("Sonnet");
    expect(capitalizedModelName("OPUS")).toBe("Opus");
  });
  it("leaves anything unfamiliar untouched", () => {
    expect(capitalizedModelName("gpt-4-turbo")).toBe("gpt-4-turbo");
    expect(capitalizedModelName("")).toBe("");
  });
});
