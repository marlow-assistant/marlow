import SwiftUI
import UIKit

/// The system camera, for docs/tasks/ios-image-input.md step 2. SwiftUI has no
/// camera view, so this wraps `UIImagePickerController` — the capture UI,
/// retake/use-photo review, and permission prompt all come with it. Hands back
/// the captured `UIImage`, which then takes the same downscale path as paste.
///
/// Only offered where `isAvailable` — the simulator has no camera, and
/// presenting `.camera` without one throws.
///
/// The presenter dismisses it via `onFinish` rather than this view reading
/// `@Environment(\.dismiss)`: the coordinator keeps the copy of this struct it
/// was made with, and an environment value read off a stale copy outside
/// `body` is not one SwiftUI promises to keep live.
struct CameraPicker: UIViewControllerRepresentable {
    let onCapture: (UIImage) -> Void
    let onFinish: () -> Void

    static var isAvailable: Bool { UIImagePickerController.isSourceTypeAvailable(.camera) }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.cameraCaptureMode = .photo
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        private let parent: CameraPicker
        init(_ parent: CameraPicker) { self.parent = parent }

        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
        ) {
            if let image = info[.originalImage] as? UIImage {
                parent.onCapture(image)
            }
            parent.onFinish()
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            parent.onFinish()
        }
    }
}
