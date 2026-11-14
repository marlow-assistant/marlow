import Foundation
import Testing
@testable import Marlow

/// What the share sheet does when Send is tapped
/// (docs/tasks/share-and-files.md, Phase 3). The transport is a closure so
/// the four outcomes are driven without a network: a 2xx closes, a transient
/// failure is saved for the app, a 4xx stays open with the server's reason,
/// and no token means "pair first".
@MainActor
struct ShareModelTests {

    /// What the fake transport saw. The body file is read inside the upload,
    /// because the model deletes it afterwards.
    final class Wire: @unchecked Sendable {
        private let lock = NSLock()
        private var requests: [URLRequest] = []
        private var bodies: [[String: Any]] = []
        var calls: Int { lock.withLock { requests.count } }
        var lastRequest: URLRequest? { lock.withLock { requests.last } }
        var lastBody: [String: Any]? { lock.withLock { bodies.last } }
        func record(_ request: URLRequest, body: [String: Any]) {
            lock.withLock { requests.append(request); bodies.append(body) }
        }
    }

    private func makeOutbox() -> ShareOutbox {
        ShareOutbox(directory: URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("ShareModelTests-\(UUID().uuidString)", isDirectory: true))
    }

    private func makeModel(
        token: String? = "tok", outbox: ShareOutbox? = nil, wire: Wire = Wire(),
        reply: @escaping @Sendable () throws -> (Data, Int)
    ) -> ShareModel {
        ShareModel(
            token: token, baseURL: URL(string: "https://marlow.example.ts.net:8443")!,
            outbox: outbox ?? makeOutbox(),
            upload: { request, bodyFile in
                let body = (try? JSONSerialization.jsonObject(with: Data(contentsOf: bodyFile)) as? [String: Any]) ?? [:]
                wire.record(request, body: body)
                let (data, status) = try reply()
                return (data, status)
            }
        )
    }

    private func stage(_ model: ShareModel) {
        model.staged.addPhoto(Data([0xFF, 0xD8, 0xFF, 0xD9]))
        model.staged.addFile(StagedFile(name: "Booking.pdf", mime: "application/pdf", data: Data("%PDF-1.7".utf8)))
    }

    // MARK: - No token

    /// The app has never been paired, or has been signed out: the shared
    /// Keychain item is absent. There is nothing to send with and nowhere to
    /// queue to, so the sheet says so and offers Close.
    @Test func withoutATokenTheSheetAsksToPairFirstAndSendsNothing() async {
        let wire = Wire()
        let model = makeModel(token: nil, wire: wire) { (Data(), 202) }
        stage(model)
        model.caption = "hello"

        #expect(model.phase == .needsPairing)
        #expect(ShareModel.pairFirstMessage == "Open Marlow and pair first")
        await model.send()

        #expect(wire.calls == 0)
        #expect(model.phase == .needsPairing)
        #expect(!model.canSend)
    }

    @Test func anEmptyTokenIsNoToken() {
        let model = makeModel(token: "") { (Data(), 202) }
        #expect(model.phase == .needsPairing)
    }

    // MARK: - 2xx

    @Test func aSuccessfulSendPostsTheAppsBodyAndFinishes() async throws {
        let wire = Wire()
        let outbox = makeOutbox()
        let model = makeModel(outbox: outbox, wire: wire) { (Data(#"{"turn_id": "t1"}"#.utf8), 202) }
        stage(model)
        model.caption = "  this one is time sensitive  "
        await model.send()

        #expect(model.phase == .finished(.sent))
        #expect(outbox.pendingIds().isEmpty, "nothing queued when the box took it")

        let request = try #require(wire.lastRequest)
        #expect(request.url?.absoluteString == "https://marlow.example.ts.net:8443/v1/message")
        #expect(request.httpMethod == "POST")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")

        let body = try #require(wire.lastBody)
        #expect(body["text"] as? String == "this one is time sensitive")
        let clientMsgId = try #require(body["client_msg_id"] as? String)
        #expect(UUID(uuidString: clientMsgId) != nil, "a fresh id per share: the server dedups on it")
        #expect(body["timezone"] as? String == TimeZone.current.identifier)
        #expect((body["images"] as? [[String: String]])?.count == 1)
        #expect((body["files"] as? [[String: String]]) == [[
            "b64": Data("%PDF-1.7".utf8).base64EncodedString(), "mime": "application/pdf", "name": "Booking.pdf",
        ]])
    }

    @Test func eachSendGetsItsOwnClientMsgId() async throws {
        let wire = Wire()
        let first = makeModel(wire: wire) { (Data(), 202) }
        let second = makeModel(wire: wire) { (Data(), 202) }
        first.caption = "a"
        second.caption = "b"
        await first.send()
        await second.send()
        #expect(wire.calls == 2)
    }

    @Test func nothingStagedAndNoCaptionIsNotSendable() async {
        let wire = Wire()
        let model = makeModel(wire: wire) { (Data(), 202) }
        #expect(!model.canSend)
        await model.send()
        #expect(wire.calls == 0)

        model.caption = "   "
        #expect(!model.canSend)
        stage(model)
        #expect(model.canSend, "an attachment alone is a message, as in the composer")
    }

    // MARK: - Transient failure

    @Test(arguments: [500, 502, 503])
    func aServerErrorIsSavedForTheApp(status: Int) async throws {
        let wire = Wire()
        let outbox = makeOutbox()
        let model = makeModel(outbox: outbox, wire: wire) { (Data(), status) }
        stage(model)
        model.caption = "later please"
        await model.send()

        #expect(model.phase == .finished(.savedForLater))
        let sentId = try #require(wire.lastBody?["client_msg_id"] as? String)
        #expect(outbox.pendingIds() == [sentId], "saved under the id already sent: if the box did get it, the retry dedups")
        let saved = try #require(outbox.load(id: sentId))
        #expect(saved.text == "later please")
        #expect(saved.photos == [Data([0xFF, 0xD8, 0xFF, 0xD9])])
        #expect(saved.files.map(\.name) == ["Booking.pdf"])
    }

    @Test(arguments: [URLError.notConnectedToInternet, .timedOut, .networkConnectionLost, .cannotConnectToHost])
    func anOfflinePhoneSavesTheMessageForTheApp(code: URLError.Code) async throws {
        let outbox = makeOutbox()
        let model = makeModel(outbox: outbox) { throw URLError(code) }
        stage(model)
        await model.send()

        #expect(model.phase == .finished(.savedForLater))
        #expect(outbox.pendingIds().count == 1)
        #expect(ShareModel.savedMessage == "Saved \u{2014} it\u{2019}ll send when you open Marlow.")
    }

    /// Saving is the last resort; if the container is not there, the sheet
    /// must not claim a save that did not happen.
    @Test func ifItCannotBeSavedTheSheetStaysOpenAndSaysSo() async {
        let model = makeModel(outbox: ShareOutbox(directory: nil)) { throw URLError(.notConnectedToInternet) }
        stage(model)
        await model.send()

        #expect(model.phase == .composing)
        #expect(model.notice != nil)
        #expect(!model.staged.isEmpty, "what the user chose is still there")
    }

    // MARK: - 4xx

    /// A retry cannot fix it, so it is not queued; the reason is the server's.
    @Test func aRejectionShowsTheServersDetailKeepsTheSheetOpenAndQueuesNothing() async throws {
        let outbox = makeOutbox()
        let model = makeModel(outbox: outbox) {
            (Data(#"{"detail": "files[0] could not be read"}"#.utf8), 422)
        }
        stage(model)
        model.caption = "keep this"
        await model.send()

        #expect(model.phase == .composing)
        #expect(model.notice == "files[0] could not be read")
        #expect(outbox.pendingIds().isEmpty)
        #expect(model.caption == "keep this")
        #expect(model.staged.count == 2)
    }

    @Test(arguments: [400, 413, 422])
    func aRejectionWithNoDetailStillSaysSomething(status: Int) async {
        let model = makeModel { (Data(), status) }
        model.caption = "x"
        await model.send()
        #expect(model.phase == .composing)
        #expect(model.notice?.isEmpty == false)
    }

    @Test func aRevokedTokenSaysToPairAgain() async {
        let model = makeModel { (Data(#"{"detail": "Unauthorized"}"#.utf8), 401) }
        model.caption = "x"
        await model.send()

        #expect(model.phase == .composing)
        #expect(model.notice == ShareModel.rePairMessage)
    }

    /// After a refusal the user can fix the message and send again.
    @Test func theUserCanSendAgainAfterARejection() async {
        let attempts = Attempts()
        let model = makeModel {
            attempts.append("x")
            return attempts.values.count == 1 ? (Data(#"{"detail": "no"}"#.utf8), 400) : (Data(), 202)
        }
        model.caption = "x"
        await model.send()
        await model.send()
        #expect(model.phase == .finished(.sent))
    }
}
