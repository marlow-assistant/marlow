import SwiftUI

/// Which portrait Marlow wears: the header avatar and, where iOS allows, the
/// home-screen icon. A per-device presentation choice, so it lives here and
/// never reaches the server (web mirrors this table in `lib/faces.ts`).
///
/// Face 1 is the original icon and the default. The set deliberately spans
/// male, female and androgynous faces, and monocle against glasses: the prop
/// is part of the choice, which is why the picker shows them large.
struct MarlowFace: Identifiable, Equatable {
    let id: Int
    let prop: Prop
    /// The crop for the 52pt header avatar (and smaller).
    let header: FaceCrop
    /// The crop for big circles: the picker's tiles, and anything Marlow
    /// shows large. Looser than `header`, since a bigger circle has room for
    /// more than the face (Mike, 2026-10-16).
    let large: FaceCrop

    enum Prop { case monocle, glasses }

    func crop(_ size: FaceCrop.Size) -> FaceCrop {
        switch size {
        case .header: header
        case .large: large
        }
    }

    var imageName: String { "MarlowFace\(id)" }

    /// `nil` is the primary icon, which is what `setAlternateIconName` takes
    /// to put it back. The others must match project.yml's
    /// ASSETCATALOG_COMPILER_ALTERNATE_APP_ICON_NAMES.
    var alternateIconName: String? { id == 1 ? nil : "AppIcon\(id)" }

    var accessibilityName: String {
        "Face \(id), \(prop == .monocle ? "monocle" : "glasses")"
    }

    static let all: [MarlowFace] = [
        MarlowFace(id: 1, prop: .monocle,
                   header: FaceCrop(zoom: 1.49, x: 0.43, y: 0.00),
                   large: FaceCrop(zoom: 1.00, x: 0.47, y: 0.36)),
        MarlowFace(id: 2, prop: .monocle,
                   header: FaceCrop(zoom: 1.12, x: 0.89, y: 0.00),
                   large: FaceCrop(zoom: 1.00, x: 0.90, y: 0.78)),
        MarlowFace(id: 3, prop: .monocle,
                   header: FaceCrop(zoom: 1.50, x: 0.39, y: 0.00),
                   large: FaceCrop(zoom: 1.00, x: 0.68, y: 0.30)),
        MarlowFace(id: 4, prop: .glasses,
                   header: FaceCrop(zoom: 1.39, x: 0.48, y: 0.00),
                   large: FaceCrop(zoom: 1.00, x: 0.50, y: 0.55)),
        MarlowFace(id: 5, prop: .glasses,
                   header: FaceCrop(zoom: 1.29, x: 0.56, y: 0.00),
                   large: FaceCrop(zoom: 1.00, x: 0.52, y: 0.64)),
        MarlowFace(id: 6, prop: .monocle,
                   header: FaceCrop(zoom: 1.32, x: 0.56, y: 0.12),
                   large: FaceCrop(zoom: 1.00, x: 0.80, y: 0.62)),
        MarlowFace(id: 7, prop: .monocle,
                   header: FaceCrop(zoom: 1.20, x: 0.65, y: 0.05),
                   large: FaceCrop(zoom: 1.00, x: 0.84, y: 0.55)),
        MarlowFace(id: 8, prop: .monocle,
                   header: FaceCrop(zoom: 1.35, x: 0.57, y: 0.03),
                   large: FaceCrop(zoom: 1.00, x: 0.80, y: 0.55)),
        MarlowFace(id: 9, prop: .monocle,
                   header: FaceCrop(zoom: 1.45, x: 0.61, y: 0.00),
                   large: FaceCrop(zoom: 1.00, x: 0.74, y: 0.43)),
        MarlowFace(id: 10, prop: .monocle,
                   header: FaceCrop(zoom: 1.38, x: 0.50, y: 0.00),
                   large: FaceCrop(zoom: 1.00, x: 0.41, y: 0.55)),
        MarlowFace(id: 11, prop: .monocle,
                   header: FaceCrop(zoom: 1.39, x: 0.52, y: 0.22),
                   large: FaceCrop(zoom: 1.00, x: 0.62, y: 0.68)),
    ]

    static let `default` = all[0]

    /// Unknown ids (a future build's face, a hand-edited default) fall back to
    /// the default rather than an avatar with no image.
    static func with(id: Int) -> MarlowFace {
        all.first { $0.id == id } ?? .default
    }
}

/// How one avatar size frames a portrait: an aspect-fill square scaled by
/// `zoom` about `anchor`, then clipped round. Zoom 1 is the whole portrait.
/// Per face because the newer portraits frame the head about a fifth larger
/// than face 1, so one zoom read as too tight on them; and per size because a
/// crop that keeps a 52pt face legible is too tight at 140pt (2026-10-16).
/// The anchor is where the face sits: one shared anchor cut most of the new
/// faces at the chin. Web mirrors these values in `lib/faces.ts`; the
/// home-screen icons are cut by `scripts/face-icons.sh` instead.
struct FaceCrop: Equatable {
    let zoom: CGFloat
    let anchor: UnitPoint

    init(zoom: CGFloat, x: CGFloat, y: CGFloat) {
        self.zoom = zoom
        anchor = UnitPoint(x: x, y: y)
    }

    enum Size { case header, large }
}

/// Where the choice lives: `UserDefaults`, read by `MarlowAvatar` through
/// `@AppStorage` so the header follows a change made in Settings at once.
enum FacePreference {
    static let storageKey = "marlowFace"

    static func current(in defaults: UserDefaults = .standard) -> MarlowFace {
        MarlowFace.with(id: defaults.integer(forKey: storageKey))
    }

    static func set(_ face: MarlowFace, in defaults: UserDefaults = .standard) {
        defaults.set(face.id, forKey: storageKey)
    }
}

/// The seam over `UIApplication`'s icon API, so the picker's choice of
/// whether to call it is testable without the app.
@MainActor
protocol AppIconSetting {
    var supportsAlternateIcons: Bool { get }
    var alternateIconName: String? { get }
    func setAlternateIconName(_ name: String?) async throws
}

extension UIApplication: AppIconSetting {}

enum AppIconSync {
    /// Brings the home-screen icon in line with `face`. iOS shows its own
    /// "You have changed the icon" alert on every call, so skip the call when
    /// the icon already matches: re-picking the current face says nothing.
    /// A failure leaves the old icon, which is harmless, so it is swallowed.
    @MainActor
    static func apply(_ face: MarlowFace, to app: AppIconSetting) async {
        guard app.supportsAlternateIcons, app.alternateIconName != face.alternateIconName else { return }
        try? await app.setAlternateIconName(face.alternateIconName)
    }

    /// The face picker is on screen. The icon waits for Done while it is, so
    /// `activated` leaves it alone (Mike, 2026-10-16).
    @MainActor static var choosing = false

    /// Run on every activation, so the swallowed failures above heal on their
    /// own. Not while the picker is open: pulling down Notification Center
    /// mid-choice would otherwise change the icon before Done.
    @MainActor
    static func activated(
        stored: MarlowFace = FacePreference.current(),
        app: AppIconSetting = UIApplication.shared
    ) async {
        guard !choosing else { return }
        await reconcile(stored: stored, app: app)
    }

    /// Brings the icon to the stored face: Done on Settings or the Marlow
    /// sheet, and each activation. The likeliest swallowed failure: a second
    /// change while iOS's alert for the first is still up is refused, which
    /// left the icon on the earlier face. That alert deactivates the scene, so
    /// dismissing it lands back in `activated`.
    @MainActor
    static func reconcile(
        stored: MarlowFace = FacePreference.current(),
        app: AppIconSetting = UIApplication.shared
    ) async {
        #if DEBUG
        // A fixture launch can carry `-marlowFace N` for screenshots, and
        // the icon is the install's real one (UIFixture's "only the chat's
        // store is fake").
        if UIFixture.state != nil { return }
        #endif
        await apply(stored, to: app)
    }
}
