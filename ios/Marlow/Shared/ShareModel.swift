import Combine
import Foundation

/// What the share sheet is doing, and what happens when Send is tapped
/// (docs/tasks/share-and-files.md, Phase 3). Everything the extension decides
/// lives here, in `Shared/`, so the app's tests can drive it; the extension's
/// own code is the view controller and the SwiftUI view around it.
///
/// Send is a direct `POST /v1/message` with a fresh `client_msg_id` and the
/// app's body. Four outcomes, and each is a different thing to the user:
///
/// - **2xx**: close. The message reaches the app through the normal transcript
///   sync, as a row from another device, and the reply arrives with a push.
/// - **Offline, a timeout or a 5xx**: save it to the `ShareOutbox` under the
///   same id and close with "Saved". The app sends it when it next opens. The
///   id is already the one the first attempt carried, so if the box did get it
///   after all, the retry is the server's dedup.
/// - **4xx**: stay open and show the server's reason. A retry can't fix it,
///   so nothing is queued.
/// - **No token**: the app has never been paired. Say so; there is nothing to
///   send with.
@MainActor
final class ShareModel: ObservableObject {
    enum Finish: Equatable {
        case sent
        case savedForLater
    }

    enum Phase: Equatable {
        case needsPairing
        case composing
        case sending
        case finished(Finish)
    }

    static let pairFirstMessage = "Open Marlow and pair first"
    static let savedMessage = "Saved \u{2014} it\u{2019}ll send when you open Marlow."
    static let rePairMessage = "Marlow doesn\u{2019}t recognise this phone. Open Marlow and pair it again."

    /// POSTs the body file and answers with the status and body. A closure so
    /// tests have no network; the live one uploads from the file, which keeps a
    /// 25MB message out of memory (an extension has about 120MB).
    typealias Upload = (URLRequest, URL) async throws -> (data: Data, status: Int)

    @Published var staged = StagedAttachments()
    @Published var caption = ""
    @Published private(set) var phase: Phase
    /// A refusal, or the server's reason, shown under the caption.
    @Published var notice: String?

    private let token: String?
    private let baseURL: URL
    private let outbox: ShareOutbox
    private let upload: Upload

    /// Inactivity timeout, not a deadline: a share is made from inside another
    /// app with the user watching, so a stalled upload should fall to "Saved"
    /// in the time it takes to notice.
    static let requestTimeout: TimeInterval = 30

    init(
        token: String?, baseURL: URL, outbox: ShareOutbox,
        upload: @escaping Upload = ShareModel.liveUpload
    ) {
        self.token = (token?.isEmpty ?? true) ? nil : token
        self.baseURL = baseURL
        self.outbox = outbox
        self.upload = upload
        self.phase = self.token == nil ? .needsPairing : .composing
    }

    var canSend: Bool {
        phase == .composing
            && (!caption.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !staged.isEmpty)
    }

    // MARK: - Staging

    /// A refusal's notice, or nothing. The same count and size rules as the
    /// composer, so the same words.
    func stage(_ outcome: AttachmentIntake.Outcome) {
        let refusal: StagedAttachments.Refusal?
        switch outcome {
        case .photo(let jpeg): refusal = staged.addPhoto(jpeg)
        case .file(let file): refusal = staged.addFile(file)
        case .tooLarge(let name): refusal = .fileTooLarge(name: name)
        case .unreadable:
            notice = "Couldn\u{2019}t read one of those items."
            return
        }
        if let refusal { notice = refusal.notice }
    }

    /// Shared text or a URL pre-fills the caption, one under another.
    func addToCaption(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        caption = caption.isEmpty ? trimmed : caption + "\n" + trimmed
    }

    // MARK: - Send

    func send() async {
        guard canSend, let token else { return }
        phase = .sending
        notice = nil

        let clientMsgId = UUID().uuidString
        let text = caption.trimmingCharacters(in: .whitespacesAndNewlines)
        let bodyFile = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("share-body-\(clientMsgId).json")
        defer { try? FileManager.default.removeItem(at: bodyFile) }

        do {
            try MessageBody.write(
                to: bodyFile, text: text, clientMsgId: clientMsgId, timezone: TimeZone.current.identifier,
                images: staged.photos, files: staged.files
            )
            var request = URLRequest(url: baseURL.appendingPathComponent("v1/message"))
            request.httpMethod = "POST"
            request.timeoutInterval = Self.requestTimeout
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

            let (data, status) = try await upload(request, bodyFile)
            switch status {
            case 200..<300:
                phase = .finished(.sent)
            case 401:
                fail(Self.rePairMessage)
            case 400..<500:
                fail(Self.detail(in: data) ?? "Marlow couldn\u{2019}t take that (error \(status)).")
            case let status where TransientFailure.isTransient(status: status):
                saveForLater(clientMsgId: clientMsgId, text: text)
            default:
                fail("Marlow couldn\u{2019}t take that (error \(status)).")
            }
        } catch {
            if TransientFailure.isTransient(error) {
                saveForLater(clientMsgId: clientMsgId, text: text)
            } else {
                fail(error.localizedDescription)
            }
        }
    }

    /// Saved under the id this attempt used. If saving fails the sheet stays
    /// open with the user's work in it and says so; "Saved" is never a guess.
    private func saveForLater(clientMsgId: String, text: String) {
        let entry = ShareOutbox.Entry(
            clientMsgId: clientMsgId, text: text, createdAt: Date(), photos: staged.photos, files: staged.files
        )
        do {
            try outbox.store(entry)
            phase = .finished(.savedForLater)
        } catch {
            fail("Couldn\u{2019}t reach Marlow, and couldn\u{2019}t save this to send later.")
        }
    }

    private func fail(_ message: String) {
        notice = message
        phase = .composing
    }

    private static func detail(in data: Data) -> String? {
        let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        guard let detail = object?["detail"] as? String, !detail.isEmpty else { return nil }
        return detail
    }

    /// `nonisolated` so it can be the default argument, which is evaluated at
    /// the (not necessarily main-actor) call site.
    nonisolated static func liveUpload(
        _ request: URLRequest, _ bodyFile: URL
    ) async throws -> (data: Data, status: Int) {
        let (data, response) = try await URLSession.shared.upload(for: request, fromFile: bodyFile)
        guard let http = response as? HTTPURLResponse else { return (data, 0) }
        return (data, http.statusCode)
    }
}
