import Foundation
import Testing
@testable import Marlow

/// "Your day" in Settings: the wording, the "HH:MM" <-> picker conversion, and
/// what the model does with a refusal.
@MainActor
struct YourDayTests {
    // MARK: Wording

    @Test func eachCodeHasItsOwnLine() {
        #expect(YourDay.message(forCode: "day_start_range") == "Your day has to start between 4 and noon.")
        #expect(YourDay.message(forCode: "day_end_range") == "Your day has to end between 8 pm and 3 am.")
        #expect(YourDay.message(forCode: "night_too_short")
            == "Leave at least 4 hours between the end of your day and the next start.")
    }

    @Test func badTimeAnUnknownCodeAndNoCodeAreTheGenericLine() {
        #expect(YourDay.message(forCode: "bad_time") == "Couldn't save that time.")
        #expect(YourDay.message(forCode: "something_new") == "Couldn't save that time.")
        #expect(YourDay.message(forCode: nil) == "Couldn't save that time.")
    }

    @Test func aRefusalIsWordedByItsCodeAndAMissingNetworkByTheConnection() {
        #expect(YourDay.message(for: APIError.server(status: 422, detail: "day_end_range"))
            == "Your day has to end between 8 pm and 3 am.")
        #expect(YourDay.message(for: URLError(.notConnectedToInternet)) == "Couldn't save. Check your connection.")
    }

    // MARK: Conversion

    private func calendar(_ zone: String) -> Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: zone)!
        return c
    }

    @Test func minutesReadsOnlyZeroPaddedTwentyFourHourTimes() {
        #expect(YourDay.minutes(from: "08:00") == 480)
        #expect(YourDay.minutes(from: "00:00") == 0)
        #expect(YourDay.minutes(from: "23:59") == 1439)
        for bad in ["", "8:00", "24:00", "08:60", "08:00:00", "8 am"] {
            #expect(YourDay.minutes(from: bad) == nil, "\(bad)")
        }
    }

    @Test func aTimeRoundTripsThroughTheDatePickersDate() throws {
        // Including the hours a clock change moves in some zones: the picker's
        // Date is a time of day, so the same wall time must come back.
        for zone in ["America/Toronto", "Europe/London", "Australia/Lord_Howe", "UTC"] {
            let cal = calendar(zone)
            for hm in ["00:00", "02:30", "04:00", "08:00", "12:00", "20:15", "23:00", "23:59"] {
                let date = try #require(YourDay.date(from: hm, calendar: cal))
                #expect(YourDay.string(from: date, calendar: cal) == hm, "\(hm) in \(zone)")
            }
        }
    }

    @Test func aBadStringHasNoDate() {
        #expect(YourDay.date(from: "later", calendar: calendar("UTC")) == nil)
    }

    @Test func thePickersWallTimeIsWhatIsSentWhateverTheClockStyle() {
        // 8:00 PM in the picker is "20:00" on the wire, in any locale.
        let cal = calendar("America/Toronto")
        let evening = cal.date(from: DateComponents(year: 2026, month: 10, day: 21, hour: 20, minute: 0))!
        #expect(YourDay.string(from: evening, calendar: cal) == "20:00")
    }

    @Test func anOlderBoxWithoutTheFieldsStillDecodes() throws {
        let json = Data(#"{"face": 3, "effort": "low"}"#.utf8)
        let prefs = try JSONDecoder().decode(Preferences.self, from: json)
        #expect(prefs == Preferences(face: 3, effort: "low"))
        #expect(prefs.dayStart == nil)
        let now = try JSONDecoder().decode(
            Preferences.self,
            from: Data(#"{"face": null, "effort": null, "day_start": "08:00", "day_end": "02:00"}"#.utf8)
        )
        #expect(now.dayStart == "08:00")
        #expect(now.dayEnd == "02:00")
        #expect(now.checkinEveryHours == nil)
        let withCheckins = try JSONDecoder().decode(
            Preferences.self,
            from: Data(#"{"face": null, "effort": null, "day_start": "08:00", "day_end": "23:00", "checkin_every_hours": 0}"#.utf8)
        )
        #expect(withCheckins.checkinEveryHours == 0)
    }

    // MARK: Check-ins

    @Test func checkinOptionsRunFromOffToSixHoursEachWordedForItsNumber() {
        #expect(YourDay.checkinOptions.map(YourDay.checkinLabel) == [
            "Off", "Every hour", "Every 2 hours", "Every 3 hours", "Every 4 hours", "Every 5 hours", "Every 6 hours",
        ])
    }

    @Test func aCheckinRefusalHasNoCodeToWordSoItIsTheSettingLine() {
        let validation = APIError.server(status: 422, detail: nil)
        #expect(YourDay.message(for: validation, checkin: true) == "Couldn't save that setting.")
        #expect(YourDay.message(for: URLError(.notConnectedToInternet), checkin: true)
            == "Couldn't save. Check your connection.")
    }

    @Test func pickingACheckinSendsOnlyThatAndShowsTheBoxsWholeAnswer() async {
        let api = FakeDayAPI(.success(Preferences(
            face: nil, effort: nil, dayStart: "08:00", dayEnd: "23:00", checkinEveryHours: 3)))
        let model = YourDayModel()
        await model.load(api: api, deviceToken: "t")
        #expect(model.times?.checkinEveryHours == 3)
        api.next = .success(Preferences(
            face: nil, effort: nil, dayStart: "08:15", dayEnd: "23:00", checkinEveryHours: 0))
        await model.setCheckin(0, api: api, deviceToken: "t")
        #expect(api.sent == [FakeDayAPI.Sent(start: nil, end: nil, checkin: 0)])
        #expect(model.times == YourDayModel.Times(start: "08:15", end: "23:00", checkinEveryHours: 0))
        #expect(model.message == nil)
    }

    @Test func aRefusedCheckinRevertsAndSaysSo() async {
        let api = FakeDayAPI(.success(Preferences(
            face: nil, effort: nil, dayStart: "08:00", dayEnd: "23:00", checkinEveryHours: 3)))
        let model = YourDayModel()
        await model.load(api: api, deviceToken: "t")
        api.next = .failure(APIError.server(status: 422, detail: nil))
        await model.setCheckin(6, api: api, deviceToken: "t")
        #expect(model.times?.checkinEveryHours == 3)
        #expect(model.message == "Couldn't save that setting.")

        api.next = .failure(URLError(.notConnectedToInternet))
        await model.setCheckin(1, api: api, deviceToken: "t")
        #expect(model.times?.checkinEveryHours == 3)
        #expect(model.message == "Couldn't save. Check your connection.")
    }

    @Test func aBoxWithoutCheckinsKeepsTheDayAndHasNothingToSet() async {
        let api = FakeDayAPI(.success(Preferences(face: nil, effort: nil, dayStart: "08:00", dayEnd: "23:00")))
        let model = YourDayModel()
        await model.load(api: api, deviceToken: "t")
        #expect(model.times == YourDayModel.Times(start: "08:00", end: "23:00", checkinEveryHours: nil))
        await model.setCheckin(2, api: api, deviceToken: "t")
        #expect(api.sent.isEmpty)
    }

    // MARK: Model

    @Test func nothingIsShownUntilTheBoxHasAnswered() async {
        let api = FakeDayAPI(.success(Preferences(face: nil, effort: nil, dayStart: "07:30", dayEnd: "02:00")))
        let model = YourDayModel()
        #expect(model.times == nil)
        await model.load(api: api, deviceToken: "t")
        #expect(model.times == YourDayModel.Times(start: "07:30", end: "02:00"))

        // A box that predates the setting, or one that can't be reached.
        let old = YourDayModel()
        await old.load(api: FakeDayAPI(.success(Preferences(face: 1, effort: nil))), deviceToken: "t")
        #expect(old.times == nil)
        let down = YourDayModel()
        await down.load(api: FakeDayAPI(.failure(URLError(.notConnectedToInternet))), deviceToken: "t")
        #expect(down.times == nil)
    }

    @Test func aChangeSendsOnlyThatFieldAndShowsTheBoxsAnswer() async {
        let api = FakeDayAPI(.success(Preferences(face: nil, effort: nil, dayStart: "08:00", dayEnd: "23:00")))
        let model = YourDayModel()
        await model.load(api: api, deviceToken: "t")
        api.next = .success(Preferences(face: nil, effort: nil, dayStart: "08:00", dayEnd: "22:00"))
        await model.set(.end, to: "22:00", api: api, deviceToken: "t")
        #expect(api.sent == [FakeDayAPI.Sent(start: nil, end: "22:00")])
        #expect(model.times == YourDayModel.Times(start: "08:00", end: "22:00"))
        #expect(model.message == nil)
    }

    @Test func aRefusalPutsTheLastGoodTimeBackAndSaysWhy() async {
        let api = FakeDayAPI(.success(Preferences(face: nil, effort: nil, dayStart: "08:00", dayEnd: "23:00")))
        let model = YourDayModel()
        await model.load(api: api, deviceToken: "t")
        api.next = .failure(APIError.server(status: 422, detail: "night_too_short"))
        await model.set(.start, to: "12:00", api: api, deviceToken: "t")
        #expect(model.times == YourDayModel.Times(start: "08:00", end: "23:00"))
        #expect(model.message == "Leave at least 4 hours between the end of your day and the next start.")

        // The next try starts clean, and no network reverts with its own line.
        api.next = .failure(URLError(.notConnectedToInternet))
        await model.set(.start, to: "09:00", api: api, deviceToken: "t")
        #expect(model.times?.start == "08:00")
        #expect(model.message == "Couldn't save. Check your connection.")

        api.next = .success(Preferences(face: nil, effort: nil, dayStart: "09:00", dayEnd: "23:00"))
        await model.set(.start, to: "09:00", api: api, deviceToken: "t")
        #expect(model.times?.start == "09:00")
        #expect(model.message == nil)
    }

    @Test func settingTheTimeAlreadyShownSendsNothing() async {
        let api = FakeDayAPI(.success(Preferences(face: nil, effort: nil, dayStart: "08:00", dayEnd: "23:00")))
        let model = YourDayModel()
        await model.load(api: api, deviceToken: "t")
        await model.set(.start, to: "08:00", api: api, deviceToken: "t")
        #expect(api.sent.isEmpty)
    }
}

@MainActor
private final class FakeDayAPI: YourDayAPI {
    struct Sent: Equatable {
        var start: String?
        var end: String?
        var checkin: Int?
    }

    private let initial: Result<Preferences, Error>
    var next: Result<Preferences, Error> = .failure(URLError(.badServerResponse))
    private(set) var sent: [Sent] = []

    init(_ initial: Result<Preferences, Error>) { self.initial = initial }

    func preferences(deviceToken: String) async throws -> Preferences { try initial.get() }

    func setDay(start: String?, end: String?, checkinEveryHours: Int?, deviceToken: String) async throws -> Preferences {
        sent.append(Sent(start: start, end: end, checkin: checkinEveryHours))
        return try next.get()
    }
}
