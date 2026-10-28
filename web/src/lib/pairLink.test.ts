import { describe, expect, it } from "vitest";
import { inviteFromHash, inviteFromText } from "./pairLink";

const INVITE = "k3J9xQ2mZpL0vR7tYb5nWc8dHs1fGaU4eXoI6jMqKzE";

describe("pairing link", () => {
  it("reads the invite from the fragment", () => {
    expect(inviteFromHash(`#pair=${INVITE}`)).toBe(INVITE);
    expect(inviteFromHash(`pair=${INVITE}`)).toBe(INVITE);
    expect(inviteFromHash(`#x=1&pair=${INVITE}`)).toBe(INVITE);
  });

  it("ignores anything that is not an invite", () => {
    expect(inviteFromHash("")).toBeNull();
    expect(inviteFromHash("#pair=")).toBeNull();
    expect(inviteFromHash("#pair=short")).toBeNull();
    expect(inviteFromHash("#pair=<script>alert(1)</script>")).toBeNull();
    expect(inviteFromHash(`#other=${INVITE}`)).toBeNull();
  });

  it("takes a pasted link or a bare invite", () => {
    expect(inviteFromText(`  https://box.example.ts.net:8443/#pair=${INVITE}\n`)).toBe(INVITE);
    expect(inviteFromText(INVITE)).toBe(INVITE);
    expect(inviteFromText("https://box.example.ts.net:8443/")).toBeNull();
    expect(inviteFromText("123456")).toBeNull();
  });

  it("does not take an invite from a query string", () => {
    expect(inviteFromText(`https://box.example.ts.net:8443/?pair=${INVITE}`)).toBeNull();
  });
});
