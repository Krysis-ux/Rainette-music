import SwiftUI

@available(macOS 14, *)
struct HomeView: View {
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 34) {
                header
                if !library.catalogNote.isEmpty {
                    Label(library.catalogNote, systemImage: "exclamationmark.triangle")
                        .font(.system(size: 12))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, CardMetrics.pagePadding)
                }
                if let track = player.current {
                    ContinueCard(track: track).padding(.horizontal, CardMetrics.pagePadding)
                } else if let session = library.lastSession, !session.tracks.isEmpty {
                    ResumeCard(tracks: session.tracks, index: session.index).padding(.horizontal, CardMetrics.pagePadding)
                }
                if !library.recent.isEmpty {
                    shelfSection("Recently Played", action: ("See All", { router.selection = .recents })) {
                        let tracks = Array(library.recent.prefix(16))
                        Shelf(items: tracks) { TrackTile(track: $0, list: tracks, context: .recents) }
                    }
                }
                if !library.playlists.isEmpty {
                    shelfSection("Your Playlists") {
                        Shelf(items: library.sortedPlaylists) { PlaylistCard(playlist: $0) }
                    }
                }
                if !library.topArtists.isEmpty {
                    shelfSection("Your Top Artists") {
                        Shelf(items: library.topArtists) { artist in
                            ArtistCard(artist: artist, detail: Format.count(artist.playCount, "play"))
                        }
                    }
                }
                if !library.followed.isEmpty {
                    shelfSection("Artists You Follow") {
                        Shelf(items: library.followed) { ArtistCard(artist: $0) }
                    }
                }
                if library.recent.isEmpty && library.playlists.isEmpty && !player.hasTrack {
                    EmptyState(title: "Your music starts here", symbol: "music.note.house",
                               message: "Search for a song, artist or album and press play. What you listen to builds this page.")
                }
            }
            .padding(.vertical, 24)
        }
        .navigationTitle("Home")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(Date.now.formatted(.dateTime.weekday(.wide).month(.wide).day()).uppercased())
                .font(.system(size: 11, weight: .semibold))
                .tracking(0.6)
                .foregroundStyle(.secondary)
            Text(greeting).font(.system(size: 34, weight: .bold)).tracking(-0.6)
        }
        .padding(.horizontal, CardMetrics.pagePadding)
    }

    private var greeting: String {
        switch Calendar.current.component(.hour, from: .now) {
        case 5..<12: return "Good morning"
        case 12..<17: return "Good afternoon"
        default: return "Good evening"
        }
    }

    private func shelfSection<Content: View>(_ title: String, action: (String, () -> Void)? = nil,
                                             @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: title, action: action.map { (label: $0.0, run: $0.1) })
                .padding(.horizontal, CardMetrics.pagePadding)
            content()
        }
    }
}

/// The song in the player, big, with its colours behind it.
@available(macOS 14, *)
private struct ContinueCard: View {
    let track: Track
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(Router.self) private var router

    var body: some View {
        HStack(spacing: 22) {
            ArtworkView(url: track.artworkURL, pixels: 400, cornerRadius: 12, seed: track.title)
                .frame(width: 132, height: 132)
                .shadow(color: .black.opacity(0.3), radius: 18, y: 10)
            VStack(alignment: .leading, spacing: 6) {
                Text(player.isPlaying ? "NOW PLAYING" : "CONTINUE LISTENING")
                    .font(.system(size: 11, weight: .bold)).tracking(0.8)
                    .foregroundStyle(.white.opacity(0.7))
                Text(track.title).font(.system(size: 24, weight: .bold)).lineLimit(2)
                Text(track.artist).font(.system(size: 15, weight: .medium)).foregroundStyle(.white.opacity(0.8)).lineLimit(1)
                HStack(spacing: 10) {
                    Button {
                        app.sounds.play(player.showsPause ? .pause : .play)
                        player.togglePlayPause()
                    } label: {
                        Label(player.showsPause ? "Pause" : "Play", systemImage: player.showsPause ? "pause.fill" : "play.fill")
                            .contentTransition(.symbolEffect(.replace))
                    }
                    .buttonStyle(PillButtonStyle(prominent: true))
                    Button { router.showNowPlaying = true; app.sounds.play(.open) } label: {
                        Label("Now Playing", systemImage: "arrow.up.left.and.arrow.down.right")
                    }
                    .buttonStyle(PillButtonStyle())
                }
                .padding(.top, 8)
            }
            .foregroundStyle(.white)
            Spacer(minLength: 0)
        }
        .padding(22)
        .background { ArtworkBackdrop(url: track.artworkURL, blur: 50, dim: 0.35) }
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .shadow(color: .black.opacity(0.15), radius: 20, y: 10)
        .environment(\.colorScheme, .dark)
    }
}

@available(macOS 14, *)
private struct ResumeCard: View {
    let tracks: [Track]
    let index: Int
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player

    var body: some View {
        let track = tracks.indices.contains(index) ? tracks[index] : tracks[0]
        HStack(spacing: 18) {
            ArtworkView(url: track.artworkURL, pixels: 240, cornerRadius: 10, seed: track.title)
                .frame(width: 72, height: 72)
            VStack(alignment: .leading, spacing: 3) {
                Text("Pick up where you left off").font(.system(size: 15, weight: .semibold))
                Text("\(track.title) · \(Format.count(tracks.count, "song")) in your last session")
                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Button {
                app.sounds.play(.play)
                player.play(tracks, startingAt: min(max(index, 0), tracks.count - 1))
            } label: { Label("Resume", systemImage: "play.fill") }
            .buttonStyle(PillButtonStyle(prominent: true))
        }
        .padding(16)
        .glassSurface(in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }
}
