import SwiftUI

@available(macOS 14, *)
struct Sidebar: View {
    @Environment(AppModel.self) private var app
    @Environment(Router.self) private var router
    @Environment(LibraryModel.self) private var library
    @Environment(PlayerModel.self) private var player

    var body: some View {
        @Bindable var router = router
        List(selection: $router.selection) {
            Section {
                row("Home", "house", .home)
                row("Search", "magnifyingglass", .search)
            }
            Section("Library") {
                row("Recently Played", "clock.arrow.circlepath", .recents)
                row("Songs", "music.note", .songs)
                row("Artists", "music.mic", .artists)
                row("Albums", "square.stack", .albums)
                row("On This Mac", "internaldrive", .downloads)
                row("Insights", "chart.bar.xaxis", .insights)
            }
            Section {
                ForEach(library.sortedPlaylists) { playlist in
                    Label {
                        HStack {
                            Text(playlist.name).lineLimit(1)
                            Spacer()
                            if playlist.isPinned { Image(systemName: "pin.fill").font(.system(size: 9)).foregroundStyle(.tertiary) }
                        }
                    } icon: {
                        Image(systemName: playlist.isSmart ? "gearshape.2" : "music.note.list")
                    }
                    .tag(SidebarItem.playlist(playlist.id))
                    .contextMenu { PlaylistMenu(playlist: playlist) }
                }
            } header: {
                HStack {
                    Text("Playlists")
                    Spacer()
                    Button { router.playlistPrompt = .create(adding: nil) } label: { Image(systemName: "plus") }
                        .buttonStyle(.plain)
                        .help("New Playlist")
                }
            }
        }
        .listStyle(.sidebar)
        .onChange(of: router.selection) { app.sounds.play(.tap) }
        .safeAreaInset(edge: .bottom, spacing: 0) { DeviceStatus() }
    }

    private func row(_ title: String, _ symbol: String, _ item: SidebarItem) -> some View {
        Label(title, systemImage: symbol).tag(item)
    }
}

/// Where the music is playing, and the way into phone settings.
@available(macOS 14, *)
private struct DeviceStatus: View {
    @Environment(PlayerModel.self) private var player
    @Environment(Router.self) private var router
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Button {
            router.settingsTab = .phone
            openSettings()
        } label: {
            HStack(spacing: 10) {
                Image(systemName: player.ownerIsDesktop ? "macbook" : "iphone")
                    .font(.system(size: 15))
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 22)
                VStack(alignment: .leading, spacing: 1) {
                    Text(player.ownerIsDesktop ? "Playing on This Mac" : "Playing on \(player.ownerName)")
                        .font(.system(size: 12, weight: .medium))
                        .lineLimit(1)
                    Text("Phone & Connection").font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .glassSurface(in: RoundedRectangle(cornerRadius: 12, style: .continuous), interactive: true)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(10)
    }
}
