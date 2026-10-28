import SwiftUI

/// One of Marlow's files, rendered (docs/tasks/ux-lane.md §3a). Pushed from
/// the Memory, Schedule and Soul panels; the You and Soul panels embed
/// `IdentityFileContent` directly instead of pushing.
struct IdentityFileView: View {
    let title: String
    let loader: IdentityFileLoader
    let kind: IdentityKind

    var body: some View {
        ScrollView {
            IdentityFileContent(loader: loader, kind: kind)
                .padding(.horizontal)
                .padding(.vertical, 12)
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// The note, the date and the text, without a scroll view of its own so a
/// panel can put other rows after it. Fetches fresh each time it appears;
/// nothing is cached on the phone (§3a).
struct IdentityFileContent: View {
    @ObservedObject var loader: IdentityFileLoader
    let kind: IdentityKind

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            switch loader.state {
            case .loading:
                ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
            case .failed(let message):
                ContentUnavailableView {
                    Label("Couldn't load", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Try again") { Task { await loader.load() } }
                }
            case .loaded(let file):
                AboutThisFile(kind: file.kind == .unknown ? kind : file.kind)
                Text("Last changed \(Date(timeIntervalSince1970: file.modifiedAt).formatted(date: .abbreviated, time: .shortened))")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                MarkdownMessageView(text: IdentityFormat.stripFrontmatter(file.text))
                    .textSelection(.enabled)
                if file.truncated {
                    Text("Showing the first 256 KB.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task { await loader.load() }
    }
}

/// Written in the app, not part of the file.
private struct AboutThisFile: View {
    let kind: IdentityKind

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("About this file")
                .font(.footnote.weight(.semibold))
            Text(IdentityFormat.aboutNote(kind))
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
    }
}
