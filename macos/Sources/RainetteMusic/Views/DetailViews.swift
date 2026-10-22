import SwiftUI

@available(macOS 14, *)
struct DetailHeader<Actions: View>: View {
    let artworkURL: String
    var circle = false
    var symbol = "music.note"
    var artwork: AnyView?
    let eyebrow: String
    let title: String
    var subtitle: String = ""
    var subtitleAction: (() -> Void)?
    var detail: String = ""
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        HStack(alignment: .bottom, spacing: 28) {
            Group {
                if let artwork { artwork } else {
                    ArtworkView(url: artworkURL, pixels: 600, cornerRadius: 14, circle: circle, placeholderSymbol: symbol, seed: title)
                }
            }
            .frame(width: 220, height: 220)
            .shadow(color: .black.opacity(0.35), radius: 26, y: 14)
            VStack(alignment: .leading, spacing: 6) {
                Text(eyebrow.uppercased()).font(.system(size: 11, weight: .bold)).tracking(0.9).foregroundStyle(.white.opacity(0.7))
                Text(title).font(.system(size: 38, weight: .heavy)).tracking(-0.8).lineLimit(2).minimumScaleFactor(0.6)
                if !subtitle.isEmpty {
                    if let subtitleAction {
                        Button(subtitle, action: subtitleAction).buttonStyle(.plain)
                            .font(.system(size: 17, weight: .semibold)).foregroundStyle(.white.opacity(0.92))
                    } else {
                        Text(subtitle).font(.system(size: 17, weight: .semibold)).foregroundStyle(.white.opacity(0.92))
                    }
                }
                if !detail.isEmpty { Text(detail).font(.system(size: 12)).foregroundStyle(.white.opacity(0.65)) }
                HStack(spacing: 10) { actions() }.padding(.top, 12)
            }
            .foregroundStyle(.white)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, CardMetrics.pagePadding)
        .padding(.top, 36)
        .padding(.bottom, 28)
        .frame(maxWidth: .infinity)
        .background {
            ArtworkBackdrop(url: artworkURL, blur: 80, dim: 0.38)
                .mask(LinearGradient(stops: [.init(color: .black, location: 0), .init(color: .black, location: 0.7), .init(color: .clear, location: 1)],
                                     startPoint: .top, endPoint: .bottom))
                .ignoresSafeArea()
        }
        .environment(\.colorScheme, .dark)
    }
}

// MARK: - Artist

@available(macOS 14, *)
struct ArtistDetailView: View {
    let artist: Artist
    @Environment(AppModel.self) private var app
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router
    @State private var catalog: ArtistCatalog?
    @State private var showAllSongs = false

    var body: some View {
        let shown = catalog?.artist ?? artist
        ScrollView {
            VStack(alignment: .leading, spacing: 34) {
                DetailHeader(artworkURL: shown.artworkURL.isEmpty ? artist.artworkURL : shown.artworkURL, circle: true, symbol: "music.mic",
                             eyebrow: "Artist", title: shown.name, detail: Self.audience(shown.subscribers)) {
                    Button { app.play(artist: shown) } label: { Label("Play", systemImage: "play.fill") }
                        .buttonStyle(PillButtonStyle(prominent: true))
                    Button { app.play(artist: shown, shuffled: true) } label: { Label("Shuffle", systemImage: "shuffle") }
                        .buttonStyle(PillButtonStyle())
                    let following = library.isFollowing(shown)
                    Button { library.toggleFollow(shown) } label: {
                        Label(following ? "Following" : "Follow", systemImage: following ? "heart.fill" : "heart")
                            .contentTransition(.symbolEffect(.replace))
                    }
                    .buttonStyle(PillButtonStyle())
                    Button { app.startMix(["kind": "artist", "artist": shown.raw], named: shown.name) } label: { Label("Mix", systemImage: "sparkles") }
                        .buttonStyle(PillButtonStyle())
                }
                if let catalog {
                    content(catalog)
                } else {
                    HStack { Spacer(); ProgressView("Loading \(artist.name)…").controlSize(.small); Spacer() }.frame(minHeight: 200)
                }
            }
            .padding(.bottom, 28)
        }

        .navigationTitle(shown.name)
        .toolbar { LayoutToggle() }
        .task(id: artist.id) {
            if catalog == nil, let cached = library.cachedCatalog(for: artist) { catalog = cached; return }
            let loaded = await library.catalog(for: artist)
            withAnimation(.smooth(duration: 0.25)) { catalog = loaded }
        }
    }

    @ViewBuilder
    private func content(_ catalog: ArtistCatalog) -> some View {
        if !catalog.note.isEmpty && catalog.songs.isEmpty && catalog.albums.isEmpty {
            EmptyState(title: "Catalog unavailable", symbol: "exclamationmark.triangle", message: catalog.note)
        }
        if !catalog.songs.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                SectionHeader(title: "Top Songs", action: catalog.songs.count > 6 ? (label: showAllSongs ? "Show Less" : "See All", run: { withAnimation(.smooth) { showAllSongs.toggle() } }) : nil)
                LazyVStack(spacing: 2) {
                    let songs = showAllSongs ? catalog.songs : Array(catalog.songs.prefix(6))
                    ForEach(Array(songs.enumerated()), id: \.element.id) { offset, track in
                        TrackRow(track: track, list: catalog.songs, number: offset + 1)
                    }
                }
            }
            .padding(.horizontal, CardMetrics.pagePadding)
        }
        shelf("Albums", catalog.albums)
        shelf("EPs", catalog.eps)
        shelf("Singles", catalog.singles)
        if !catalog.videos.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                SectionHeader(title: "Videos").padding(.horizontal, CardMetrics.pagePadding)
                Shelf(items: catalog.videos) { TrackTile(track: $0, list: catalog.videos) }
            }
        }
        if !catalog.artist.description.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "About")
                Text(catalog.artist.description).font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(3)
                    .frame(maxWidth: 720, alignment: .leading)
            }
            .padding(.horizontal, CardMetrics.pagePadding)
        }
    }

    /// YouTube Music sends a bare "615K"; say what it counts.
    static func audience(_ value: String) -> String {
        guard !value.isEmpty else { return "" }
        return value.rangeOfCharacter(from: .letters.subtracting(CharacterSet(charactersIn: "KMBkmb"))) == nil ? "\(value) subscribers" : value
    }

    @ViewBuilder
    private func shelf(_ title: String, _ albums: [Album]) -> some View {
        if !albums.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                SectionHeader(title: title).padding(.horizontal, CardMetrics.pagePadding)
                Shelf(items: albums) { AlbumCard(album: $0) }
            }
        }
    }
}

// MARK: - Album

@available(macOS 14, *)
struct AlbumDetailView: View {
    let album: Album
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router
    @State private var loaded: (album: Album, tracks: [Track])?
    @State private var error: String?

    var body: some View {
        let shown = loaded?.album ?? album
        let tracks = loaded?.tracks ?? []
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                DetailHeader(artworkURL: shown.artworkURL.isEmpty ? album.artworkURL : shown.artworkURL, symbol: "square.stack",
                             eyebrow: loaded == nil ? album.kindLabel : (tracks.count == 1 ? "Single" : shown.kindLabel),
                             title: shown.title, subtitle: shown.artist,
                             subtitleAction: shown.artistRef.map { artist in { router.open(.artist(artist)) } },
                             detail: detailLine(shown, tracks)) {
                    Button { app.sounds.play(.play); player.play(tracks) } label: { Label("Play", systemImage: "play.fill") }
                        .buttonStyle(PillButtonStyle(prominent: true)).disabled(tracks.isEmpty)
                    Button { app.sounds.play(.play); player.shuffle(tracks) } label: { Label("Shuffle", systemImage: "shuffle") }
                        .buttonStyle(PillButtonStyle()).disabled(tracks.count < 2)
                    Menu {
                        Button { library.download(tracks, label: shown.title) } label: { Label("Download to This Mac", systemImage: "arrow.down.circle") }
                        Button { app.startMix(["kind": "album", "album": shown.raw], named: shown.title) } label: { Label("Start Mix", systemImage: "sparkles") }
                        if let artist = shown.artistRef { Button { router.open(.artist(artist)) } label: { Label("Go to Artist", systemImage: "music.mic") } }
                    } label: { Image(systemName: "ellipsis") }
                        .menuStyle(.button).buttonStyle(GlassIconButtonStyle(size: 32)).menuIndicator(.hidden).fixedSize()
                }
                Group {
                    if loaded == nil && error == nil {
                        HStack { Spacer(); ProgressView().controlSize(.small); Spacer() }.frame(minHeight: 160)
                    } else if tracks.isEmpty {
                        EmptyState(title: "No songs found", symbol: "square.stack", message: error ?? "Try searching for this album instead.")
                    } else {
                        LazyVStack(spacing: 2) {
                            ForEach(Array(tracks.enumerated()), id: \.element.id) { offset, track in
                                TrackRow(track: track, list: tracks, number: offset + 1, showsArtwork: false, showsAlbum: false,
                                         albumArtist: shown.artist)
                            }
                        }
                    }
                }
                .padding(.horizontal, CardMetrics.pagePadding)
            }
            .padding(.bottom, 28)
        }

        .navigationTitle(shown.title)
        .task(id: album.id) {
            if loaded == nil, let (cachedAlbum, tracks) = library.cachedTracks(for: album) { loaded = (cachedAlbum, tracks); return }
            let (described, tracks, failure) = await library.tracks(for: album)
            withAnimation(.smooth(duration: 0.25)) { loaded = (described, tracks); error = failure }
        }
    }

    private func detailLine(_ album: Album, _ tracks: [Track]) -> String {
        var parts: [String] = []
        if !album.year.isEmpty { parts.append(album.year) }
        if !tracks.isEmpty {
            parts.append(Format.count(tracks.count, "song"))
            parts.append(Format.totalDuration(tracks.reduce(0) { $0 + $1.duration }))
        }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Playlist

@available(macOS 14, *)
struct PlaylistDetailView: View {
    let playlist: Playlist
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router
    @State private var tracks: [Track]?
    @State private var failed = false

    var body: some View {
        let current = library.playlists.first { $0.id == playlist.id } ?? playlist
        let list = tracks ?? []
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                DetailHeader(artworkURL: library.artworkURL(for: current)?.absoluteString ?? list.first?.artworkURL ?? "",
                             artwork: AnyView(PlaylistCover(playlist: current, tracks: list)),
                             eyebrow: current.isSmart ? "Smart Playlist" : "Playlist", title: current.name,
                             subtitle: current.description,
                             detail: list.isEmpty ? "" : "\(Format.count(list.count, "song")) · \(Format.totalDuration(list.reduce(0) { $0 + $1.duration }))") {
                    Button { app.sounds.play(.play); player.play(list) } label: { Label("Play", systemImage: "play.fill") }
                        .buttonStyle(PillButtonStyle(prominent: true)).disabled(list.isEmpty)
                    Button { app.sounds.play(.play); player.shuffle(list) } label: { Label("Shuffle", systemImage: "shuffle") }
                        .buttonStyle(PillButtonStyle()).disabled(list.count < 2)
                    Menu {
                        PlaylistMenu(playlist: current)
                        Divider()
                        Button { library.download(list, label: current.name) } label: { Label("Download to This Mac", systemImage: "arrow.down.circle") }
                    } label: { Image(systemName: "ellipsis") }
                        .menuStyle(.button).buttonStyle(GlassIconButtonStyle(size: 32)).menuIndicator(.hidden).fixedSize()
                }
                Group {
                    if failed && tracks == nil {
                        EmptyState(title: "Couldn't load this playlist", symbol: "exclamationmark.triangle",
                                   message: "Check that Rainette is still running, then open it again.")
                    } else if tracks == nil {
                        HStack { Spacer(); ProgressView().controlSize(.small); Spacer() }.frame(minHeight: 160)
                    } else if list.isEmpty {
                        EmptyState(title: "This playlist is empty", symbol: "music.note.list",
                                   message: "Add songs from their ⋯ menu with Add to Playlist.")
                    } else {
                        TrackCollection(tracks: list, context: .playlist(current))
                    }
                }
                .padding(.horizontal, CardMetrics.pagePadding)
            }
            .padding(.bottom, 28)
        }

        .navigationTitle(current.name)
        .toolbar { LayoutToggle() }
        .task(id: "\(playlist.id)|\(router.playlistChanged)|\(current.trackCount)") {
            let loaded = await library.tracks(for: playlist)
            withAnimation(.smooth(duration: 0.25)) {
                if let loaded { tracks = loaded; failed = false } else { failed = true }
            }
        }
    }
}

/// A playlist without its own artwork shows a mosaic of its first four songs.
@available(macOS 14, *)
struct PlaylistCover: View {
    let playlist: Playlist
    let tracks: [Track]
    @Environment(LibraryModel.self) private var library

    var body: some View {
        var seen = Set<String>()
        let arts = tracks.map(\.artworkURL).filter { !$0.isEmpty && seen.insert($0).inserted }.prefix(4)
        if library.artworkURL(for: playlist) != nil || arts.count < 4 {
            PlaylistArtwork(playlist: playlist, cornerRadius: 14)
        } else {
            Grid(horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow { tile(arts[arts.startIndex]); tile(arts[arts.startIndex + 1]) }
                GridRow { tile(arts[arts.startIndex + 2]); tile(arts[arts.startIndex + 3]) }
            }
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
    }

    private func tile(_ url: String) -> some View {
        ArtworkView(url: url, pixels: 300, cornerRadius: 0)
    }
}
