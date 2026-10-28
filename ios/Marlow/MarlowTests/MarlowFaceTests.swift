import Foundation
import Testing
import UIKit
@testable import Marlow

/// The face picker: a per-device choice of portrait for the avatar and,
/// through alternate icons, the home-screen icon.
@MainActor
struct MarlowFaceTests {

    /// A private suite, not `.standard`, so a test run never changes the
    /// face of the simulator's installed app.
    private func makeDefaults() -> (UserDefaults, String) {
        let name = "MarlowFaceTests-\(UUID().uuidString)"
        return (UserDefaults(suiteName: name)!, name)
    }

    /// Face 1 is the original icon. An untouched install must look exactly
    /// as it did before faces existed.
    @Test func defaultIsTheOriginalAndUsesThePrimaryIcon() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        #expect(FacePreference.current(in: defaults).id == 1)
        #expect(MarlowFace.default.alternateIconName == nil)
    }

    @Test func choiceRoundTrips() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        FacePreference.set(MarlowFace.with(id: 7), in: defaults)
        #expect(FacePreference.current(in: defaults).id == 7)
    }

    /// A face id this build doesn't know falls back rather than showing an
    /// avatar with no image behind it.
    @Test func unknownIdFallsBackToTheDefault() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set(42, forKey: FacePreference.storageKey)
        #expect(FacePreference.current(in: defaults) == .default)
    }

    /// Hosted in the app, so `UIImage(named:)` reads the shipped catalog: a
    /// face whose imageset is missing or misnamed renders as nothing, with no
    /// error anywhere.
    @Test func everyFaceHasAnImage() {
        for face in MarlowFace.all {
            #expect(UIImage(named: face.imageName) != nil, "missing \(face.imageName)")
        }
    }

    /// The alternate icons iOS knows about come from project.yml's
    /// ASSETCATALOG_COMPILER_ALTERNATE_APP_ICON_NAMES, a separate list from
    /// `MarlowFace.all`. If they drift, picking that face silently leaves the
    /// icon alone.
    @Test func alternateIconsInTheBuiltPlistMatchTheFaces() {
        let icons = Bundle.main.infoDictionary?["CFBundleIcons"] as? [String: Any]
        let alternates = icons?["CFBundleAlternateIcons"] as? [String: Any] ?? [:]
        let expected = Set(MarlowFace.all.compactMap(\.alternateIconName))
        #expect(Set(alternates.keys) == expected)
    }

    @Test func iconSyncSetsTheFacesIcon() async {
        let app = FakeIconApp()
        await AppIconSync.apply(MarlowFace.with(id: 4), to: app)
        #expect(app.calls == ["AppIcon4"])
        await AppIconSync.apply(.default, to: app)
        #expect(app.calls == ["AppIcon4", nil])
    }

    /// iOS raises an alert on every call, so re-choosing the current face
    /// must not call at all.
    @Test func iconSyncSkipsWhenTheIconAlreadyMatches() async {
        let app = FakeIconApp(current: "AppIcon4")
        await AppIconSync.apply(MarlowFace.with(id: 4), to: app)
        let primary = FakeIconApp(current: nil)
        await AppIconSync.apply(.default, to: primary)
        #expect(app.calls.isEmpty)
        #expect(primary.calls.isEmpty)
    }

    /// The drift case: face 7 is stored, but the icon change for it was
    /// refused and the icon is still face 4. Activation puts it right.
    @Test func reconcileBringsADriftedIconBackToTheStoredFace() async {
        let app = FakeIconApp(current: "AppIcon4")
        await AppIconSync.reconcile(stored: MarlowFace.with(id: 7), app: app)
        #expect(app.calls == ["AppIcon7"])
    }

    // Pulling down Notification Center mid-choice deactivates the scene; on
    // return the icon must still wait for Done.
    @Test func activationLeavesTheIconAloneWhileThePickerIsOpen() async {
        let app = FakeIconApp(current: "AppIcon4")
        AppIconSync.choosing = true
        defer { AppIconSync.choosing = false }
        await AppIconSync.activated(stored: MarlowFace.with(id: 7), app: app)
        #expect(app.calls.isEmpty)
        AppIconSync.choosing = false
        await AppIconSync.activated(stored: MarlowFace.with(id: 7), app: app)
        #expect(app.calls == ["AppIcon7"])
    }

    @Test func iconSyncDoesNothingWithoutAlternateIconSupport() async {
        let app = FakeIconApp(supports: false)
        await AppIconSync.apply(MarlowFace.with(id: 4), to: app)
        #expect(app.calls.isEmpty)
    }
}

@MainActor
private final class FakeIconApp: AppIconSetting {
    let supportsAlternateIcons: Bool
    private(set) var alternateIconName: String?
    private(set) var calls: [String?] = []

    init(supports: Bool = true, current: String? = nil) {
        supportsAlternateIcons = supports
        alternateIconName = current
    }

    func setAlternateIconName(_ name: String?) async throws {
        calls.append(name)
        alternateIconName = name
    }
}
