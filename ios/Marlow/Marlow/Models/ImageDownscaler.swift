import ImageIO
import UIKit
import UniformTypeIdentifiers

/// Downscales an attached image before it ever reaches the network
/// (docs/tasks/ios-image-input.md, decided 2026-08-11: on the device, not the
/// server — 12MP HEIC over a van-dead-zone tailnet buys the model nothing).
/// Long edge 1568px: above this the model's own tiling stops gaining detail.
///
/// **Quality dropped 0.8 -> 0.6 on 2026-08-27**, after Mike measured a real
/// attachment at 431KB — over the ~200-400KB this comment used to claim, and
/// over the ~300KB `docs/tasks/ios-image-input.md` assumed when it judged
/// re-uploading on every send retry acceptable.
///
/// 0.6 is where the ImageIO curve stops paying: measured across smooth and
/// detail-heavy content it returns roughly 60-75% of q0.8's bytes, and each
/// further step costs more visible quality for less. It does NOT reach the
/// ~200KB Mike asked for on a 431KB source — that needs ~q0.4, which damages
/// text, and these attachments are often screenshots. Fewer pixels, not more
/// compression, is the lever if 200KB ever becomes a hard requirement.
enum ImageDownscaler {
    static let longEdge: CGFloat = 1568
    static let jpegQuality: CGFloat = 0.6

    /// The photo-roll path: raw file bytes in, downscaled JPEG out. ImageIO's
    /// thumbnail generator, not `UIImage` + redraw — this avoids fully
    /// decoding a 12MP original into memory just to shrink it.
    static func downscaledJPEG(from data: Data) -> Data? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceThumbnailMaxPixelSize: longEdge,
            kCGImageSourceCreateThumbnailWithTransform: true,
        ]
        guard let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
            return nil
        }
        return jpegData(from: thumbnail)
    }

    /// The paste path: `UIPasteboard.general.image` hands back an already-
    /// decoded `UIImage` — there is no raw file to hand ImageIO instead, so
    /// this resizes the already-decoded bitmap. The "avoid a full decode"
    /// win the photo-roll path gets from ImageIO doesn't apply here; the
    /// pasteboard did that decode before this code ever runs.
    ///
    /// The camera path lands here too. Its photos are sensor-orientation
    /// pixels tagged `.right`, so the size comes from `image.size` (oriented)
    /// rather than `cgImage` (raw): sizing from the raw pixels while `draw`
    /// applies the orientation squashed every portrait shot into landscape.
    static func downscaledJPEG(from image: UIImage) -> Data? {
        guard image.cgImage != nil else { return nil }
        let pixelSize = CGSize(width: image.size.width * image.scale, height: image.size.height * image.scale)
        guard pixelSize.width > 0, pixelSize.height > 0 else { return nil }
        let scale = min(1, longEdge / max(pixelSize.width, pixelSize.height))
        let targetSize = CGSize(width: pixelSize.width * scale, height: pixelSize.height * scale)
        // targetSize is computed from the image's pixel size, i.e. already in
        // PIXEL terms. UIGraphicsImageRenderer's default format.scale is the
        // current trait environment's device scale (2x/3x), which would
        // multiply that pixel count by the device scale a second time —
        // rendering an image up to 3x larger than `longEdge` actually asks
        // for. format.scale = 1 makes targetSize's units and the renderer's
        // output pixels agree.
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: targetSize, format: format)
        let resized = renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: targetSize))
        }
        return resized.jpegData(compressionQuality: jpegQuality)
    }

    private static func jpegData(from cgImage: CGImage) -> Data? {
        let mutableData = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            mutableData, UTType.jpeg.identifier as CFString, 1, nil
        ) else { return nil }
        let options: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: jpegQuality]
        CGImageDestinationAddImage(destination, cgImage, options as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return mutableData as Data
    }
}
