import SwiftUI

/// A playlist from a pasted Spotify, Apple Music, Deezer or YouTube link (or a list of songs).
@available(macOS 14, *)
struct ImportSheet: View {
    @Environment(Router.self) private var router
    @Environment(LibraryModel.self) private var library
    @State private var pasted = ""
    @State private var name = ""
    @State private var status = ""
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Import Playlist").font(.title2.bold())
            Text("Paste a Spotify, Apple Music, Deezer or YouTube playlist link.")
                .font(.callout).foregroundStyle(.secondary)
            TextEditor(text: $pasted)
                .font(.body)
                .frame(height: 96)
                .scrollContentBackground(.hidden)
                .padding(6)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            TextField("Name (optional)", text: $name).textFieldStyle(.roundedBorder)
            Text(progressText).font(.callout).foregroundStyle(.secondary).frame(minHeight: 18, alignment: .leading)
            HStack {
                Text("Private? Make it public, or export a CSV at exportify.net.").font(.caption).foregroundStyle(.tertiary)
                Spacer()
                Button("Cancel") { router.showImport = false }.keyboardShortcut(.cancelAction)
                Button("Import") { run() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || pasted.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(22)
        .frame(width: 480)
    }

    private var progressText: String {
        if working, let progress = library.importProgress { return "Looked up \(progress.done) of \(progress.total)…" }
        return working ? "Reading that playlist…" : status
    }

    private func run() {
        working = true
        status = ""
        Task {
            let (playlist, message) = await library.importPlaylist(pasted, name: name)
            working = false
            status = message
            guard let playlist else { return }
            router.showImport = false
            router.selection = .playlist(playlist.id)
        }
    }
}
