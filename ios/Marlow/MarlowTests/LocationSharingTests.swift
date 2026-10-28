import CoreLocation
import Foundation
import Testing
@testable import Marlow

/// docs/tasks/message-location.md, iOS half. Real Core Location, the
/// permission prompt and real geocoding cannot run in a test; everything that
/// decides *what is sent* is a pure function and is pinned here.
struct LocationSharingTests {

    private func makeDefaults() -> (UserDefaults, String) {
        let name = "LocationSharingTests-\(UUID().uuidString)"
        return (UserDefaults(suiteName: name)!, name)
    }

    // MARK: - Preference

    @Test func defaultsToOff() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        #expect(LocationPreference.current(in: defaults) == .off)
    }

    @Test func roundTripsThroughDefaults() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        for mode in LocationSharing.allCases {
            LocationPreference.set(mode, in: defaults)
            #expect(LocationPreference.current(in: defaults) == mode)
        }
        #expect(LocationPreference.storageKey == "locationSharing")
    }

    /// A mode from a future build must never become sharing the user didn't pick.
    @Test func anUnknownStoredValueFallsBackToOff() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set("exact", forKey: LocationPreference.storageKey)
        #expect(LocationPreference.current(in: defaults) == .off)
    }

    @Test func displayNames() {
        #expect(LocationSharing.allCases.map(\.displayName) == ["Off", "Approximate", "Precise"])
    }

    // MARK: - Place string

    private func place(
        _ sharing: LocationSharing,
        sub: String? = "Old Québec", locality: String? = "Québec",
        area: String? = "QC", country: String? = "Canada"
    ) -> String? {
        LocationProvider.placeString(
            subLocality: sub, locality: locality, administrativeArea: area, country: country,
            sharing: sharing
        )
    }

    @Test func approximatePlaceStopsAtTheLocality() {
        #expect(place(.approximate) == "Québec, QC, Canada")
    }

    @Test func precisePlaceAddsTheNeighbourhood() {
        #expect(place(.precise) == "Old Québec, Québec, QC, Canada")
    }

    @Test func placeDropsNilEmptyAndBlankParts() {
        #expect(place(.precise, sub: nil, area: "") == "Québec, Canada")
        #expect(place(.precise, sub: "  ", area: nil) == "Québec, Canada")
    }

    @Test func placeDropsAdjacentDuplicates() {
        // "Paris, Paris, France" reads as noise.
        #expect(place(.approximate, locality: "Singapore", area: "Singapore", country: "Singapore") == "Singapore")
        #expect(place(.precise, sub: "Paris", locality: "Paris", area: "Île-de-France", country: "France")
                == "Paris, Île-de-France, France")
    }

    @Test func placeIsNilWhenNothingIsLeft() {
        #expect(place(.approximate, sub: nil, locality: nil, area: "", country: nil) == nil)
    }

    // MARK: - Freshness and assembly

    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    private func fix(
        age: TimeInterval, accuracy: CLLocationAccuracy = 65,
        latitude: Double = 46.81, longitude: Double = -71.21
    ) -> CLLocation {
        CLLocation(
            coordinate: CLLocationCoordinate2D(latitude: latitude, longitude: longitude),
            altitude: 0, horizontalAccuracy: accuracy, verticalAccuracy: -1,
            timestamp: now.addingTimeInterval(-age)
        )
    }

    private func outgoing(
        sharing: LocationSharing = .precise, authorized: Bool = true,
        fix: CLLocation?, place: String? = "Québec, QC, Canada", origin: CLLocation? = nil
    ) -> OutgoingLocation? {
        LocationProvider.outgoing(
            sharing: sharing, authorized: authorized, fix: fix, place: place,
            placeOrigin: origin, now: now
        )
    }

    @Test func aFourteenMinuteOldFixIsSent() throws {
        let sent = try #require(outgoing(fix: fix(age: 14 * 60)))
        #expect(sent.latitude == 46.81)
        #expect(sent.longitude == -71.21)
        #expect(sent.accuracyM == 65)
        #expect(sent.place == "Québec, QC, Canada")
    }

    /// A precise fix that slipped through a switch to Approximate still goes
    /// out at ~1 km.
    @Test func approximateRoundsWhateverCoreLocationGave() throws {
        let sent = try #require(outgoing(sharing: .approximate, fix: fix(age: 1, accuracy: 5, latitude: 46.814949, longitude: -71.213838)))
        #expect(sent.latitude == 46.81)
        #expect(sent.longitude == -71.21)
        #expect(sent.accuracyM == 1000)
    }

    @Test func aSixteenMinuteOldFixIsNotSent() {
        #expect(outgoing(fix: fix(age: 16 * 60)) == nil)
    }

    @Test func offSendsNothingEvenWithAFreshFix() {
        #expect(outgoing(sharing: .off, fix: fix(age: 10)) == nil)
    }

    @Test func withoutPermissionNothingIsSent() {
        #expect(outgoing(authorized: false, fix: fix(age: 10)) == nil)
    }

    @Test func withoutAFixNothingIsSent() {
        #expect(outgoing(fix: nil) == nil)
    }

    /// Core Location reports -1 for an unusable accuracy; the server rejects
    /// zero or less and drops the whole object.
    @Test func anInvalidAccuracyIsLeftOut() throws {
        #expect(try #require(outgoing(fix: fix(age: 1, accuracy: -1))).accuracyM == nil)
        #expect(try #require(outgoing(fix: fix(age: 1, accuracy: 0))).accuracyM == nil)
    }

    @Test func aPlaceFromFarAwayIsDroppedButTheFixStillGoes() throws {
        // ~1.1 km north of where the place was geocoded.
        let origin = fix(age: 60, latitude: 46.80, longitude: -71.21)
        let moved = fix(age: 1, latitude: 46.81, longitude: -71.21)
        let sent = try #require(outgoing(fix: moved, origin: origin))
        #expect(sent.place == nil)
        #expect(sent.latitude == 46.81)
    }

    @Test func aPlaceNearTheFixIsKept() throws {
        let origin = fix(age: 60, latitude: 46.810, longitude: -71.21)
        let nearby = fix(age: 1, latitude: 46.813, longitude: -71.21)
        #expect(try #require(outgoing(fix: nearby, origin: origin)).place == "Québec, QC, Canada")
    }

    // MARK: - When to geocode

    private func needs(
        fix: CLLocation, origin: CLLocation?, hasPlace: Bool = true, attemptAge: TimeInterval? = 60
    ) -> Bool {
        LocationProvider.needsGeocode(
            fix: fix, origin: origin, hasPlace: hasPlace,
            lastAttempt: attemptAge.map { now.addingTimeInterval(-$0) }, now: now
        )
    }

    @Test func geocodesWhenThereIsNoPlaceYet() {
        #expect(needs(fix: fix(age: 1), origin: nil, hasPlace: false, attemptAge: nil))
        #expect(needs(fix: fix(age: 1), origin: fix(age: 5), hasPlace: false, attemptAge: 61))
    }

    /// Offline, every fix would otherwise fire a geocode, and Apple throttles.
    @Test func aFailedGeocodeWaitsBeforeTryingAgain() {
        #expect(!needs(fix: fix(age: 1), origin: nil, hasPlace: false, attemptAge: 30))
    }

    @Test func doesNotGeocodeAgainForASmallMoveSoon() {
        let origin = fix(age: 60)
        #expect(!needs(fix: fix(age: 1, latitude: 46.812), origin: origin))
    }

    @Test func geocodesAfterMovingMoreThanFiveHundredMetres() {
        let origin = fix(age: 60, latitude: 46.80)
        #expect(needs(fix: fix(age: 1, latitude: 46.81), origin: origin))
    }

    @Test func geocodesWhenTheLastOneIsOverTenMinutesOld() {
        let origin = fix(age: 60)
        #expect(needs(fix: fix(age: 1), origin: origin, attemptAge: 11 * 60))
        #expect(!needs(fix: fix(age: 1), origin: origin, attemptAge: 9 * 60))
    }

    // MARK: - Reaches the wire from the store

    @MainActor
    @Test func theStoreAttachesWhateverTheProviderGivesAtSendTime() async throws {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "t1")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])
        var current: OutgoingLocation? = OutgoingLocation(latitude: 1, longitude: 2, accuracyM: 30, place: "Here")
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(), preferences: defaults, currentLocation: { current }
        )
        defer { store.cancelActiveWork() }

        store.send(text: "first")
        await awaitSettled(store)
        current = nil
        store.send(text: "second")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[0].location == OutgoingLocation(latitude: 1, longitude: 2, accuracyM: 30, place: "Here"))
        #expect(sent[1].location == nil)
    }
}
