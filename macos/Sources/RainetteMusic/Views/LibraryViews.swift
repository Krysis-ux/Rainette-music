import AppKit
import SwiftUI

/// An artist or album as a list row, for when the list layout is chosen.
@available(macOS 14, *)
struct EntityRow: View {
    let title: String
    let subtitle: String
    let artworkURL: String
    var circle = false
    var symbol = "square.stack"
    let play: () -> Void
    let open: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 12) {
            ArtworkView(url: artworkURL, pixels: 120, cornerRadius: 6, circle: circle, placeholderSymbol: symbol, seed: title)
                .frame(width: 44, height: 44)
                .overlay {
                    if hovering {
                        Button(action: play) { Image(systemName: "play.fill").font(.system(size: 14)).foregroundStyle(.white) }
                            .buttonStyle(.plain)
                            .frame(width: 44, height: 44)
                            .background(.black.opacity(0.4), in: circle ? AnyShape(Circle()) : AnyShape(RoundedRectangle(cornerRadius: 6)))
                    }
                }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .medium)).lineLimit(1)
                if !subtitle.isEmpty { Text(subtitle).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1) }
            }
            Spacer()
            Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 10)
        .frame(height: 58)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(hovering ? Color.primary.opacity(0.06) : .clear))
        .contentShape(Rectangle())
        .onTapGesture(perform: open)
        .onHover { hovering = $0 }
    }
}

@available(macOS 14, *)
struct PageHeader: View {
    let title: String
    var subtitle: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 30, weight: .bold)).tracking(-0.5)
            if !subtitle.isEmpty { Text(subtitle).font(.system(size: 13)).foregroundStyle(.secondary) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A rounded filter field for narrowing what is already on screen.
@available(macOS 14, *)
struct FilterField: View {
    @Binding var text: String
    var prompt = "Filter"

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "line.3.horizontal.decrease").foregroundStyle(.secondary)
            TextField(prompt, text: $text).textFieldStyle(.plain)
            if !text.isEmpty {
                Button { text = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary) }
                    .buttonStyle(.plain)
            }
        }
        .font(.system(size: 13))
        .padding(.horizontal, 10)
        .frame(width: 220, height: 28)
        .glassSurface(in: Capsule())
    }
}

// MARK: - Recently played

@available(macOS 14, *)
struct RecentsView: View {
    enum Mode: String, CaseIterable { case songs = "Songs", artists = "Artists", albums = "Albums" }
    @Environment(AppModel.self) private var app
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router
    @State private var mode: Mode = .songs
    @AppStorage(Preference.layout) private var layout = "grid"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(alignment: .bottom) {
                    PageHeader(title: "Recently Played", subtitle: Format.count(library.recent.count, "song"))
                    if !library.recent.isEmpty {
                        Button { app.sounds.play(.play); app.player.play(library.recent) } label: { Label("Play", systemImage: "play.fill") }
                            .buttonStyle(PillButtonStyle(prominent: true))
                        Button { app.sounds.play(.play); app.player.shuffle(library.recent) } label: { Label("Shuffle", systemImage: "shuffle") }
                            .buttonStyle(PillButtonStyle())
                    }
                }
                if library.recent.isEmpty {
                    EmptyState(title: "Nothing played yet", symbol: "clock", message: "Songs you play appear here.")
                } else {
                    switch mode {
                    case .songs: TrackCollection(tracks: library.recent, context: .recents)
                    case .artists: ArtistGrid(artists: uniqueArtists)
                    case .albums: AlbumGrid(albums: uniqueAlbums)
                    }
                }
            }
            .padding(CardMetrics.pagePadding)
        }
        .navigationTitle("Recently Played")
        .toolbar {
            ToolbarItem(placement: .principal) {
                Picker("Show", selection: $mode) { ForEach(Mode.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                    .pickerStyle(.segmented).frame(width: 240)
            }
            LayoutToggle()
        }
        .onAppear { library.refreshRecent() }
    }

    private var uniqueArtists: [Artist] {
        var seen = Set<String>()
        return library.recent.compactMap(\.artistRef).filter { seen.insert($0.name.lowercased()).inserted }
    }

    private var uniqueAlbums: [Album] {
        var seen = Set<String>()
        return library.recent.compactMap(\.albumRef).filter { !$0.albumID.isEmpty && seen.insert($0.id).inserted }
    }
}

@available(macOS 14, *)
struct ArtistGrid: View {
    let artists: [Artist]
    @Environment(AppModel.self) private var app
    @Environment(Router.self) private var router
    @AppStorage(Preference.layout) private var layout = "grid"

    var body: some View {
        if layout == "grid" {
            LazyVGrid(columns: CardMetrics.gridColumns, spacing: CardMetrics.gridSpacing) {
                ForEach(artists) { artist in
                    ArtistCard(artist: artist, detail: artist.trackCount > 0 ? Format.count(artist.trackCount, "song") : "")
                }
            }
        } else {
            LazyVStack(spacing: 2) {
                ForEach(artists) { artist in
                    EntityRow(title: artist.name, subtitle: artist.trackCount > 0 ? Format.count(artist.trackCount, "song") : artist.subscribers,
                              artworkURL: artist.artworkURL, circle: true, symbol: "music.mic",
                              play: { app.play(artist: artist) }, open: { router.open(.artist(artist)) })
                        .contextMenu { ArtistMenu(artist: artist) }
                }
            }
        }
    }
}

@available(macOS 14, *)
struct AlbumGrid: View {
    let albums: [Album]
    @Environment(AppModel.self) private var app
    @Environment(Router.self) private var router
    @AppStorage(Preference.layout) private var layout = "grid"

    var body: some View {
        if layout == "grid" {
            LazyVGrid(columns: CardMetrics.gridColumns, spacing: CardMetrics.gridSpacing) {
                ForEach(albums) { AlbumCard(album: $0) }
            }
        } else {
            LazyVStack(spacing: 2) {
                ForEach(albums) { album in
                    EntityRow(title: album.title, subtitle: [album.artist, album.year].filter { !$0.isEmpty }.joined(separator: " · "),
                              artworkURL: album.artworkURL, play: { app.play(album: album) }, open: { router.open(.album(album)) })
                        .contextMenu { AlbumMenu(album: album) }
                }
            }
        }
    }
}

// MARK: - Songs

@available(macOS 14, *)
struct SongsView: View {
    enum Sort: String, CaseIterable { case added = "Recently Added", title = "Title", artist = "Artist", played = "Most Played" }
    @Environment(AppModel.self) private var app
    @Environment(LibraryModel.self) private var library
    @AppStorage("songsSort") private var sort: Sort = .added
    @State private var filter = ""

    var body: some View {
        let songs = sorted
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(alignment: .bottom) {
                    PageHeader(title: "Songs", subtitle: Format.count(library.tracks.count, "song"))
                    if !songs.isEmpty {
                        Button { app.sounds.play(.play); app.player.play(songs) } label: { Label("Play", systemImage: "play.fill") }
                            .buttonStyle(PillButtonStyle(prominent: true))
                        Button { app.sounds.play(.play); app.player.shuffle(songs) } label: { Label("Shuffle", systemImage: "shuffle") }
                            .buttonStyle(PillButtonStyle())
                    }
                }
                if library.tracks.isEmpty {
                    EmptyState(title: library.loadedOnce ? "No songs yet" : "Loading your library…", symbol: "music.note",
                               message: library.loadedOnce ? "Search and play songs to build your library." : "")
                } else if songs.isEmpty {
                    EmptyState(title: "No matches", symbol: "line.3.horizontal.decrease", message: "Try a different filter.")
                } else {
                    TrackCollection(tracks: songs)
                }
            }
            .padding(CardMetrics.pagePadding)
        }
        .navigationTitle("Songs")
        .toolbar {
            ToolbarItem(placement: .principal) { FilterField(text: $filter, prompt: "Filter songs") }
            ToolbarItem {
                Menu {
                    Picker("Sort By", selection: $sort) { ForEach(Sort.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                        .pickerStyle(.inline)
                } label: { Label("Sort", systemImage: "arrow.up.arrow.down") }
            }
            LayoutToggle()
        }
    }

    private var sorted: [Track] {
        let needle = filter.trimmingCharacters(in: .whitespaces)
        var list = needle.isEmpty ? library.tracks : library.tracks.filter {
            $0.title.localizedCaseInsensitiveContains(needle) || $0.artist.localizedCaseInsensitiveContains(needle)
        }
        switch sort {
        case .added: list.sort { $0.raw["added_at"].text > $1.raw["added_at"].text }
        case .title: list.sort { $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending }
        case .artist: list.sort { $0.artist.localizedCaseInsensitiveCompare($1.artist) == .orderedAscending }
        case .played: list.sort { $0.playCount > $1.playCount }
        }
        return list
    }
}

// MARK: - Artists & albums

@available(macOS 14, *)
struct ArtistsView: View {
    enum Show: String, CaseIterable { case all = "All", following = "Following" }
    @Environment(LibraryModel.self) private var library
    @State private var show: Show = .all
    @State private var filter = ""

    var body: some View {
        let artists = (show == .all ? library.artists : library.followed).filter {
            filter.isEmpty || $0.name.localizedCaseInsensitiveContains(filter)
        }
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                PageHeader(title: show == .all ? "Artists" : "Following", subtitle: Format.count(artists.count, "artist"))
                if artists.isEmpty {
                    EmptyState(title: show == .all ? "No artists yet" : "You're not following anyone",
                               symbol: show == .all ? "music.mic" : "heart",
                               message: show == .all ? "Artists you play show up here." : "Follow an artist from their page to keep them here.")
                } else {
                    ArtistGrid(artists: artists)
                }
            }
            .padding(CardMetrics.pagePadding)
        }
        .navigationTitle("Artists")
        .toolbar {
            ToolbarItem(placement: .principal) {
                Picker("Show", selection: $show) { ForEach(Show.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                    .pickerStyle(.segmented).frame(width: 180)
            }
            ToolbarItem { FilterField(text: $filter, prompt: "Filter artists") }
            LayoutToggle()
        }
    }
}

@available(macOS 14, *)
struct AlbumsView: View {
    @Environment(LibraryModel.self) private var library
    @State private var filter = ""

    var body: some View {
        let albums = library.albums.filter {
            filter.isEmpty || $0.title.localizedCaseInsensitiveContains(filter) || $0.artist.localizedCaseInsensitiveContains(filter)
        }
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                PageHeader(title: "Albums", subtitle: Format.count(albums.count, "album"))
                if albums.isEmpty {
                    EmptyState(title: "No albums yet", symbol: "square.stack", message: "Albums of the songs you play show up here.")
                } else {
                    AlbumGrid(albums: albums)
                }
            }
            .padding(CardMetrics.pagePadding)
        }
        .navigationTitle("Albums")
        .toolbar {
            ToolbarItem(placement: .principal) { FilterField(text: $filter, prompt: "Filter albums") }
            LayoutToggle()
        }
    }
}

// MARK: - On this Mac

@available(macOS 14, *)
struct DownloadsView: View {
    @Environment(LibraryModel.self) private var library

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .bottom) {
                    PageHeader(title: "On This Mac", subtitle: summary)
                    Button { chooseFolder() } label: { Label("Add Folder…", systemImage: "folder.badge.plus") }
                        .buttonStyle(PillButtonStyle(prominent: true))
                    Button { library.rescan() } label: {
                        Label(library.local.scanning ? "Scanning…" : "Rescan", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(PillButtonStyle())
                    .disabled(library.local.scanning)
                }
                if !library.local.roots.isEmpty {
                    VStack(spacing: 0) {
                        ForEach(library.local.roots, id: \.path) { root in
                            HStack(spacing: 12) {
                                Image(systemName: "folder.fill").foregroundStyle(Color.accentColor).font(.system(size: 18))
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(root.path).font(.system(size: 13, weight: .medium)).lineLimit(1).truncationMode(.middle)
                                    Text(root.error.isEmpty ? Format.count(root.trackCount, "song") : root.error)
                                        .font(.system(size: 12)).foregroundStyle(root.error.isEmpty ? Color.secondary : .orange)
                                }
                                Spacer()
                                Button { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: root.path) } label: { Image(systemName: "arrow.up.forward.app") }
                                    .buttonStyle(.borderless).help("Show in Finder")
                                Button(role: .destructive) { library.removeFolder(root.path) } label: { Image(systemName: "minus.circle") }
                                    .buttonStyle(.borderless).help("Stop watching this folder")
                            }
                            .padding(.horizontal, 16).padding(.vertical, 12)
                            if root != library.local.roots.last { Divider().padding(.leading, 46) }
                        }
                    }
                    .glassSurface(in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
                if library.localTracks.isEmpty {
                    EmptyState(title: "No music on this Mac yet", symbol: "internaldrive",
                               message: "Download songs from their ⋯ menu, or add a folder of your own music.")
                } else {
                    TrackCollection(tracks: library.localTracks)
                }
            }
            .padding(CardMetrics.pagePadding)
        }
        .navigationTitle("On This Mac")
        .toolbar { LayoutToggle() }
        .onAppear { library.refreshLocal() }
    }

    private var summary: String {
        let size = ByteCountFormatter.string(fromByteCount: Int64(library.local.bytes), countStyle: .file)
        return "\(Format.count(library.local.tracks, "song")) · \(size)"
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Add Folder"
        panel.message = "Choose a folder of music for Rainette to watch."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await library.addFolder(url.path) }
    }
}
