import { describe, expect, it } from "vitest";
import { capitalizedModelName, MODEL_CHOICES, modelChoice } from "./modelName";

describe("modelChoice", () => {
  it("reads the two pickable models however /health spells them", () => {
    expect(modelChoice("sonnet")).toBe("sonnet");
    expect(modelChoice("claude-opus-5-5")).toBe("opus");
    expect(modelChoice("Opus")).toBe("opus");
  });
  it("is null for anything else, so no option reads as chosen", () => {
    expect(modelChoice("haiku")).toBeNull();
    expect(modelChoice(null)).toBeNull();
    expect(modelChoice("gpt-4-turbo")).toBeNull();
  });
  it("offers Sonnet then Opus, as the box's MODELS", () => {
    expect(MODEL_CHOICES.map((m) => m.value)).toEqual(["sonnet", "opus"]);
  });
});

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
