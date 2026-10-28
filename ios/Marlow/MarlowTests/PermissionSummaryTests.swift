import Foundation
import Testing
@testable import Marlow

/// The wire shape of `GET /v1/permissions` items (`server/app.py
/// _serialize_permission_request`), including the `summary` an older server
/// does not send.
struct PermissionSummaryTests {
    private func decode(_ json: String) throws -> PermissionRequestInfo {
        try JSONDecoder().decode(PermissionRequestInfo.self, from: Data(json.utf8))
    }

    private func item(summary: String?) -> String {
        """
        {"request_id": "r1", "tool": "Edit", "tool_input": {"file_path": "USER.md"}, \
        "created_at": 100, "expires_at": 155\(summary.map { ", \"summary\": \($0)" } ?? "")}
        """
    }

    @Test func aFullSummaryDecodes() throws {
        let request = try decode(item(summary: """
        {"title": "Edit USER.md",
         "preview": [{"op": "remove", "text": "- Drinks: tea"}, {"op": "add", "text": "- Likes unicorns"}],
         "marlow_says": "Add a line about unicorns",
         "similar": {"label": "edits to USER.md"}}
        """))

        let summary = try #require(request.summary)
        #expect(summary.title == "Edit USER.md")
        #expect(summary.preview == [
            PermissionPreviewLine(op: .remove, text: "- Drinks: tea"),
            PermissionPreviewLine(op: .add, text: "- Likes unicorns"),
        ])
        #expect(summary.marlowSays == "Add a line about unicorns")
        #expect(summary.similar == PermissionSimilar(label: "edits to USER.md"))
    }

    /// An older server: the request must still decode, so the card can fall
    /// back to the tool name and the raw input.
    @Test func aMissingSummaryDecodesAsNil() throws {
        let request = try decode(item(summary: nil))

        #expect(request.summary == nil)
        #expect(request.tool == "Edit")
        #expect(request.expiresAt == 155)
    }

    @Test func nullMarlowSaysAndSimilarDecodeAsNil() throws {
        let request = try decode(item(summary: """
        {"title": "Run ls", "preview": [], "marlow_says": null, "similar": null}
        """))

        let summary = try #require(request.summary)
        #expect(summary.marlowSays == nil)
        #expect(summary.similar == nil)
        #expect(summary.preview.isEmpty)
    }

    /// A new `op` added server-side later must degrade to plain text, not
    /// fail the decode of the whole poll response (which would hide the
    /// request entirely).
    @Test func anUnknownPreviewOpRendersAsPlainText() throws {
        let request = try decode(item(summary: """
        {"title": "Edit", "preview": [{"op": "move", "text": "a -> b"}, {"op": "text", "text": "ctx"}]}
        """))

        let preview = try #require(request.summary).preview
        #expect(preview.map(\.op) == [.text, .text])
        #expect(preview.map(\.text) == ["a -> b", "ctx"])
    }
}
