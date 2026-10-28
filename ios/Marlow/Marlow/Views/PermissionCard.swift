import SwiftUI

/// The tool-approval UI: a dimming layer and a card in the middle of the
/// screen, laid over `ChatView` (docs/tasks/ios-permission-round-trip-ui.md
/// §4, redesigned 2026-10-02).
///
/// **A card, not a sheet.** The old `PermissionSheet` was full screen, which
/// hid the one thing Mike needs to judge the request: what he just asked
/// Marlow for, sitting at the foot of the transcript above the composer. The
/// card hugs its content, never grows past 60% of the screen height, and lets
/// the conversation show above and below it.
///
/// **The card never states what the model says as fact.** `summary.title` and
/// `summary.preview` are lifted by the server from the tool input itself;
/// `summary.marlowSays` is the model's own description, which a prompt
/// injection can write as easily as an honest turn can, so it is drawn as a
/// quotation with a label, in a different style from everything else. The raw
/// input is always one tap away ("Show details") — the card is a reading aid,
/// never the only view of what is being approved.
///
/// **Secondary text is `Color(.secondaryLabel)`, not `.secondary`.** The
/// hierarchical `.secondary` style rendered invisible (the text laid out but
/// did not draw) once the content sat inside the card's `ScrollView` over its
/// material (simulator, iOS 26.5, 2026-10-02). A fixed colour draws.
struct PermissionOverlay: View {
    /// Nil shows nothing. Always in the view tree rather than inserted by the
    /// caller, so the appear/disappear transitions below belong to views this
    /// type inserts itself and run reliably.
    let request: PermissionRequestInfo?
    /// "allow", "deny" or "allow_similar". Async so the card can keep its
    /// buttons off while the answer is in flight — a second tap otherwise
    /// posts a second answer and the server's "already resolved" reply shows
    /// up as a false "too late" notice.
    let onDecision: (String) async -> Void
    let onExpire: (PermissionRequestInfo) -> Void

    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        GeometryReader { screen in
            ZStack {
                if let request {
                    // Blocks the transcript behind it, and does nothing when
                    // tapped: a stray tap must never be an answer, and there
                    // is no "dismiss" for a request waiting on the agent.
                    Color.black.opacity(colorScheme == .dark ? 0.35 : 0.2)
                        .ignoresSafeArea()
                        .contentShape(Rectangle())
                        .onTapGesture {}
                        .accessibilityHidden(true)
                        .transition(.opacity)

                    PermissionCard(
                        request: request, maxHeight: screen.size.height * 0.6,
                        onDecision: onDecision, onExpire: { onExpire(request) }
                    )
                    .id(request.requestId)
                    .padding(.horizontal, 20)
                    .frame(maxWidth: 420)
                    .transition(.scale(scale: 0.94).combined(with: .opacity))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .animation(.spring(response: 0.38, dampingFraction: 0.86), value: request?.requestId)
        }
        .allowsHitTesting(request != nil)
    }
}

struct PermissionCard: View {
    let request: PermissionRequestInfo
    let maxHeight: CGFloat
    let onDecision: (String) async -> Void
    let onExpire: () -> Void

    @State private var armed = false
    @State private var expired = false
    @State private var busy = false
    @State private var detailsExpanded = false
    @State private var contentHeight: CGFloat = 0
    @State private var buttonsHeight: CGFloat = 0

    private var summary: PermissionSummary? { request.summary }
    private var buttonsEnabled: Bool { armed && !expired && !busy }
    /// 21 is the time bar and its padding, which does not change size.
    private var scrollHeight: CGFloat { min(contentHeight, max(0, maxHeight - 21 - buttonsHeight)) }

    var body: some View {
        VStack(spacing: 0) {
            PermissionTimeBar(request: request) {
                expired = true
                onExpire()
            }
            .padding(.horizontal, 20)
            .padding(.top, 14)
            .padding(.bottom, 4)

            // Always a scroll view, given exactly its content's height or what
            // is left under `maxHeight`: the card hugs a short request and,
            // when Dynamic Type or a long preview would make it taller than
            // `maxHeight`, the content scrolls while the buttons stay pinned
            // underneath — the decision is never scrolled out of reach.
            // Measured rather than left to layout: `ViewThatFits` and
            // `.frame(maxHeight:)` were both tried and the card always grew
            // to `maxHeight`, with blank space above and below the content
            // (simulator, 2026-10-02).
            ScrollView {
                content
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { contentHeight = $0 }
            }
            .scrollBounceBehavior(.basedOnSize)
            .frame(height: scrollHeight)

            buttons
                .padding(.horizontal, 20)
                .padding(.top, 4)
                .padding(.bottom, 20)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { buttonsHeight = $0 }
        }
        // Hidden for the one frame before the content has been measured.
        .opacity(contentHeight > 0 ? 1 : 0)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 28, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 28, style: .continuous)
                .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
        )
        .shadow(color: .black.opacity(0.18), radius: 28, y: 10)
        .accessibilityElement(children: .contain)
        .accessibilityAddTraits(.isModal)
        // 0.5s before any button works, the same arm delay the web client
        // uses: a touch already in flight when the card appears (the tap that
        // sent the message, a scroll) must not land on Allow.
        .task(id: request.requestId) {
            armed = false
            try? await Task.sleep(for: .milliseconds(500))
            armed = true
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 14) {
            header

            if let says = summary?.marlowSays {
                MarlowSaysBlock(text: says)
            }

            if let preview = summary?.preview, !preview.isEmpty {
                PermissionPreview(lines: preview)
            }

            details
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(summary == nil ? "Marlow wants to use" : "Marlow wants to")
                .font(.subheadline)
                .foregroundStyle(Color(.secondaryLabel))
            Text(summary?.title ?? request.tool)
                .font(.title3.weight(.semibold))
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    /// The raw request, always present. Collapsed it is a three-line peek; a
    /// long input (a whole file for `Write`) expands into its own capped
    /// scroll view rather than stretching the card.
    private var details: some View {
        VStack(alignment: .leading, spacing: 6) {
            let raw = AnyCodableValue.formattedPairs(request.toolInput).joined(separator: "\n")
            if detailsExpanded {
                ScrollView {
                    Text(raw)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 180)
            } else {
                Text(raw)
                    .lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Button {
                withAnimation(.snappy(duration: 0.25)) { detailsExpanded.toggle() }
            } label: {
                HStack(spacing: 4) {
                    Text(detailsExpanded ? "Hide details" : "Show details")
                    Image(systemName: "chevron.down")
                        .font(.caption2.weight(.semibold))
                        .rotationEffect(.degrees(detailsExpanded ? 180 : 0))
                }
                .font(.footnote.weight(.medium))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.accentColor)
        }
        .font(.system(.caption, design: .monospaced))
        .foregroundStyle(Color(.secondaryLabel))
    }

    private var buttons: some View {
        VStack(spacing: 10) {
            HStack(spacing: 12) {
                Button {
                    decide("deny")
                } label: {
                    Text("Deny").frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(.primary)

                Button {
                    decide("allow")
                } label: {
                    Text("Allow").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
            }
            .controlSize(.large)
            .buttonBorderShape(.capsule)

            if let similar = summary?.similar {
                VStack(spacing: 2) {
                    Button {
                        decide("allow_similar")
                    } label: {
                        Text("Allow similar for this reply")
                            .font(.subheadline.weight(.medium))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .buttonBorderShape(.capsule)
                    .controlSize(.regular)
                    Text("Covers \(similar.label)")
                        .font(.caption)
                        .foregroundStyle(Color(.secondaryLabel))
                }
            }
        }
        .disabled(!buttonsEnabled)
    }

    private func decide(_ decision: String) {
        busy = true
        Task {
            await onDecision(decision)
            busy = false
        }
    }
}

/// Drains from full to empty between the request's creation and the server's
/// own deadline. Deliberately a bar with no number: a ticking "12s left" made
/// the old sheet feel like a bomb timer (Mike, 2026-10-02), and the decision
/// is not meant to be rushed. It only turns orange for the last ten seconds.
struct PermissionTimeBar: View {
    let request: PermissionRequestInfo
    let onExpire: () -> Void

    var body: some View {
        TimelineView(.animation(minimumInterval: 0.1)) { context in
            let remaining = max(0, request.expiresAt - context.date.timeIntervalSince1970)
            let total = max(1, request.expiresAt - request.createdAt)
            let fraction = min(1, remaining / total)
            let urgent = remaining <= 10
            Capsule()
                .fill(Color.primary.opacity(0.08))
                .frame(height: 3)
                .overlay(alignment: .leading) {
                    GeometryReader { track in
                        Capsule()
                            .fill(urgent ? Color.orange : Color.accentColor.opacity(0.75))
                            .frame(width: track.size.width * fraction)
                    }
                }
                .animation(.easeInOut(duration: 0.4), value: urgent)
                .accessibilityElement()
                .accessibilityLabel("Time left to decide: \(Int(remaining.rounded(.up))) seconds")
                .onChange(of: remaining <= 0, initial: true) { _, expired in
                    if expired { onExpire() }
                }
        }
        .frame(height: 3)
    }
}

/// The model's own account of why it wants this, drawn as a quotation. It is
/// the one part of the card that is a claim rather than a fact, so it carries
/// a label, quote marks and a tinted background that nothing factual uses.
private struct MarlowSaysBlock: View {
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "text.bubble")
                .font(.subheadline)
                .foregroundStyle(Color.accentColor)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 3) {
                Text("Marlow says")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color(.secondaryLabel))
                Text("\u{201C}\(text)\u{201D}")
                    .font(.subheadline.italic())
                    .foregroundStyle(Color(.secondaryLabel))
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(Color.accentColor.opacity(0.1), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Marlow says: \(text)")
    }
}

/// The facts lifted from the request, as a diff: removed lines on red, added
/// lines on green, anything else plain. One line each, cut with an ellipsis —
/// the full text is in the raw details.
private struct PermissionPreview: View {
    let lines: [PermissionPreviewLine]

    var body: some View {
        VStack(spacing: 0) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(sign(line.op))
                        .foregroundStyle(signColor(line.op))
                        .frame(width: 10)
                    Text(line.text)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 3)
                .background(tint(line.op))
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(spoken(line.op))\(line.text)")
            }
        }
        .font(.system(.footnote, design: .monospaced))
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func sign(_ op: PermissionPreviewLine.Op) -> String {
        switch op {
        case .remove: return "\u{2212}"
        case .add: return "+"
        case .text: return ""
        }
    }

    private func signColor(_ op: PermissionPreviewLine.Op) -> Color {
        switch op {
        case .remove: return .red
        case .add: return .green
        case .text: return .secondary
        }
    }

    private func tint(_ op: PermissionPreviewLine.Op) -> Color {
        switch op {
        case .remove: return Color.red.opacity(0.16)
        case .add: return Color.green.opacity(0.16)
        case .text: return .clear
        }
    }

    private func spoken(_ op: PermissionPreviewLine.Op) -> String {
        switch op {
        case .remove: return "Removed: "
        case .add: return "Added: "
        case .text: return ""
        }
    }
}

/// What stands in for the card while Mike is dictating: the request is real
/// and ticking, but a modal landing in the middle of a sentence once cancelled
/// the recording (reported 2026-10-02). Tapping it ends the dictation the
/// ordinary way — the transcript lands in the field — and the card follows.
struct PermissionPendingPill: View {
    let request: PermissionRequestInfo
    let onTap: () -> Void
    let onExpire: () -> Void

    var body: some View {
        Button(action: onTap) {
            VStack(spacing: 6) {
                HStack(spacing: 8) {
                    Image(systemName: "hand.raised.fill")
                        .foregroundStyle(Color.accentColor)
                    Text("Marlow needs your OK")
                        .font(.subheadline.weight(.semibold))
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color(.secondaryLabel))
                }
                PermissionTimeBar(request: request, onExpire: onExpire)
                    .frame(width: 120)
            }
            .padding(.horizontal, 18)
            .padding(.top, 10)
            .padding(.bottom, 9)
            .background(.regularMaterial, in: Capsule())
            .overlay(Capsule().stroke(Color.primary.opacity(0.08), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.15), radius: 10, y: 3)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Marlow needs your OK")
        .accessibilityHint("Stops dictation and shows the request")
    }
}
