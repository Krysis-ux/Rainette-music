import SwiftUI

// MARK: - Layout

@available(macOS 14, *)
enum CardMetrics {
    static let gridColumns = [GridItem(.adaptive(minimum: 164, maximum: 214), spacing: 22, alignment: .top)]
    static let shelfWidth: CGFloat = 176
    static let gridSpacing: CGFloat = 26
    static let pagePadding: CGFloat = 28
}

@available(macOS 14, *)
struct SectionHeader: View {
    let title: String
    var subtitle: String = ""
    var action: (label: String, run: () -> Void)?

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 20, weight: .bold)).tracking(-0.3)
                if !subtitle.isEmpty { Text(subtitle).font(.system(size: 12)).foregroundStyle(.secondary) }
            }
            Spacer()
            if let action {
                Button(action.label, action: action.run)
                    .buttonStyle(.plain)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Color.accentColor)
            }
        }
    }
}

/// A horizontal row of cards that scrolls and snaps card by card.
@available(macOS 14, *)
struct Shelf<Item: Identifiable, Card: View>: View {
    let items: [Item]
    @ViewBuilder let card: (Item) -> Card

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            LazyHStack(alignment: .top, spacing: 20) {
                ForEach(items) { item in card(item).frame(width: CardMetrics.shelfWidth) }
            }
            .scrollTargetLayout()
            .padding(.horizontal, CardMetrics.pagePadding)
            .padding(.vertical, 6)
        }
        .scrollTargetBehavior(.viewAligned)
        .scrollClipDisabled()
    }
}

@available(macOS 14, *)
struct EmptyState: View {
    let title: String
    let symbol: String
    var message: String = ""

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: symbol)
        } description: {
            if !message.isEmpty { Text(message) }
        }
        .frame(maxWidth: .infinity, minHeight: 260)
    }
}

// MARK: - Now playing indicator

@available(macOS 14, *)
struct PlayingIndicator: View {
    let playing: Bool
    var size: CGFloat = 14
    var color: Color = .accentColor

    var body: some View {
        Image(systemName: "waveform")
            .font(.system(size: size, weight: .semibold))
            .foregroundStyle(color)
            .symbolEffect(.variableColor.iterative.reversing, options: .repeating, isActive: playing)
            .accessibilityLabel(playing ? "Now playing" : "Paused")
    }
}

// MARK: - Hover play

/// The round play button that appears over artwork on hover.
@available(macOS 14, *)
struct HoverPlayButton: View {
    let isCurrent: Bool
    let showsPause: Bool
    var size: CGFloat = 40
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: isCurrent && showsPause ? "pause.fill" : "play.fill")
                .offset(x: isCurrent && showsPause ? 0 : 1)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(GlassIconButtonStyle(size: size, prominent: true))
        .help(isCurrent && showsPause ? "Pause" : "Play")
    }
}

@available(macOS 14, *)
private struct CardHover: ViewModifier {
    @Binding var hovering: Bool

    func body(content: Content) -> some View {
        content
            .onHover { inside in withAnimation(.snappy(duration: 0.2)) { hovering = inside } }
    }
}

@available(macOS 14, *)
extension View {
    func cardHover(_ hovering: Binding<Bool>) -> some View { modifier(CardHover(hovering: hovering)) }

    /// A soft lift under artwork, stronger while hovered.
    func artworkShadow(_ lifted: Bool) -> some View {
        shadow(color: .black.opacity(lifted ? 0.28 : 0.14), radius: lifted ? 18 : 8, y: lifted ? 10 : 4)
    }
}

// MARK: - Cards

@available(macOS 14, *)
struct AlbumCard: View {
    let album: Album
    @Environment(AppModel.self) private var app
    @Environment(Router.self) private var router
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ArtworkView(url: album.artworkURL, pixels: 480, cornerRadius: 10, placeholderSymbol: "square.stack", seed: album.title)
                .overlay(alignment: .bottomLeading) {
                    if hovering {
                        HoverPlayButton(isCurrent: false, showsPause: false) { app.play(album: album) }
                            .padding(10)
                            .transition(.scale(scale: 0.7).combined(with: .opacity))
                    }
                }
                .overlay(alignment: .bottomTrailing) {
                    if hovering {
                        Menu { AlbumMenu(album: album) } label: { Image(systemName: "ellipsis") }
                            .menuStyle(.button)
                            .buttonStyle(GlassIconButtonStyle(size: 28))
                            .menuIndicator(.hidden)
                            .fixedSize()
                            .padding(10)
                            .transition(.opacity)
                    }
                }
                .scaleEffect(hovering ? 1.015 : 1)
                .artworkShadow(hovering)
            VStack(alignment: .leading, spacing: 1) {
                Text(album.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                Text(subtitle).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { app.sounds.play(.tap); router.open(.album(album)) }
        .cardHover($hovering)
        .contextMenu { AlbumMenu(album: album) }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }

    private var subtitle: String {
        [album.artist.isEmpty ? album.kindLabel : album.artist, album.year].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

@available(macOS 14, *)
struct ArtistCard: View {
    let artist: Artist
    var detail: String = ""
    @Environment(AppModel.self) private var app
    @Environment(Router.self) private var router
    @State private var hovering = false

    var body: some View {
        VStack(spacing: 10) {
            ArtworkView(url: artist.artworkURL, pixels: 480, circle: true, placeholderSymbol: "music.mic", seed: artist.name)
                .overlay(alignment: .bottomTrailing) {
                    if hovering {
                        HoverPlayButton(isCurrent: false, showsPause: false, size: 36) { app.play(artist: artist) }
                            .padding(6)
                            .transition(.scale(scale: 0.7).combined(with: .opacity))
                    }
                }
                .scaleEffect(hovering ? 1.02 : 1)
                .artworkShadow(hovering)
            VStack(spacing: 1) {
                Text(artist.name).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                let line = detail.isEmpty ? artist.subscribers : detail
                if !line.isEmpty { Text(line).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1) }
            }
            .multilineTextAlignment(.center)
        }
        .contentShape(Rectangle())
        .onTapGesture { app.sounds.play(.tap); router.open(.artist(artist)) }
        .cardHover($hovering)
        .contextMenu { ArtistMenu(artist: artist) }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }
}

@available(macOS 14, *)
struct PlaylistCard: View {
    let playlist: Playlist
    @Environment(AppModel.self) private var app
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            PlaylistArtwork(playlist: playlist, cornerRadius: 10)
                .overlay(alignment: .bottomLeading) {
                    if hovering {
                        HoverPlayButton(isCurrent: false, showsPause: false) { app.play(playlist: playlist) }
                            .padding(10)
                            .transition(.scale(scale: 0.7).combined(with: .opacity))
                    }
                }
                .scaleEffect(hovering ? 1.015 : 1)
                .artworkShadow(hovering)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(playlist.name).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    if playlist.isPinned { Image(systemName: "pin.fill").font(.system(size: 9)).foregroundStyle(.secondary) }
                }
                Text(playlist.isSmart ? "Smart Playlist" : Format.count(playlist.trackCount, "song"))
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { app.sounds.play(.tap); router.open(.playlist(playlist)) }
        .cardHover($hovering)
        .contextMenu { PlaylistMenu(playlist: playlist) }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }
}

/// A playlist's own artwork, or a gradient tile with its glyph.
@available(macOS 14, *)
struct PlaylistArtwork: View {
    let playlist: Playlist
    var cornerRadius: CGFloat = 10
    @Environment(LibraryModel.self) private var library

    var body: some View {
        if let url = library.artworkURL(for: playlist) {
            ArtworkView(url: url.absoluteString, cornerRadius: cornerRadius, placeholderSymbol: "music.note.list", seed: playlist.name)
        } else {
            ArtworkView(url: "", cornerRadius: cornerRadius,
                        placeholderSymbol: playlist.isSmart ? "gearshape.2" : "music.note.list", seed: playlist.name)
        }
    }
}

/// A song as a grid tile: big artwork, hover play, the playing indicator.
@available(macOS 14, *)
struct TrackTile: View {
    let track: Track
    let list: [Track]
    var context: TrackContext = .standard
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @State private var hovering = false

    var body: some View {
        let current = player.isCurrent(track)
        VStack(alignment: .leading, spacing: 8) {
            ArtworkView(url: track.artworkURL, pixels: 480, cornerRadius: 10, seed: track.title)
                .overlay {
                    if current && !hovering {
                        RoundedRectangle(cornerRadius: 10, style: .continuous).fill(.black.opacity(0.42))
                        PlayingIndicator(playing: player.isPlaying, size: 30, color: .white)
                            .shadow(color: .black.opacity(0.3), radius: 6)
                    }
                }
                .overlay(alignment: .bottomLeading) {
                    if hovering {
                        HoverPlayButton(isCurrent: current, showsPause: player.showsPause) { press() }
                            .padding(10)
                            .transition(.scale(scale: 0.7).combined(with: .opacity))
                    }
                }
                .overlay(alignment: .bottomTrailing) {
                    if hovering {
                        Menu { TrackMenu(track: track, list: list, context: context) } label: { Image(systemName: "ellipsis") }
                            .menuStyle(.button)
                            .buttonStyle(GlassIconButtonStyle(size: 28))
                            .menuIndicator(.hidden)
                            .fixedSize()
                            .padding(10)
                            .transition(.opacity)
                    }
                }
                .scaleEffect(hovering ? 1.015 : 1)
                .artworkShadow(hovering)
            VStack(alignment: .leading, spacing: 1) {
                Text(track.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    .foregroundStyle(current ? Color.accentColor : .primary)
                Text(track.artist).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { press() }
        .cardHover($hovering)
        .contextMenu { TrackMenu(track: track, list: list, context: context) }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { press() }
    }

    private func press() {
        app.sounds.play(player.isCurrent(track) && player.showsPause ? .pause : .play)
        player.playOrToggle(track, in: list)
    }
}

/// A song as a list row. Double-click plays; hover reveals play and More.
@available(macOS 14, *)
struct TrackRow: View {
    let track: Track
    let list: [Track]
    var number: Int?
    var showsArtwork = true
    var showsAlbum = true
    /// On an album page, the album's artist goes without saying.
    var albumArtist = ""
    var context: TrackContext = .standard
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(Router.self) private var router
    @State private var hovering = false

    var body: some View {
        let current = player.isCurrent(track)
        HStack(spacing: 12) {
            leading(current: current)
            VStack(alignment: .leading, spacing: 2) {
                Text(track.title)
                    .font(.system(size: 13, weight: current ? .semibold : .regular))
                    .foregroundStyle(current ? Color.accentColor : .primary)
                    .lineLimit(1)
                if !track.artist.isEmpty && track.artist.caseInsensitiveCompare(albumArtist) != .orderedSame {
                    Text(track.artist).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if showsAlbum, !track.albumTitle.isEmpty {
                Button(track.albumTitle) { if let album = track.albumRef { router.open(.album(album)) } }
                    .buttonStyle(.plain)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(maxWidth: 220, alignment: .leading)
            }
            if track.isLocal {
                Image(systemName: "internaldrive").font(.system(size: 11)).foregroundStyle(.tertiary).help("On this Mac")
            }
            Menu { TrackMenu(track: track, list: list, context: context) } label: {
                Image(systemName: "ellipsis").frame(width: 24, height: 24).contentShape(Rectangle())
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .menuIndicator(.hidden)
            .fixedSize()
            .opacity(hovering ? 1 : 0)
            Text(Format.time(track.duration))
                .font(.system(size: 12).monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .trailing)
        }
        .padding(.horizontal, 10)
        .frame(height: showsArtwork ? 54 : 42)
        .background {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(hovering ? Color.primary.opacity(0.06) : (current ? Color.accentColor.opacity(0.08) : .clear))
        }
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { press() }
        .onHover { hovering = $0 }
        .contextMenu { TrackMenu(track: track, list: list, context: context) }
        .accessibilityElement(children: .combine)
        .accessibilityAction { press() }
    }

    @ViewBuilder
    private func leading(current: Bool) -> some View {
        if showsArtwork {
            ArtworkView(url: track.artworkURL, pixels: 120, cornerRadius: 5, seed: track.title)
                .frame(width: 40, height: 40)
                .overlay {
                    if hovering || current {
                        RoundedRectangle(cornerRadius: 5, style: .continuous).fill(.black.opacity(0.4))
                        playGlyph(current: current).foregroundStyle(.white)
                    }
                }
        } else {
            ZStack {
                if hovering || current { playGlyph(current: current) }
                else if let number { Text("\(number)").font(.system(size: 13).monospacedDigit()).foregroundStyle(.secondary) }
            }
            .frame(width: 26)
        }
    }

    @ViewBuilder
    private func playGlyph(current: Bool) -> some View {
        if current && !hovering {
            PlayingIndicator(playing: player.isPlaying, size: 13, color: showsArtwork ? .white : .accentColor)
        } else {
            Button { press() } label: {
                Image(systemName: current && player.showsPause ? "pause.fill" : "play.fill").font(.system(size: 13))
            }
            .buttonStyle(.plain)
        }
    }

    private func press() {
        app.sounds.play(player.isCurrent(track) && player.showsPause ? .pause : .play)
        player.playOrToggle(track, in: list)
    }
}

/// Tracks in the chosen layout: tiles in the grid, rows in the list.
@available(macOS 14, *)
struct TrackCollection: View {
    let tracks: [Track]
    var context: TrackContext = .standard
    var forceList = false
    @AppStorage(Preference.layout) private var layout = "grid"

    var body: some View {
        if layout == "grid" && !forceList {
            LazyVGrid(columns: CardMetrics.gridColumns, spacing: CardMetrics.gridSpacing) {
                ForEach(tracks) { track in TrackTile(track: track, list: tracks, context: context) }
            }
        } else {
            LazyVStack(spacing: 2) {
                ForEach(tracks) { track in TrackRow(track: track, list: tracks, context: context) }
            }
        }
    }
}

// MARK: - Menus

/// Where a track is shown, for the actions that only make sense there.
enum TrackContext: Equatable {
    case standard
    case recents
    case playlist(Playlist)
    case queue(Int)
}

@available(macOS 14, *)
struct TrackMenu: View {
    let track: Track
    let list: [Track]
    var context: TrackContext = .standard
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router

    var body: some View {
        Button { player.playOrToggle(track, in: list) } label: { Label("Play", systemImage: "play") }
        Button { player.playNext(track); app.sounds.play(.confirm) } label: { Label("Play Next", systemImage: "text.line.first.and.arrowtriangle.forward") }
        Button { player.addToQueue(track); app.sounds.play(.confirm) } label: { Label("Add to Queue", systemImage: "text.badge.plus") }
        Menu {
            Button { router.playlistPrompt = .create(adding: track) } label: { Label("New Playlist…", systemImage: "plus") }
            let manual = library.sortedPlaylists.filter { !$0.isSmart }
            if !manual.isEmpty { Divider() }
            ForEach(manual) { playlist in
                Button(playlist.name) { Task { await library.add(track, to: playlist) } }
            }
        } label: { Label("Add to Playlist", systemImage: "music.note.list") }
        Divider()
        if let artist = track.artistRef {
            Button { router.open(.artist(artist)) } label: { Label("Go to Artist", systemImage: "music.mic") }
        }
        if let album = track.albumRef, !album.albumID.isEmpty {
            Button { router.open(.album(album)) } label: { Label("Go to Album", systemImage: "square.stack") }
        }
        Button { app.startMix(["kind": "track", "track": track.raw], named: track.title) } label: { Label("Start Mix", systemImage: "sparkles") }
        if !track.isLocal {
            Button { library.download([track], label: track.title) } label: { Label("Download to This Mac", systemImage: "arrow.down.circle") }
        }
        switch context {
        case .recents:
            Divider()
            Button(role: .destructive) { library.forget(track) } label: { Label("Remove from Recents", systemImage: "clock.badge.xmark") }
        case .playlist(let playlist) where !playlist.isSmart:
            Divider()
            Button(role: .destructive) {
                Task { if await library.remove(track, from: playlist) { router.playlistChanged &+= 1 } }
            } label: { Label("Remove from Playlist", systemImage: "minus.circle") }
        case .queue(let index):
            Divider()
            Button(role: .destructive) { player.removeFromQueue(index) } label: { Label("Remove from Queue", systemImage: "minus.circle") }
        default:
            EmptyView()
        }
    }
}

@available(macOS 14, *)
struct AlbumMenu: View {
    let album: Album
    @Environment(AppModel.self) private var app
    @Environment(Router.self) private var router

    var body: some View {
        Button { app.play(album: album) } label: { Label("Play", systemImage: "play") }
        Button { app.play(album: album, shuffled: true) } label: { Label("Shuffle", systemImage: "shuffle") }
        Divider()
        Button { router.open(.album(album)) } label: { Label("Open \(album.kindLabel)", systemImage: "square.stack") }
        if let artist = album.artistRef {
            Button { router.open(.artist(artist)) } label: { Label("Go to Artist", systemImage: "music.mic") }
        }
        Button { app.startMix(["kind": "album", "album": album.raw], named: album.title) } label: { Label("Start Mix", systemImage: "sparkles") }
    }
}

@available(macOS 14, *)
struct ArtistMenu: View {
    let artist: Artist
    @Environment(AppModel.self) private var app
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router

    var body: some View {
        Button { app.play(artist: artist) } label: { Label("Play", systemImage: "play") }
        Button { app.play(artist: artist, shuffled: true) } label: { Label("Shuffle", systemImage: "shuffle") }
        Divider()
        Button { router.open(.artist(artist)) } label: { Label("Open Artist", systemImage: "music.mic") }
        Button { library.toggleFollow(artist) } label: {
            library.isFollowing(artist) ? Label("Unfollow", systemImage: "heart.slash") : Label("Follow", systemImage: "heart")
        }
        Button { app.startMix(["kind": "artist", "artist": artist.raw], named: artist.name) } label: { Label("Start Mix", systemImage: "sparkles") }
    }
}

@available(macOS 14, *)
struct PlaylistMenu: View {
    let playlist: Playlist
    @Environment(AppModel.self) private var app
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router

    var body: some View {
        Button { app.play(playlist: playlist) } label: { Label("Play", systemImage: "play") }
        Button { app.play(playlist: playlist, shuffled: true) } label: { Label("Shuffle", systemImage: "shuffle") }
        Divider()
        if !playlist.isSmart {
            Button { router.playlistPrompt = .rename(playlist) } label: { Label("Rename…", systemImage: "pencil") }
        }
        Button { library.setPinned(playlist, !playlist.isPinned) } label: {
            playlist.isPinned ? Label("Unpin", systemImage: "pin.slash") : Label("Pin", systemImage: "pin")
        }
        Divider()
        Button(role: .destructive) { router.confirmDelete = playlist } label: { Label("Delete Playlist…", systemImage: "trash") }
    }
}
