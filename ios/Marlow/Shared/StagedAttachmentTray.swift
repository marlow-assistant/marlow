import SwiftUI

/// The staged photos and files above the composer, and in the share sheet.
/// Photos are 64pt tiles, three across, so six are two rows (CMP-5), each with
/// an X on its corner. Files are rows under them, one per line: type icon, the
/// name, its size and an X. A 64pt tile cut a name to a few letters, and the
/// name is what says which file is about to go (Mike, 2026-11-14).
///
/// Compiled into the app and the share extension, so a file looks the same
/// whichever way it arrived.
struct StagedAttachmentTray: View {
    let items: [StagedAttachments.Item]
    let onRemove: (UUID) -> Void

    private var photos: [(id: UUID, data: Data)] {
        items.compactMap { if case .photo(let id, let data) = $0 { return (id, data) } else { return nil } }
    }

    private var files: [StagedFile] {
        items.compactMap { if case .file(let file) = $0 { return file } else { return nil } }
    }

    var body: some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                if !photos.isEmpty { photoGrid }
                ForEach(files) { fileRow($0) }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var photoGrid: some View {
        // 64x64, three across, so six are two rows. A single row of six at
        // 44pt left a remove control too small to hit with confidence on a
        // phone (Mike, 2026-10-24); three across leaves room for both.
        LazyVGrid(
            columns: Array(repeating: GridItem(.fixed(64), spacing: 18), count: 3),
            alignment: .leading, spacing: 18
        ) {
            ForEach(Array(photos.enumerated()), id: \.element.id) { index, photo in
                photoTile(photo.data)
                    .frame(width: 64, height: 64)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .overlay(alignment: .topTrailing) {
                        cornerRemoveButton(id: photo.id, label: "Remove image \(index + 1) of \(photos.count)")
                    }
            }
        }
        .padding(.bottom, files.isEmpty ? 0 : 6)
    }

    @ViewBuilder
    private func photoTile(_ data: Data) -> some View {
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
    }

    private func fileRow(_ file: StagedFile) -> some View {
        let parts = FileDisplay.stemAndExtension(file.name)
        return HStack(spacing: 10) {
            Image(systemName: FileDisplay.symbol(forName: file.name))
                .font(.system(size: 18))
                .foregroundStyle(.secondary)
                .frame(width: 24)
            // The stem gives way first, so a long name still ends in what it is.
            HStack(spacing: 0) {
                Text(parts.stem).lineLimit(1).truncationMode(.tail)
                Text(parts.ext).lineLimit(1).fixedSize()
            }
            .font(.subheadline)
            Spacer(minLength: 4)
            if let size = FileDisplay.sizeText(file.byteCount) {
                Text(size)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize()
            }
            Button {
                onRemove(file.id)
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 20))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(.secondary)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("Remove \(file.name)")
        }
        .padding(.leading, 12)
        .frame(minHeight: 44)
        .background(Color.gray.opacity(0.15), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .contain)
    }

    private func cornerRemoveButton(id: UUID, label: String) -> some View {
        Button {
            onRemove(id)
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
        .accessibilityLabel(label)
    }
}
