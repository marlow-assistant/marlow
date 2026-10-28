import SwiftUI
import VisionKit

/// Reads the pairing QR (`deploy/pair-device.sh`) in the app. The phone's own
/// camera would open the link in Safari, which is not a supported client, and
/// a universal link can't cover an arbitrary `ts.net` host, so the app scans
/// for itself. The same QR pairs an Android phone through its camera.
///
/// Hands back the raw string and stops at the first QR code seen; the caller
/// parses it (`PairingLink`), so a QR that isn't ours is just ignored here and
/// scanning goes on.
struct PairingScannerView: View {
    let onScan: (String) -> Void
    let onCancel: () -> Void

    var body: some View {
        NavigationStack {
            Group {
                if DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                    ScannerRepresentable(onScan: onScan)
                        .ignoresSafeArea()
                } else {
                    // The Simulator, a device without the camera, or camera
                    // access refused in Settings. Pasting the link still works.
                    ContentUnavailableView(
                        "Camera unavailable",
                        systemImage: "camera.fill",
                        description: Text("Allow camera access in Settings, or paste the link instead.")
                    )
                }
            }
            .navigationTitle("Scan pairing code")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel", action: onCancel)
                }
            }
        }
    }
}

private struct ScannerRepresentable: UIViewControllerRepresentable {
    let onScan: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onScan: onScan) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true,
            isGuidanceEnabled: true,
            isHighlightingEnabled: true
        )
        scanner.delegate = context.coordinator
        return scanner
    }

    func updateUIViewController(_ scanner: DataScannerViewController, context: Context) {
        if !scanner.isScanning { try? scanner.startScanning() }
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let onScan: (String) -> Void
        private var finished = false

        init(onScan: @escaping (String) -> Void) { self.onScan = onScan }

        func dataScanner(
            _ scanner: DataScannerViewController, didAdd addedItems: [RecognizedItem],
            allItems: [RecognizedItem]
        ) {
            guard !finished else { return }
            for case .barcode(let code) in addedItems {
                // Only a pairing link ends the scan; any other QR in frame is
                // skipped, not an error.
                if let payload = code.payloadStringValue, PairingLink.parse(payload) != nil {
                    finished = true
                    scanner.stopScanning()
                    onScan(payload)
                    return
                }
            }
        }
    }
}
