import SwiftUI

/// A time-of-day picker in 15-minute steps. SwiftUI's `DatePicker` has no
/// `minuteInterval`, so this wraps `UIDatePicker`, which shows the time in the
/// device's own 12h/24h style.
struct QuarterHourPicker: UIViewRepresentable {
    @Binding var date: Date

    func makeUIView(context: Context) -> UIDatePicker {
        let picker = UIDatePicker()
        picker.datePickerMode = .time
        picker.preferredDatePickerStyle = .compact
        picker.minuteInterval = 15
        picker.date = date
        picker.addTarget(context.coordinator, action: #selector(Coordinator.changed(_:)), for: .valueChanged)
        return picker
    }

    func updateUIView(_ picker: UIDatePicker, context: Context) {
        context.coordinator.parent = self
        if picker.date != date { picker.date = date }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject {
        var parent: QuarterHourPicker

        init(_ parent: QuarterHourPicker) { self.parent = parent }

        @objc func changed(_ picker: UIDatePicker) { parent.date = picker.date }
    }
}
