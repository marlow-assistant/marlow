import Testing
import UIKit
@testable import Marlow

/// docs/tasks/ios-image-input.md: 1568px long edge, JPEG q0.8, on-device.
struct ImageDownscalerTests {

    /// scale = 1 on every fixture image here, deliberately: `ImageDownscaler`
    /// works in `cgImage.width`/`.height` (pixels), and a renderer left at
    /// its default (device-native) scale would make a "100x50" fixture's
    /// actual pixel count a multiple of that, decoupling the numbers in the
    /// test from the numbers being asserted on.
    private static func solidImage(width: CGFloat, height: CGFloat, color: UIColor) -> UIImage {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).image { ctx in
            color.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }
    }

    @Test func aDegenerateZeroSizeImageReturnsNilRatherThanCrashing() {
        // `/code-review medium` finding: the UIImage-redraw (paste) overload
        // had no size guard before computing `targetSize`, so a 0x0 image —
        // `UIPasteboard.general.image` can hand back a degenerate one — fed
        // `UIGraphicsImageRenderer(size:)` an invalid size, which traps.
        let zeroSize = Self.solidImage(width: 0, height: 0, color: .red)
        #expect(ImageDownscaler.downscaledJPEG(from: zeroSize) == nil)
    }

    @Test func anOrdinaryImageDownscalesToWithinTheLongEdge() throws {
        let original = Self.solidImage(width: 3000, height: 1500, color: .red)
        let data = try #require(ImageDownscaler.downscaledJPEG(from: original))
        let resized = try #require(UIImage(data: data))
        #expect(max(resized.size.width, resized.size.height) <= ImageDownscaler.longEdge)
    }

    @Test func aRotatedCameraPhotoKeepsItsDisplayedAspect() throws {
        // A portrait camera shot arrives as landscape sensor pixels tagged
        // `.right`: `cgImage` is 3000x1500, the photo as shown is 1500x3000.
        // Sizing from `cgImage` and drawing (which applies the orientation)
        // squashed a portrait photo into a landscape frame.
        let sensor = try #require(Self.solidImage(width: 3000, height: 1500, color: .green).cgImage)
        let portrait = UIImage(cgImage: sensor, scale: 1, orientation: .right)
        let data = try #require(ImageDownscaler.downscaledJPEG(from: portrait))
        let resized = try #require(UIImage(data: data))
        #expect(resized.size.height == ImageDownscaler.longEdge)
        #expect(resized.size.width == ImageDownscaler.longEdge / 2)
    }

    @Test func aSmallImageIsNotUpscaled() throws {
        let original = Self.solidImage(width: 100, height: 50, color: .blue)
        let data = try #require(ImageDownscaler.downscaledJPEG(from: original))
        let resized = try #require(UIImage(data: data))
        #expect(resized.size.width <= 100 && resized.size.height <= 50)
    }
}
