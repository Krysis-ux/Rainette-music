import SwiftUI

@available(macOS 14, *)
struct SearchView: View {
    enum Scope: String, CaseIterable { case all = "All", songs = "Songs", artists = "Artists", albums = "Albums" }

    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router
    @State private var results = SearchResults()
    @State private var searchedFor = ""
    @State private var loading = false
    @State private var scope: Scope = .all

    private var query: String { router.searchText.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 30) {
                if query.isEmpty {
                    idle
                } else if loading && results.isEmpty {
                    searching
                } else if results.isEmpty && searchedFor == query {
                    EmptyState(title: "No results for “\(query)”", symbol: "magnifyingglass",
                               message: results.note.isEmpty ? "Check the spelling, or try an artist or album name." : results.note)
                } else {
                    resultsView
                }
            }
            .padding(.vertical, 24)
        }
        .navigationTitle(query.isEmpty ? "Search" : "Results")
        .toolbar {
            if !query.isEmpty {
                ToolbarItem(placement: .principal) {
                    Picker("Show", selection: $scope) {
                        ForEach(Scope.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 300)
                }
            }
            LayoutToggle()
        }
        .task(id: query) { await search() }
    }

    /// Debounced: typing a word sends one search, not one per letter.
    private func search() async {
        guard !query.isEmpty else { results = SearchResults(); searchedFor = ""; return }
        try? await Task.sleep(nanoseconds: 320_000_000)
        guard !Task.isCancelled else { return }
        let asked = query
        loading = true
        let found = await library.search(asked)
        guard !Task.isCancelled, asked == query else { return }
        withAnimation(.smooth(duration: 0.25)) {
            results = found
            searchedFor = asked
            loading = false
        }
    }

    private var idle: some View {
        let artists = recentArtists
        return VStack(alignment: .leading, spacing: 16) {
            if artists.isEmpty {
                EmptyState(title: "Search the catalog", symbol: "magnifyingglass",
                           message: "Type in the search field to find songs, artists and albums.")
            } else {
                SectionHeader(title: "Recent Artists", subtitle: "From your listening history")
                    .padding(.horizontal, CardMetrics.pagePadding)
                Shelf(items: artists) { ArtistCard(artist: $0) }
            }
        }
    }

    private var recentArtists: [Artist] {
        var seen = Set<String>()
        return library.recent.compactMap(\.artistRef).filter { seen.insert($0.name.lowercased()).inserted }.prefix(12).map { $0 }
    }

    private var searching: some View {
        HStack(spacing: 10) {
            ProgressView().controlSize(.small)
            Text("Searching…").foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, minHeight: 240)
    }

    @ViewBuilder
    private var resultsView: some View {
        if scope == .all, let top = topResult {
            VStack(alignment: .leading, spacing: 14) {
                SectionHeader(title: "Top Result")
                HStack(alignment: .top, spacing: 26) {
                    top.frame(width: 300)
                    if !results.songs.isEmpty {
                        VStack(alignment: .leading, spacing: 2) {
                            let songs = Array(results.songs.prefix(4))
                            ForEach(songs) { TrackRow(track: $0, list: results.songs, showsAlbum: false) }
                        }
                    }
                }
            }
            .padding(.horizontal, CardMetrics.pagePadding)
        }
        if (scope == .all || scope == .songs), !results.songs.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                SectionHeader(title: "Songs", subtitle: scope == .all ? "" : Format.count(results.songs.count, "result"),
                              action: scope == .all && results.songs.count > 8 ? (label: "See All", run: { scope = .songs }) : nil)
                TrackCollection(tracks: scope == .all ? Array(results.songs.prefix(8)) : results.songs)
            }
            .padding(.horizontal, CardMetrics.pagePadding)
        }
        if (scope == .all || scope == .artists), !results.artists.isEmpty {
            collection("Artists", results.artists, shelf: scope == .all) { ArtistCard(artist: $0) }
        }
        if (scope == .all || scope == .albums), !results.albums.isEmpty {
            collection("Albums", results.albums, shelf: scope == .all) { AlbumCard(album: $0) }
        }
    }

    private func collection<Item: Identifiable, Card: View>(_ title: String, _ items: [Item], shelf: Bool,
                                                            @ViewBuilder card: @escaping (Item) -> Card) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: title).padding(.horizontal, CardMetrics.pagePadding)
            if shelf {
                Shelf(items: items, card: card)
            } else {
                LazyVGrid(columns: CardMetrics.gridColumns, spacing: CardMetrics.gridSpacing) {
                    ForEach(items) { card($0) }
                }
                .padding(.horizontal, CardMetrics.pagePadding)
            }
        }
    }

    /// An artist whose name is what was typed beats the first song.
    private var topResult: AnyView? {
        if let artist = results.artists.first, artist.name.localizedCaseInsensitiveCompare(query) == .orderedSame {
            return AnyView(TopResultCard(artist: artist))
        }
        if let song = results.songs.first { return AnyView(TopResultCard(track: song, list: results.songs)) }
        if let artist = results.artists.first { return AnyView(TopResultCard(artist: artist)) }
        if let album = results.albums.first { return AnyView(TopResultCard(album: album)) }
        return nil
    }
}

@available(macOS 14, *)
private struct TopResultCard: View {
    var track: Track?
    var list: [Track] = []
    var artist: Artist?
    var album: Album?

    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(Router.self) private var router
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ArtworkView(url: artworkURL, pixels: 300, cornerRadius: 10, circle: artist != nil,
                        placeholderSymbol: artist != nil ? "music.mic" : "music.note", seed: title)
                .frame(width: 96, height: 96)
                .shadow(color: .black.opacity(0.25), radius: 12, y: 6)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 22, weight: .bold)).lineLimit(2)
                Text(kind).font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, minHeight: 210, alignment: .topLeading)
        .glassSurface(in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(alignment: .bottomTrailing) {
            HoverPlayButton(isCurrent: track.map(player.isCurrent) ?? false, showsPause: player.showsPause, size: 46) { play() }
                .padding(16)
                .opacity(hovering ? 1 : 0)
                .scaleEffect(hovering ? 1 : 0.8)
        }
        .contentShape(Rectangle())
        .onTapGesture { open() }
        .cardHover($hovering)
    }

    private var title: String { track?.title ?? artist?.name ?? album?.title ?? "" }
    private var artworkURL: String { track?.artworkURL ?? artist?.artworkURL ?? album?.artworkURL ?? "" }
    private var kind: String {
        if let track { return "Song · \(track.artist)" }
        if artist != nil { return "Artist" }
        if let album { return "\(album.kindLabel) · \(album.artist)" }
        return ""
    }

    private func play() {
        if let track { app.sounds.play(.play); player.playOrToggle(track, in: list) }
        else if let artist { app.play(artist: artist) }
        else if let album { app.play(album: album) }
    }

    private func open() {
        app.sounds.play(.tap)
        if let artist { router.open(.artist(artist)) }
        else if let album { router.open(.album(album)) }
        else if let track { player.playOrToggle(track, in: list) }
    }
}
