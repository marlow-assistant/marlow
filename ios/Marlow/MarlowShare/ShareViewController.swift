import SwiftUI
import UIKit

/// "Marlow" in the share sheet (docs/tasks/share-and-files.md, Phase 3): share
/// a PDF from Mail, a page from Safari, a spreadsheet from Files or photos from
/// Photos, add a note, and send. Everything it decides is in `ShareModel`
/// (`Shared/`, where the app's tests reach it); this is the container around it.
///
/// A separate process with its own memory limit, no access to the app's
/// Keychain item, and no way to run a long job: it reads the token from the
/// shared Keychain copy (`SharedCredential`) and the server from the App
/// Group (`PairedHost`), and sends once.
final class ShareViewController: UIViewController {
    private var model: ShareModel?

    override func viewDidLoad() {
        super.viewDidLoad()

        // Absent until the app has run once since pairing, and after sign-out:
        // `ShareModel` then shows "Open Marlow and pair first" and sends nothing.
        let model = ShareModel(
            token: SharedCredential.load(), baseURL: PairedHost.url(), outbox: .shared
        )
        self.model = model

        let host = UIHostingController(rootView: ShareView(
            model: model,
            close: { [weak self] in
                self?.extensionContext?.completeRequest(returningItems: nil)
            },
            cancel: { [weak self] in
                self?.extensionContext?.cancelRequest(withError: CocoaError(.userCancelled))
            }
        ))
        addChild(host)
        host.view.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(host.view)
        NSLayoutConstraint.activate([
            host.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            host.view.topAnchor.constraint(equalTo: view.topAnchor),
            host.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        host.didMove(toParent: self)

        // No token, nothing to stage: don't read the user's items for a sheet
        // that cannot send them.
        guard model.phase != .needsPairing else { return }
        let providers = (extensionContext?.inputItems as? [NSExtensionItem] ?? [])
            .flatMap { $0.attachments ?? [] }
        Task { @MainActor in
            await ShareItemLoader.load(providers, into: model)
        }
    }
}
