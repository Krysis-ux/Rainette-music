import AppKit
import Observation
import SwiftUI

enum SidebarItem: Hashable {
    case home, search, recents, songs, artists, albums, downloads, insights
    case playlist(String)
}

enum Route: Hashable {
    case artist(Artist)
    case album(Album)
    case playlist(Playlist)
}

@available(macOS 14, *)
@Observable @MainActor
final class Router {
    var selection: SidebarItem? = .home {
        didSet { if oldValue != selection { path = [] } }
    }
    var path: [Route] = []
    var searchText = ""
    var showNowPlaying = false
    var showLyrics = false
    var settingsTab: SettingsTab = .general
    /// Naming a playlist, asked for from wherever "New Playlist…" was chosen.
    var playlistPrompt: PlaylistPrompt?
    var confirmDelete: Playlist?
    /// Bumped when a playlist's contents change, so an open one reloads.
    var playlistChanged = 0
    /// Bumped to ask the main window to open the mini player or Settings.
    var openMiniPlayer = 0
    var openSettings = 0

    func open(_ route: Route) {
        if case .playlist(let playlist) = route {
            selection = .playlist(playlist.id)
            return
        }
        guard path.last != route else { return }
        path.append(route)
    }

    func back() {
        guard !path.isEmpty else { return }
        path.removeLast()
    }
}

enum SettingsTab: Hashable { case general, playback, equalizer, library, phone, updates }

enum PlaylistPrompt: Identifiable {
    case create(adding: Track?)
    case rename(Playlist)

    var id: String {
        switch self {
        case .create(let track): return "create:\(track?.key ?? "")"
        case .rename(let playlist): return "rename:\(playlist.id)"
        }
    }
}

/// Keys for preferences, and the two the player page reads from its own storage.
enum Preference {
    static let layout = "layout"                       // "grid" | "list"
    static let appearance = "appearance"               // "system" | "light" | "dark"
    static let fadePlayPause = "fadePlayPause"         // mirrored as rainette.fadePlayPause
    static let autoplaySimilar = "autoplaySimilar"     // mirrored as rainette.autoplaySimilar
}

@available(macOS 14, *)
@Observable @MainActor
final class AppModel {
    enum Phase: Equatable { case starting, ready, failed(String) }

    var phase: Phase = .starting
    private(set) var client: RainetteClient?
    var engineVersion = ""

    let engine = EngineProcess()
    let audio = AudioEngineHost()
    let player = PlayerModel()
    let library = LibraryModel()
    let phone = PhoneModel()
    let router = Router()
    let toasts = ToastCenter()
    let sounds = SoundEffects.shared

    @ObservationIgnored var mainWindowOpen = false
    @ObservationIgnored var showMainWindow: (() -> Void)?

    @ObservationIgnored private var crashes: [Date] = []
    @ObservationIgnored private var quitting = false
    @ObservationIgnored private var playingActivity: NSObjectProtocol?

    init() {
        player.app = self
        library.app = self
        phone.app = self
        engine.onExit = { [weak self] status in self?.engineExited(status) }
        audio.onLoad = { [weak self] in self?.enginePageLoaded() }
    }

    /// main.ENGINE_EXIT_FOR_UPDATE: the engine quit so an update can install.
    static let engineExitForUpdate: Int32 = 75

    func start() async {
        phase = .starting
        do {
            let handshake = try await engine.start()
            attach(handshake)
            phase = .ready
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    /// "Try Again" after a failure gets a fresh restart budget.
    func retry() {
        crashes.removeAll()
        Task { await start() }
    }

    func shutdown() {
        quitting = true
        client?.close()
        engine.stop()
    }

    private func attach(_ handshake: EngineHandshake) {
        client?.close()
        engineVersion = handshake.version
        // Development only: lets a test client join the same engine.
        if let path = ProcessInfo.processInfo.environment["RAINETTE_DEBUG_HANDSHAKE_FILE"], !path.isEmpty {
            let json = JSON.object(["port": .number(Double(handshake.port)), "token": .string(handshake.token)])
            try? json.encoded().write(to: URL(fileURLWithPath: path), options: .atomic)
        }
        let client = RainetteClient(handshake: handshake)
        client.onMessage = { [weak self] message in self?.route(message) }
        client.onOpen = { [weak self] in
            self?.player.requestState()
            self?.library.refreshAll()
        }
        self.client = client
        client.connect()
        audio.load(handshake)
    }

    private func route(_ message: JSON) {
        player.handle(message)
        library.handle(message)
        updatePlayingActivity()
        switch message["type"].text {
        case "music_open_artist" where debugging && !message["debug_route"].isNull:
            debugRoute(message["debug_route"])
        case "music_open_artist":
            let name = message["name"].text, id = message["artist_id"].text
            guard !name.isEmpty || !id.isEmpty else { return }
            router.showNowPlaying = false
            router.open(.artist(Artist(["id": .string(id), "name": .string(name), "thumbnail_url": message["thumbnail_url"]])))
            NSApp.activate(ignoringOtherApps: true)
        default:
            break
        }
    }

    // MARK: Development

    @ObservationIgnored private lazy var debugging = ProcessInfo.processInfo.environment["RAINETTE_DEBUG_HANDSHAKE_FILE"].map { !$0.isEmpty } ?? false

    private func debugRoute(_ message: JSON) {
        let value = message["value"].text
        switch message["route"].text {
        case "search": router.showNowPlaying = false; router.selection = .search; router.searchText = value
        case "sidebar":
            router.showNowPlaying = false
            let items: [String: SidebarItem] = ["home": .home, "recents": .recents, "songs": .songs, "artists": .artists,
                                                "albums": .albums, "downloads": .downloads, "insights": .insights]
            if let item = items[value] { router.selection = item }
            else if let playlist = library.playlists.first(where: { $0.name == value }) { router.selection = .playlist(playlist.id) }
        case "album": router.showNowPlaying = false; router.open(.album(Album(message["album"])))
        case "nowplaying": router.showLyrics = value == "lyrics"; router.showNowPlaying = true
        case "settings":
            if let tab = ["general": SettingsTab.general, "playback": .playback, "equalizer": .equalizer,
                          "phone": .phone, "updates": .updates][value] { router.settingsTab = tab }
            router.openSettings += 1
        case "mini": router.openMiniPlayer += 1
        case "close": NSApp.windows.first { $0.title != "Rainette Engine" && $0.isVisible && $0.canBecomeMain }?.performClose(nil)
        case "drag":
            let parts = value.split(separator: ",").map(String.init)
            guard parts.count == 5, let window = NSApp.windows.first(where: { $0.title == parts[0] && $0.isVisible }),
                  let x = Double(parts[1]), let y = Double(parts[2]), let dx = Double(parts[3]), let dy = Double(parts[4]) else { return }
            let height = window.contentLayoutRect.height + (window.frame.height - window.contentLayoutRect.height)
            func send(_ type: NSEvent.EventType, _ px: Double, _ py: Double) {
                guard let event = NSEvent.mouseEvent(with: type, location: NSPoint(x: px, y: height - py), modifierFlags: [],
                                                     timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber,
                                                     context: nil, eventNumber: 0, clickCount: 1, pressure: type == .leftMouseUp ? 0 : 1) else { return }
                window.sendEvent(event)
            }
            // A first click on an inactive window only focuses it.
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                send(.leftMouseDown, x, y)
                for step in 1...10 { send(.leftMouseDragged, x + dx * Double(step) / 10, y + dy * Double(step) / 10) }
                send(.leftMouseUp, x + dx, y + dy)
            }
        case "layout": UserDefaults.standard.set(value, forKey: Preference.layout)
        default: break
        }
    }

    /// The player page reads these from its own localStorage when it decides.
    private func enginePageLoaded() {
        Task {
            // The first native launch inherits whatever the old windows set.
            await adopt(Preference.fadePlayPause, from: "rainette.fadePlayPause") { $0 != "0" }
            await adopt(Preference.autoplaySimilar, from: "rainette.autoplaySimilar") { $0 == "1" }
            pushEngineSettings()
            if let stored = await audio.storage("rw.mp.volume"), let volume = Double(stored) { player.volume = volume }
        }
    }

    private func adopt(_ key: String, from storageKey: String, _ isOn: (String) -> Bool) async {
        guard UserDefaults.standard.object(forKey: key) == nil, let stored = await audio.storage(storageKey) else { return }
        UserDefaults.standard.set(isOn(stored), forKey: key)
    }

    func pushEngineSettings() {
        let defaults = UserDefaults.standard
        let fade = defaults.object(forKey: Preference.fadePlayPause) as? Bool ?? true
        audio.setStorage("rainette.fadePlayPause", fade ? "1" : "0")
        audio.setStorage("rainette.autoplaySimilar", defaults.bool(forKey: Preference.autoplaySimilar) ? "1" : "0")
    }

    private func engineExited(_ status: Int32) {
        guard !quitting else { return }
        if status == Self.engineExitForUpdate {
            NSApp.terminate(nil)
            return
        }
        let now = Date()
        crashes = crashes.filter { now.timeIntervalSince($0) < 300 } + [now]
        client?.close()
        client = nil
        if crashes.count > 3 {
            phase = .failed("Rainette's music engine keeps stopping.")
            return
        }
        toasts.show("Reconnecting to the music engine…", symbol: "arrow.clockwise", style: .info)
        Task { await start() }
    }

    /// Keep App Nap away from the app while music plays in its web view.
    private func updatePlayingActivity() {
        if player.isPlaying, playingActivity == nil {
            playingActivity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep], reason: "Playing music")
        } else if !player.isPlaying, let activity = playingActivity {
            ProcessInfo.processInfo.endActivity(activity)
            playingActivity = nil
        }
    }

    // MARK: Shared actions

    /// A play that has to fetch first says so if the fetch is not instant.
    private func loading<T>(_ name: String, _ work: () async -> T) async -> T {
        let notice = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 400_000_000)
            guard !Task.isCancelled else { return }
            self?.toasts.show("Loading \(name)…", symbol: "music.note", style: .info)
        }
        let result = await work()
        notice.cancel()
        if toasts.current?.message == "Loading \(name)…" { toasts.dismiss() }
        return result
    }

    func play(album: Album, shuffled: Bool = false) {
        sounds.play(.play)
        Task {
            let (_, tracks, error) = await loading(album.title) { await library.tracks(for: album) }
            guard !tracks.isEmpty else {
                toasts.show(error ?? "Couldn't load \(album.title).", symbol: "exclamationmark.triangle.fill", style: .error)
                return
            }
            shuffled ? player.shuffle(tracks) : player.play(tracks)
        }
    }

    func play(playlist: Playlist, shuffled: Bool = false) {
        sounds.play(.play)
        Task {
            guard let tracks = await loading(playlist.name, { await library.tracks(for: playlist) }) else {
                toasts.show("Couldn't load \(playlist.name).", symbol: "exclamationmark.triangle.fill", style: .error)
                return
            }
            guard !tracks.isEmpty else {
                toasts.show("\(playlist.name) is empty.", symbol: "music.note.list", style: .info)
                return
            }
            shuffled ? player.shuffle(tracks) : player.play(tracks)
        }
    }

    func play(artist: Artist, shuffled: Bool = false) {
        sounds.play(.play)
        Task {
            let catalog = await loading(artist.name) { await library.catalog(for: artist) }
            let tracks = catalog.songs.isEmpty ? catalog.videos : catalog.songs
            guard !tracks.isEmpty else {
                toasts.show(catalog.note.isEmpty ? "No songs found for \(artist.name)." : catalog.note, symbol: "exclamationmark.triangle.fill", style: .error)
                return
            }
            shuffled ? player.shuffle(tracks) : player.play(tracks)
        }
    }

    func startMix(_ seed: JSONObject, named name: String) {
        toasts.show("Making a mix from \(name)…", symbol: "sparkles", style: .info)
        Task {
            let tracks = await library.mix(from: seed)
            guard !tracks.isEmpty else {
                toasts.show("Couldn't make a mix from \(name).", symbol: "exclamationmark.triangle.fill", style: .error)
                return
            }
            sounds.play(.play)
            player.play(tracks)
        }
    }
}
