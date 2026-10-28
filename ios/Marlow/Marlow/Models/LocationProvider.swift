import CoreLocation
import Foundation

/// Keeps the latest location fix and hands the send path a `location` to attach
/// (docs/tasks/message-location.md). One instance, owned by `MarlowApp`.
///
/// Location is gathered only while the setting is not Off AND the app is in the
/// foreground: when-in-use authorization, no background mode. A message is sent
/// from the foreground, so a fix from the last few minutes is all it needs.
///
/// The whole point of `currentLocation()` returning nil is that the server
/// reads absence as "no location from this device right now" and clears what it
/// holds. Off, a denied permission and a stale fix therefore all collapse into
/// the same wire shape on purpose — never send a stale position instead.
@MainActor
final class LocationProvider: NSObject, ObservableObject, CLLocationManagerDelegate {
    /// Older than this and the fix is treated as nothing. Matches the server
    /// contract's "a fix it got within the last 15 minutes".
    nonisolated static let maxFixAge: TimeInterval = 15 * 60
    /// A new fix this far from the last geocoded point earns a new geocode.
    /// Also how far a place may drift from the fix it describes before it is
    /// dropped.
    nonisolated static let regeocodeDistance: CLLocationDistance = 500
    nonisolated static let regeocodeAge: TimeInterval = 10 * 60
    /// With no place yet, a failed geocode waits this long before the next
    /// try. Retrying on every fix (every 50 m on Precise) while offline gets
    /// the app throttled by Apple, which then outlasts the outage.
    nonisolated static let geocodeRetryDelay: TimeInterval = 60
    /// Approximate on the wire, whatever Core Location handed over: 2 places
    /// (~1 km) and an accuracy no better than this. Matches web.
    nonisolated static let approximateFloor: CLLocationAccuracy = 1000

    /// For the Settings footer: "turned off for Marlow in iOS Settings".
    @Published private(set) var authorizationStatus: CLAuthorizationStatus

    private let manager: CLLocationManager
    private let defaults: UserDefaults
    private var geocoder = CLGeocoder()

    private var appIsActive = false
    private var updating = false
    /// The mode the held fix was gathered under. A change clears the fix and
    /// place: a precise fix must not outlive a switch to Approximate (that
    /// would send precision the user just took back), and an approximate one
    /// is the wrong size for Precise.
    private var appliedSharing: LocationSharing

    private var latestFix: CLLocation?
    private var place: String?
    /// The fix `place` was geocoded from, and when the last attempt ran.
    private var placeOrigin: CLLocation?
    private var lastGeocodeAttempt: Date?
    private var geocodeInFlight = false
    /// Bumped whenever held state is cleared, so a geocode that finishes after
    /// a mode change cannot put a place from the old mode back.
    private var generation = 0
    private var defaultsObserver: NSObjectProtocol?

    init(defaults: UserDefaults = .standard, manager: CLLocationManager = CLLocationManager()) {
        self.defaults = defaults
        self.manager = manager
        self.authorizationStatus = manager.authorizationStatus
        self.appliedSharing = LocationPreference.current(in: defaults)
        super.init()
        manager.delegate = self
        // The picker writes through `UserDefaults`; this is how a change while
        // the app is open reaches us without the view having to tell us. On
        // the main queue: the notification arrives on whichever thread wrote,
        // and Swift 5 mode would let a selector run this main-actor code there.
        defaultsObserver = NotificationCenter.default.addObserver(
            forName: UserDefaults.didChangeNotification, object: defaults, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.defaultsChanged() }
        }
    }

    deinit {
        if let defaultsObserver { NotificationCenter.default.removeObserver(defaultsObserver) }
    }

    // MARK: - Lifecycle

    /// Called from the scene phase: true only for `.active`. `.inactive` stops
    /// updates too, which is harmless, since the next message is sent from an
    /// active app and the 15-minute window covers the gap.
    func setAppActive(_ active: Bool) {
        appIsActive = active
        reconcile()
    }

    /// Every defaults write in the app lands here; only a change of mode
    /// matters.
    private func defaultsChanged() {
        guard LocationPreference.current(in: defaults) != appliedSharing else { return }
        reconcile()
    }

    /// Brings Core Location in line with the setting, the scene phase and the
    /// authorization state. Idempotent — it runs on every `UserDefaults` change,
    /// which is far more often than the setting moves.
    private func reconcile() {
        let sharing = LocationPreference.current(in: defaults)
        if sharing != appliedSharing {
            appliedSharing = sharing
            clearHeldState()
        }
        guard sharing != .off, appIsActive else {
            stopUpdates()
            return
        }

        switch sharing {
        case .approximate:
            manager.desiredAccuracy = kCLLocationAccuracyReduced
            manager.distanceFilter = 1000
        case .precise:
            manager.desiredAccuracy = kCLLocationAccuracyBest
            manager.distanceFilter = 50
        case .off:
            break
        }

        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedWhenInUse, .authorizedAlways:
            if !updating {
                updating = true
                manager.startUpdatingLocation()
            }
        default:
            stopUpdates()
        }
    }

    private func stopUpdates() {
        guard updating else { return }
        updating = false
        manager.stopUpdatingLocation()
    }

    private func clearHeldState() {
        generation += 1
        latestFix = nil
        place = nil
        placeOrigin = nil
        lastGeocodeAttempt = nil
    }

    // MARK: - CLLocationManagerDelegate

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in
            self.authorizationStatus = self.manager.authorizationStatus
            self.reconcile()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let newest = locations.last else { return }
        Task { @MainActor in self.receive(newest) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        // Nothing to do: the held fix ages out on its own, and a missing fix
        // already sends nothing.
    }

    private func receive(_ fix: CLLocation) {
        // A fix delivered after the setting went Off must not be kept.
        guard appliedSharing != .off else { return }
        latestFix = fix
        geocodeIfNeeded(for: fix)
    }

    // MARK: - Reverse geocoding

    /// Whether `fix` earns a reverse-geocode. Pure so the rules are testable.
    nonisolated static func needsGeocode(
        fix: CLLocation, origin: CLLocation?, hasPlace: Bool, lastAttempt: Date?, now: Date
    ) -> Bool {
        guard let lastAttempt else { return true }
        guard hasPlace, let origin else {
            return now.timeIntervalSince(lastAttempt) > geocodeRetryDelay
        }
        if fix.distance(from: origin) > regeocodeDistance { return true }
        return now.timeIntervalSince(lastAttempt) > regeocodeAge
    }

    private func geocodeIfNeeded(for fix: CLLocation) {
        // CLGeocoder allows one request at a time and fails the second.
        guard !geocodeInFlight else { return }
        guard Self.needsGeocode(
            fix: fix, origin: placeOrigin, hasPlace: place != nil,
            lastAttempt: lastGeocodeAttempt, now: Date()
        ) else { return }

        geocodeInFlight = true
        lastGeocodeAttempt = Date()
        let sharing = appliedSharing
        let started = generation
        Task { [geocoder] in
            // CLGeocoder is deprecated under the iOS 26 SDK (MapKit replaces
            // it); the deployment target is 17, where this is the API.
            let placemarks = try? await geocoder.reverseGeocodeLocation(fix)
            let formatted = placemarks?.first.flatMap { Self.placeString(for: $0, sharing: sharing) }
            self.finishGeocode(fix: fix, place: formatted, generation: started)
        }
    }

    private func finishGeocode(fix: CLLocation, place formatted: String?, generation started: Int) {
        geocodeInFlight = false
        // The mode changed mid-request: this result describes a different
        // precision, and the state it would land in was already cleared.
        guard started == generation else { return }
        if let formatted {
            place = formatted
            placeOrigin = fix
        }
        // On failure the previous place stays, and `outgoing` drops it once
        // the fix has moved more than `regeocodeDistance` from `placeOrigin`.
    }

    // MARK: - Assembly

    /// What to attach to a message sent at `now`, or nil to attach nothing.
    func currentLocation(now: Date = Date()) -> OutgoingLocation? {
        let status = manager.authorizationStatus
        return Self.outgoing(
            sharing: LocationPreference.current(in: defaults),
            authorized: status == .authorizedWhenInUse || status == .authorizedAlways,
            fix: latestFix, place: place, placeOrigin: placeOrigin, now: now
        )
    }

    /// Pure assembly, so the rules are testable without `CLLocationManager`.
    nonisolated static func outgoing(
        sharing: LocationSharing, authorized: Bool,
        fix: CLLocation?, place: String?, placeOrigin: CLLocation?, now: Date
    ) -> OutgoingLocation? {
        guard sharing != .off, authorized, let fix else { return nil }
        guard now.timeIntervalSince(fix.timestamp) <= maxFixAge else { return nil }
        let coordinate = fix.coordinate
        guard coordinate.latitude.isFinite, coordinate.longitude.isFinite else { return nil }
        let accuracy: Double? = fix.horizontalAccuracy > 0 ? fix.horizontalAccuracy : nil

        // Reduced accuracy is asked of Core Location, but not trusted alone: a
        // full-accuracy fix can still arrive across a Precise -> Approximate
        // switch (a queued delegate Task, or the update before the new
        // desiredAccuracy takes). Rounding here means it never reaches the wire.
        if sharing == .approximate {
            return OutgoingLocation(
                latitude: (coordinate.latitude * 100).rounded() / 100,
                longitude: (coordinate.longitude * 100).rounded() / 100,
                accuracyM: max(accuracy ?? 0, approximateFloor),
                place: placeNear(fix, place: place, origin: placeOrigin)
            )
        }

        return OutgoingLocation(
            latitude: coordinate.latitude,
            longitude: coordinate.longitude,
            accuracyM: accuracy,
            place: placeNear(fix, place: place, origin: placeOrigin)
        )
    }

    /// The place describes where it was geocoded, not where the fix is now.
    nonisolated private static func placeNear(_ fix: CLLocation, place: String?, origin: CLLocation?) -> String? {
        if let origin, fix.distance(from: origin) > regeocodeDistance { return nil }
        return place
    }

    /// Joins the placemark's parts, dropping empty ones and adjacent repeats
    /// ("Paris, Paris, France"). Never the street, thoroughfare or name: the
    /// server and the model need a city, not an address. Approximate stops at
    /// the locality so the neighbourhood doesn't leak what the coordinates
    /// were rounded to hide.
    nonisolated static func placeString(for placemark: CLPlacemark, sharing: LocationSharing) -> String? {
        placeString(
            subLocality: placemark.subLocality, locality: placemark.locality,
            administrativeArea: placemark.administrativeArea, country: placemark.country,
            sharing: sharing
        )
    }

    nonisolated static func placeString(
        subLocality: String?, locality: String?, administrativeArea: String?, country: String?,
        sharing: LocationSharing
    ) -> String? {
        let parts: [String?]
        switch sharing {
        case .off: return nil
        case .approximate: parts = [locality, administrativeArea, country]
        case .precise: parts = [subLocality, locality, administrativeArea, country]
        }
        var kept: [String] = []
        for part in parts {
            let trimmed = part?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard !trimmed.isEmpty, trimmed != kept.last else { continue }
            kept.append(trimmed)
        }
        return kept.isEmpty ? nil : kept.joined(separator: ", ")
    }
}
