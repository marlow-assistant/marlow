import SwiftUI

/// The staged photos and files above the composer, and in the share sheet:
/// one 64pt tile each, three across, so six are two rows (CMP-5). A photo is
/// its thumbnail; a file is its type icon with the name truncated in the
/// middle (the end is the part that says what it is). Each has an X.
///
/// Compiled into the app and the share extension, so a file looks the same
/// whichever way it arrived.
struct StagedAttachmentTray: View {
    let items: [StagedAttachments.Item]
    let onRemove: (UUID) -> Void

    var body: some View {
        if !items.isEmpty {
            // 64x64, three across, so six are two rows. A single row of six at
            // 44pt left a remove control too small to hit with confidence on a
            // phone (Mike, 2026-10-24); three across leaves room for both.
            LazyVGrid(
                columns: Array(repeating: GridItem(.fixed(64), spacing: 18), count: 3),
                alignment: .leading, spacing: 18
            ) {
                ForEach(items) { item in
                    tile(for: item)
                        .frame(width: 64, height: 64)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                        .overlay(alignment: .topTrailing) { removeButton(for: item) }
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func tile(for item: StagedAttachments.Item) -> some View {
        switch item {
        case .photo(_, let data):
            // A staged image that will not decode still counts toward the cap
            // and is still sent, so it still gets a tile and a remove control:
            // a gray one.
            if let preview = UIImage(data: data) {
                Image(uiImage: preview)
                    .resizable()
                    .scaledToFill()
            } else {
                Color.gray.opacity(0.25)
            }
        case .file(let file):
            VStack(spacing: 4) {
                Image(systemName: FileDisplay.symbol(forName: file.name))
                    .font(.system(size: 22))
                    .foregroundStyle(.secondary)
                Text(file.name)
                    .font(.system(size: 10))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .padding(.horizontal, 4)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.gray.opacity(0.25))
        }
    }

    private func removeButton(for item: StagedAttachments.Item) -> some View {
        Button {
            onRemove(item.id)
        } label: {
            // A 36pt target centred on the corner, as on the web: a 44pt one
            // over the tile covered most of it, so touching a photo removed
            // it. The grid's 18pt spacing keeps it off the next tile.
            Image(systemName: "xmark.circle.fill")
                .font(.system(size: 26))
                .symbolRenderingMode(.palette)
                .foregroundStyle(.white, .black.opacity(0.65))
                .frame(width: 36, height: 36)
                .contentShape(Rectangle())
        }
        .offset(x: 18, y: -18)
        .accessibilityLabel(label(for: item))
    }

    private func label(for item: StagedAttachments.Item) -> String {
        switch item {
        case .photo:
            let photos = items.filter { if case .photo = $0 { return true } else { return false } }
            let index = photos.firstIndex { $0.id == item.id } ?? 0
            return "Remove image \(index + 1) of \(photos.count)"
        case .file(let file):
            return "Remove \(file.name)"
        }
    }
}
