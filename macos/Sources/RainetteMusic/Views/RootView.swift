import SwiftUI

@available(macOS 14, *)
struct RootView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        switch app.phase {
        case .starting:
            LaunchView()
        case .failed(let reason):
            FailureView(reason: reason)
        case .ready:
            MainView()
        }
    }
}

@available(macOS 14, *)
private struct LaunchView: View {
    var body: some View {
        VStack(spacing: 18) {
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .frame(width: 96, height: 96)
                .shadow(color: .black.opacity(0.2), radius: 16, y: 8)
            ProgressView().controlSize(.small)
            Text("Starting Rainette…").font(.system(size: 13)).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
    }
}

@available(macOS 14, *)
private struct FailureView: View {
    let reason: String
    @Environment(AppModel.self) private var app

    var body: some View {
        ContentUnavailableView {
            Label("Rainette couldn't start", systemImage: "exclamationmark.triangle")
        } description: {
            Text(reason)
        } actions: {
            Button("Try Again") { app.retry() }
                .buttonStyle(PillButtonStyle(prominent: true))
            Button("Use Classic Interface") { ClassicInterface.relaunchClassic() }
                .buttonStyle(PillButtonStyle())
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

@available(macOS 14, *)
private struct MainView: View {
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(Router.self) private var router
    @Environment(LibraryModel.self) private var library
    @State private var columns = NavigationSplitViewVisibility.all
    @State private var promptText = ""

    var body: some View {
        @Bindable var router = router
        NavigationSplitView(columnVisibility: $columns) {
            Sidebar()
                .navigationSplitViewColumnWidth(min: 200, ideal: 230, max: 300)
        } detail: {
            // One SwiftUI hierarchy for every page.
            ZStack(alignment: .bottom) {
                Group {
                    if let route = router.path.last {
                        RouteView(route: route)
                            .id(route)
                            .transition(.asymmetric(insertion: .move(edge: .trailing).combined(with: .opacity), removal: .opacity))
                    } else {
                        DestinationView(item: router.selection ?? .home)
                            .id(router.selection)
                            .transition(.opacity)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .contentMargins(.bottom, player.hasTrack ? 104 : 0, for: .scrollContent)
                if player.hasTrack && !router.showNowPlaying {
                    PlayerBar()
                        .padding(.horizontal, 18)
                        .padding(.bottom, 14)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.smooth(duration: 0.32), value: router.path)
            .toolbar {
                if !router.path.isEmpty {
                    ToolbarItem(placement: .navigation) {
                        Button { router.back() } label: { Image(systemName: "chevron.left") }
                            .help("Back")
                    }
                }
            }
        }
        .searchable(text: $router.searchText, placement: .sidebar, prompt: "Songs, artists, albums")
        .onChange(of: router.searchText) { _, text in
            if !text.isEmpty, router.selection != .search { router.selection = .search }
        }
        .overlay {
            if router.showNowPlaying {
                NowPlayingView()
                    .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity),
                                            removal: .move(edge: .bottom).combined(with: .opacity)))
                    .zIndex(2)
            }
        }
        .overlay(alignment: .top) {
            if let toast = app.toasts.current {
                ToastView(toast: toast)
                    .padding(.top, 12)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .onTapGesture { app.toasts.dismiss() }
                    .zIndex(3)
            }
        }
        .onAppear { app.mainWindowOpen = true }
        .onDisappear { app.mainWindowOpen = false }
        .onChange(of: router.openMiniPlayer) { openWindow(id: "mini") }
        .onChange(of: router.openSettings) { openSettings() }
        .toolbar(router.showNowPlaying ? .hidden : .automatic, for: .windowToolbar)
        .animation(.smooth(duration: 0.35), value: router.showNowPlaying)
        .animation(.smooth(duration: 0.3), value: player.hasTrack)
        .alert(promptTitle, isPresented: Binding(get: { router.playlistPrompt != nil }, set: { if !$0 { router.playlistPrompt = nil } })) {
            TextField("Playlist name", text: $promptText)
            Button("Cancel", role: .cancel) { router.playlistPrompt = nil }
            Button(promptConfirm) { commitPrompt() }
                .keyboardShortcut(.defaultAction)
        }
        .onChange(of: router.playlistPrompt?.id) {
            if case .rename(let playlist) = router.playlistPrompt { promptText = playlist.name } else { promptText = "" }
        }
        .confirmationDialog("Delete “\(router.confirmDelete?.name ?? "")”?",
                            isPresented: Binding(get: { router.confirmDelete != nil }, set: { if !$0 { router.confirmDelete = nil } })) {
            Button("Delete Playlist", role: .destructive) {
                if let playlist = router.confirmDelete {
                    library.delete(playlist)
                    if router.selection == .playlist(playlist.id) { router.selection = .home }
                }
                router.confirmDelete = nil
            }
        } message: {
            Text("The songs stay in your library.")
        }
    }

    private var promptTitle: String {
        if case .rename = router.playlistPrompt { return "Rename Playlist" }
        return "New Playlist"
    }

    private var promptConfirm: String {
        if case .rename = router.playlistPrompt { return "Rename" }
        return "Create"
    }

    private func commitPrompt() {
        let name = promptText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, let prompt = router.playlistPrompt else { return }
        router.playlistPrompt = nil
        switch prompt {
        case .rename(let playlist):
            library.rename(playlist, to: name)
        case .create(let track):
            Task {
                guard let playlist = await library.createPlaylist(named: name) else { return }
                app.sounds.play(.confirm)
                if let track { await library.add(track, to: playlist) }
                else { library.refreshPlaylists(); router.selection = .playlist(playlist.id) }
            }
        }
    }
}

@available(macOS 14, *)
private struct RouteView: View {
    let route: Route

    var body: some View {
        switch route {
        case .artist(let artist): ArtistDetailView(artist: artist)
        case .album(let album): AlbumDetailView(album: album)
        case .playlist(let playlist): PlaylistDetailView(playlist: playlist)
        }
    }
}

@available(macOS 14, *)
private struct DestinationView: View {
    let item: SidebarItem
    @Environment(LibraryModel.self) private var library

    var body: some View {
        switch item {
        case .home: HomeView()
        case .search: SearchView()
        case .recents: RecentsView()
        case .songs: SongsView()
        case .artists: ArtistsView()
        case .albums: AlbumsView()
        case .downloads: DownloadsView()
        case .insights: InsightsView()
        case .playlist(let id):
            if let playlist = library.playlists.first(where: { $0.id == id }) {
                PlaylistDetailView(playlist: playlist).id(id)
            } else {
                EmptyState(title: "Playlist not found", symbol: "music.note.list")
            }
        }
    }
}

/// The grid/list switch, shown in the toolbar of every collection.
@available(macOS 14, *)
struct LayoutToggle: ToolbarContent {
    @AppStorage(Preference.layout) private var layout = "grid"

    var body: some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            Picker("Layout", selection: $layout) {
                Image(systemName: "square.grid.2x2").tag("grid").help("Grid")
                Image(systemName: "list.bullet").tag("list").help("List")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
        }
    }
}
