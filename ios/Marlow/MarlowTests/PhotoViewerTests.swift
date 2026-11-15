import Foundation
import Testing
@testable import Marlow

/// Tapping a photo in an old message opens it full screen (Mike, 2026-11-14):
/// the bubble's slots, and the on-disk copy QuickLook is handed for each.
@MainActor
struct PhotoViewerTests {

    private func userRow(_ text: String) -> ChatMessage {
        ChatMessage(
            id: UUID(), role: .user, text: text, createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil
        )
    }

    // MARK: - Slots

    @Test func aServerRowsSlotsFollowItsAttachmentIds() {
        var message = userRow("two")
        message.attachmentIds = ["a1", "a2"]
        message.imageFilenames = ["mine-0.jpg"]
        #expect(message.photoSlots == ["a1.jpg", "a2.jpg"])
    }

    @Test func thisPhonesOwnSendUsesItsSavedFilenames() {
        var message = userRow("mine")
        message.imageFilenames = ["c-0.jpg", "c-1.jpg"]
        #expect(message.photoSlots == ["c-0.jpg", "c-1.jpg"])
    }

    // MARK: - The file QuickLook opens

    private func makeStore(attachments: URL) -> ChatStore {
        ChatStore(
            deviceToken: "tok", client: FakeChatAPI(), persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: attachments, policy: .test()
        )
    }

    @Test func aPhotoOnDiskGivesItsURL() throws {
        let dir = makeTempAttachmentsDirectoryURL()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data([0xFF, 0xD8, 0xFF]).write(to: dir.appendingPathComponent("a1.jpg"))
        let store = makeStore(attachments: dir)
        #expect(store.photoURL(filename: "a1.jpg") == dir.appendingPathComponent("a1.jpg"))
    }

    @Test func aPhotoStillDownloadingGivesNoURL() {
        let store = makeStore(attachments: makeTempAttachmentsDirectoryURL())
        #expect(store.photoURL(filename: "a2.jpg") == nil)
    }

    @Test func aFilenameThatLeavesTheDirectoryGivesNoURL() throws {
        let dir = makeTempAttachmentsDirectoryURL()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let store = makeStore(attachments: dir)
        #expect(store.photoURL(filename: "../x.jpg") == nil)
        #expect(store.photoURL(filename: "") == nil)
    }
}
