import SwiftUI
import UIKit

/// Marlow's face floating over the top of the transcript, with a pill under it
/// that says what the turn in flight is doing.
///
/// **This replaced the navigation bar**, and took over both of its jobs. The
/// bar's title was "Sonnet/Low", a diagnostic Mike asked for on 2026-08-27 so
/// an odd answer could be checked against the settings at a glance; it moved
/// to a chip top-left, and the chip was removed 2026-10-12 (Mike: low value;
/// the lightbulb took its slot, and model and effort stay visible in
/// Settings). The bar's leading icon answered "whose
/// conversation is this"; the avatar still does. What is new is the working
/// state, which used to be a spinner inside the reply's own row and scrolled
/// away with it. The header does not scroll, so "Searching the web…" is
/// visible from anywhere in the transcript.
///
/// Tapping the avatar opens what Marlow has done. For now that is the activity
/// log on its own (docs/tasks/ux-lane.md §2c); step 3's transparency view
/// (recent actions, SOUL, memory) takes the tap over and holds the log as one
/// part. Settings stays on the gear, Mike's call, 2026-09-28.
struct PresenceHeader: View {
    let presence: ChatPresence
    /// A spotlight is waiting, or an idea has arrived since the panel was last
    /// opened (`IdeaFormat.showsDot`).
    let ideasHaveNews: Bool
    let onSettings: () -> Void
    let onAvatar: () -> Void
    let onIdeas: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var isWorking: Bool { presence != .idle }

    var body: some View {
        ZStack(alignment: .top) {
            // Clears the pulsing ring (padding -4 plus half its 2.5 stroke):
            // any overlap clips the glow, which read as a bug on device.
            VStack(spacing: 6) {
                Button(action: onAvatar) { avatar }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Recent activity")
                pill
            }
            .frame(maxWidth: .infinity)

            HStack(spacing: 0) {
                ideasButton
                Spacer()
                FloatingIconButton(systemName: "gearshape", action: onSettings)
                    .accessibilityLabel("Settings")
            }
            .padding(.horizontal, 16)
            .padding(.top, 6)
        }
        .padding(.top, 4)
        .padding(.bottom, 10)
        // Fades the transcript out as it passes underneath, rather than
        // cutting it off at a hard edge. The header floats: rows scroll
        // beneath it, and the transcript's top inset keeps the first row
        // clear of it at rest.
        .background {
            LinearGradient(
                stops: [
                    .init(color: Color(.systemBackground), location: 0),
                    .init(color: Color(.systemBackground), location: 0.7),
                    .init(color: Color(.systemBackground).opacity(0), location: 1),
                ],
                startPoint: .top, endPoint: .bottom
            )
            .ignoresSafeArea(edges: .top)
        }
    }

    private var avatar: some View {
        MarlowAvatar(size: 52)
            .overlay {
                // A slow breath on the ring rather than a spinner: the pill
                // says what is happening, this only says that something is.
                // Static under Reduce Motion, still visibly "on".
                //
                // Paused while idle, so an idle chat is not redrawing thirty
                // times a second for a ring nobody can see.
                TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !isWorking || reduceMotion)) { context in
                    Circle()
                        .stroke(Color.accentColor, lineWidth: 2.5)
                        .padding(-4)
                        .opacity(isWorking ? ringOpacity(at: context.date) : 0)
                }
            }
            .animation(.easeInOut(duration: 0.25), value: isWorking)
            .accessibilityHidden(true)
    }

    /// 0.35 → 1 → 0.35 over 1.8s.
    private func ringOpacity(at date: Date) -> Double {
        guard !reduceMotion else { return 1 }
        let phase = date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.8) / 1.8
        return 0.35 + 0.65 * (0.5 - 0.5 * cos(phase * 2 * .pi))
    }

    private var pill: some View {
        HStack(spacing: 6) {
            switch presence {
            case .idle:
                Text("Marlow")
                    .font(.subheadline.weight(.semibold))
            case .active(let label):
                Text(label)
                    .font(.subheadline.weight(.medium))
                    .contentTransition(.opacity)
            }
        }
        .lineLimit(1)
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .background(.regularMaterial, in: Capsule())
        .overlay(Capsule().stroke(Color.primary.opacity(0.08), lineWidth: 0.5))
        .animation(.easeInOut(duration: 0.2), value: presence)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    /// The lightbulb, top-left where the model chip was, balancing the gear in
    /// the same 44pt target (docs/tasks/ideas-panel.md, Surface). The dot is the
    /// same accent as the working ring: "there is something here", never a
    /// count.
    private var ideasButton: some View {
        FloatingIconButton(systemName: "lightbulb", action: onIdeas)
            .overlay(alignment: .topTrailing) {
                if ideasHaveNews {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 9, height: 9)
                        .overlay(Circle().stroke(Color(.systemBackground), lineWidth: 1.5))
                        .offset(x: -6, y: 6)
                        .accessibilityHidden(true)
                }
            }
            .accessibilityLabel(ideasHaveNews ? "Ideas, new" : "Ideas")
    }

    private var accessibilityText: String {
        switch presence {
        case .idle: return "Marlow"
        case .active(let label): return "Marlow is working: \(label)"
        }
    }
}

/// What an empty transcript shows, under the header's avatar. Marlow's voice,
/// kept to two lines: it only has to make the first message obvious. The face
/// leads it at full size, in the large crop: an empty chat is where someone
/// meets Marlow (Mike, 2026-10-16).
struct EmptyChatGreeting: View {
    /// The first transcript fetch hasn't answered yet, so "empty" may only
    /// mean "not loaded": say so rather than greet a returning user as new.
    var loading = false

    var body: some View {
        VStack(spacing: 6) {
            MarlowAvatar(size: 120, crop: .large)
                .padding(.bottom, 10)
                .accessibilityHidden(true)
            Text("Hi, I'm Marlow.")
                .font(.title3.weight(.semibold))
            if loading {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Loading your conversation…")
                }
                .font(.subheadline)
                .foregroundStyle(.secondary)
            } else {
                Text("Ask me anything, or hand me something to keep track of.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .multilineTextAlignment(.center)
        .padding(.horizontal, 40)
        .accessibilityElement(children: .combine)
    }
}

/// The chosen portrait, cropped round and pulled in toward the face. At 52pt
/// the whole portrait reads as a dark smudge; the face is what makes it Marlow.
struct MarlowAvatar: View {
    let size: CGFloat
    /// A specific face (the picker's tiles); `nil` follows the stored choice.
    var face: MarlowFace? = nil
    var crop: FaceCrop.Size = .header
    @AppStorage(FacePreference.storageKey) private var storedFaceID = MarlowFace.default.id

    var body: some View {
        let shown = face ?? MarlowFace.with(id: storedFaceID)
        let framing = shown.crop(crop)
        Image(shown.imageName)
            .resizable()
            .aspectRatio(contentMode: .fill)
            // Each face sits right of centre and somewhere above the middle of
            // its portrait; anchor the zoom there so it lands in the circle.
            .scaleEffect(framing.zoom, anchor: framing.anchor)
            .frame(width: size, height: size)
            .clipShape(Circle())
            .overlay(Circle().stroke(Color.primary.opacity(0.12), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.25), radius: 6, y: 2)
    }
}

/// A round, material-backed control that floats over content — the chat's
/// one shape for a free-standing button, so the gear and the jump-to-latest
/// arrow read as the same family.
struct FloatingIconButton: View {
    let systemName: String
    var size: CGFloat = 40
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: size * 0.42, weight: .semibold))
                .foregroundStyle(.primary)
                .frame(width: size, height: size)
                .background(.regularMaterial, in: Circle())
                .overlay(Circle().stroke(Color.primary.opacity(0.08), lineWidth: 0.5))
                // The visible circle is 40pt; the tap target is not allowed
                // to be smaller than 44.
                .frame(width: max(44, size), height: max(44, size))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
    }
}

/// A small rotating arc in the accent colour, shown at the foot of a reply
/// that is still being worked on.
///
/// Deliberately not a "typing" bubble (Mike, 2026-09-28): most of a turn's
/// wait is tools and thinking, not writing, and three bouncing dots would
/// claim otherwise. The header says WHAT the work is; this only marks WHERE
/// the answer will appear.
struct WorkingIndicator: View {
    /// What VoiceOver says. The visual carries no text.
    let label: String

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: reduceMotion)) { context in
            let seconds = context.date.timeIntervalSinceReferenceDate
            Circle()
                .trim(from: 0, to: 0.7)
                .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                .frame(width: 18, height: 18)
                .rotationEffect(.degrees(reduceMotion ? 0 : (seconds * 360).truncatingRemainder(dividingBy: 360)))
        }
        .frame(width: 22, height: 22)
        .padding(.vertical, 4)
        .accessibilityElement()
        .accessibilityLabel(label)
    }
}
