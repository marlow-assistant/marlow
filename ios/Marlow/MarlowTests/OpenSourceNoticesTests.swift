import XCTest
@testable import Marlow

/// About > Open-source notices (docs/tasks/settings-overhaul.md). Apache-2.0 §4
/// wants the licence text in a binary, so a notice whose file didn't make it
/// into the bundle is the failure that matters.
final class OpenSourceNoticesTests: XCTestCase {
    func testEveryNoticeHasItsTextInTheApp() throws {
        let notices = OpenSourceNotice.all
        XCTAssertFalse(notices.isEmpty)
        for notice in notices {
            XCTAssertGreaterThan(notice.text.count, 500, "\(notice.name) has no licence text")
        }
    }

    /// The next dependency added without a notice fails here. The simulator
    /// can read the source tree, so this is the real lockfile.
    func testEveryResolvedPackageHasANotice() throws {
        let resolved = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Marlow.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved")
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: resolved)) as? [String: Any]
        let pins = try XCTUnwrap(json?["pins"] as? [[String: Any]])
        XCTAssertFalse(pins.isEmpty)
        let covered = Set(OpenSourceNotice.all.compactMap(\.package))
        for pin in pins {
            let identity = try XCTUnwrap(pin["identity"] as? String)
            XCTAssertTrue(covered.contains(identity), "\(identity) has no notice")
        }
    }

    /// Settings' Avatar row names the prop: the faces have no names of their own.
    func testAFaceIsDescribedByItsProp() {
        XCTAssertEqual(MarlowFace.all.first { $0.prop == .monocle }?.propName, "Monocle")
        XCTAssertEqual(MarlowFace.all.first { $0.prop == .glasses }?.propName, "Glasses")
    }
}
