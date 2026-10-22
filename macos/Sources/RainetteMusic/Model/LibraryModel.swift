import Foundation
import Observation

struct SearchResults {
    var songs: [Track] = []
    var artists: [Artist] = []
    var albums: [Album] = []
    var note = ""
    var isEmpty: Bool { songs.isEmpty && artists.isEmpty && albums.isEmpty }
}

struct ArtistCatalog {
    var artist: Artist
    var songs: [Track] = []
    var videos: [Track] = []
    var albums: [Album] = []
    var eps: [Album] = []
    var singles: [Album] = []
    var note = ""
}

struct Lyrics: Equatable {
    struct Line: Equatable, Hashable { let time: Double; let text: String }
    var plain = ""
    var lines: [Line] = []
    var instrumental = false
    var notFound = false
}

struct Insights {
    struct Bucket: Identifiable { let id: Int; let start: Date; let count: Int }
    var unit = "day"
    var buckets: [Bucket] = []
    var totalPlays = 0
    var totalMinutes = 0.0
    var uniqueTracks = 0
    var uniqueArtists = 0
    var topTracks: [Track] = []
    var topArtists: [Artist] = []
}

struct LocalStatus {
    struct Root: Hashable { let path: String; let trackCount: Int; let error: String }
    var roots: [Root] = []
    var tracks = 0
    var missing = 0
    var bytes = 0
    var scanning = false
    var loaded = false
}

/// Everything the app reads from the library and catalog.
@available(macOS 14, *)
@Observable @MainActor
final class LibraryModel {
    var tracks: [Track] = []
    var artists: [Artist] = []
    var albums: [Album] = []
    var followed: [Artist] = []
    var playlists: [Playlist] = []
    var recent: [Track] = []
    var topArtists: [Artist] = []
    var lastSession: (tracks: [Track], index: Int)?
    var local = LocalStatus()
    var localTracks: [Track] { tracks.filter(\.isLocal) }
    var loadedOnce = false
    var catalogNote = ""

    @ObservationIgnored weak var app: AppModel?
    private var client: RainetteClient? { app?.client }
    /// Catalog pages, kept briefly so Back and a second visit are instant.
    @ObservationIgnored private var catalogs: [String: (at: Date, value: ArtistCatalog)] = [:]
    @ObservationIgnored private var albumTracks: [String: (at: Date, album: Album, tracks: [Track])] = [:]
    private static let cacheLife: TimeInterval = 15 * 60

    func cachedCatalog(for artist: Artist) -> ArtistCatalog? {
        catalogs[artist.id].flatMap { Date().timeIntervalSince($0.at) < Self.cacheLife ? $0.value : nil }
    }

    func cachedTracks(for album: Album) -> (Album, [Track])? {
        albumTracks[album.id].flatMap { Date().timeIntervalSince($0.at) < Self.cacheLife ? ($0.album, $0.tracks) : nil }
    }

    var sortedPlaylists: [Playlist] {
        playlists.sorted { ($0.isPinned ? 0 : 1, $0.name.lowercased()) < ($1.isPinned ? 0 : 1, $1.name.lowercased()) }
    }

    func refreshAll() {
        refreshLibrary()
        refreshPlaylists()
        refreshRecent()
        client?.send("music_top_artists", ["id": .string(Self.id("mta"))])
        client?.send("music_queue_session_list", ["id": .string(Self.id("qsl"))])
        client?.send("music_status", ["id": .string(Self.id("mstat"))])
        refreshLocal()
    }

    func refreshLibrary() { client?.send("music_library_index", ["id": .string(Self.id("mlib")), "limit": 1000]) }
    func refreshPlaylists() { client?.send("music_playlist_list", ["id": .string(Self.id("pll"))]) }
    func refreshRecent() { client?.send("music_recent", ["id": .string(Self.id("mrec"))]) }
    func refreshLocal() { client?.send("music_local_status", ["id": .string(Self.id("mls"))]) }

    static func id(_ prefix: String) -> String { "\(prefix)_\(UUID().uuidString.prefix(10))" }

    // MARK: Catalog

    func search(_ query: String) async -> SearchResults {
        guard let client else { return SearchResults() }
        let reply = await client.request("music_catalog_search", ["query": .string(query)], timeout: 30)
        if !reply["ok"].bool { return SearchResults(note: reply["msg"].string ?? "Search failed.") }
        return SearchResults(songs: Track.list(reply["songs"]), artists: Artist.list(reply["artists"]),
                             albums: Album.list(reply["albums"]), note: reply["msg"].text)
    }

    func catalog(for artist: Artist) async -> ArtistCatalog {
        if let cached = cachedCatalog(for: artist) { return cached }
        guard let client else { return ArtistCatalog(artist: artist) }
        let reply = await client.request("music_artist_catalog", [
            "artist_id": .string(artist.artistID), "name": .string(artist.name),
        ], timeout: 40)
        guard reply["ok"].bool else {
            return ArtistCatalog(artist: artist, note: reply["msg"].string ?? "This artist's catalog could not be loaded.")
        }
        let releases = Album.list(reply["albums"])
        let isEP: (Album) -> Bool = { $0.releaseType.lowercased() == "ep" }
        let described = reply["artist"].isNull ? artist : Artist(reply["artist"])
        let catalog = ArtistCatalog(artist: described.artworkURL.isEmpty ? artist : described,
                                    songs: Track.list(reply["songs"]), videos: Track.list(reply["videos"]),
                                    albums: releases.filter { !isEP($0) }, eps: releases.filter(isEP),
                                    singles: Album.list(reply["singles"]), note: reply["msg"].text)
        catalogs[artist.id] = (Date(), catalog)
        return catalog
    }

    func tracks(for album: Album) async -> (Album, [Track], String?) {
        if !album.tracks.isEmpty { return (album, album.tracks, nil) }
        if let (cachedAlbum, tracks) = cachedTracks(for: album) { return (cachedAlbum, tracks, nil) }
        guard let client else { return (album, [], "Not connected.") }
        let reply = await client.request("music_album_tracks", [
            "album_id": .string(album.albumID), "title": .string(album.title), "artist": .string(album.artist),
        ], timeout: 30)
        guard reply["ok"].bool else { return (album, [], reply["msg"].string ?? "This album could not be loaded.") }
        let described = reply["album"].isNull ? album : Album(reply["album"])
        let result = (described.artworkURL.isEmpty ? album : described, Track.list(reply["tracks"]))
        if !result.1.isEmpty { albumTracks[album.id] = (Date(), result.0, result.1) }
        return (result.0, result.1, nil)
    }

    /// nil when the playlist could not be loaded, which is not the same as empty.
    func tracks(for playlist: Playlist) async -> [Track]? {
        guard let client else { return nil }
        let type = playlist.isSmart ? "music_smart_playlist_tracks" : "music_playlist_tracks"
        let reply = await client.request(type, ["playlist_id": .string(playlist.id)], timeout: 20)
        return reply["ok"].bool ? Track.list(reply["tracks"]) : nil
    }

    func mix(from seed: JSONObject) async -> [Track] {
        guard let client else { return [] }
        let reply = await client.request("music_mix_from_seed", ["seed": .object(seed)], timeout: 30)
        return Track.list(reply["tracks"])
    }

    func lyrics(for track: Track) async -> Lyrics? {
        guard let client else { return nil }
        let reply = await client.request("music_lyrics", ["track": track.raw], timeout: 20)
        guard reply["ok"].bool else { return nil }
        return Lyrics(plain: reply["plain"].text, lines: Self.parseSynced(reply["synced"].text),
                      instrumental: reply["instrumental"].bool,
                      notFound: reply["not_found"].bool || (reply["plain"].text.isEmpty && !reply["instrumental"].bool))
    }

    static func parseSynced(_ lrc: String) -> [Lyrics.Line] {
        var lines: [Lyrics.Line] = []
        let pattern = try! NSRegularExpression(pattern: #"\[(\d+):(\d+(?:\.\d+)?)\]"#)
        for raw in lrc.split(whereSeparator: \.isNewline) {
            let line = String(raw)
            let range = NSRange(line.startIndex..., in: line)
            let matches = pattern.matches(in: line, range: range)
            guard !matches.isEmpty else { continue }
            let text = pattern.stringByReplacingMatches(in: line, range: range, withTemplate: "").trimmingCharacters(in: .whitespaces)
            for match in matches {
                guard let minutes = Range(match.range(at: 1), in: line).flatMap({ Double(line[$0]) }),
                      let seconds = Range(match.range(at: 2), in: line).flatMap({ Double(line[$0]) }) else { continue }
                lines.append(Lyrics.Line(time: minutes * 60 + seconds, text: text))
            }
        }
        // LRC marks every pause with a blank line.
        let sorted = lines.sorted { $0.time < $1.time }
        return sorted.enumerated().compactMap { offset, line in
            guard line.text.isEmpty else { return line }
            let next = sorted[(offset + 1)...].first { !$0.text.isEmpty }?.time ?? .infinity
            return next - line.time >= 6 ? line : nil
        }
    }

    func insights(days: Int) async -> Insights? {
        guard let client else { return nil }
        let reply = await client.request("music_insights", ["days": .number(Double(days))], timeout: 20)
        guard reply["ok"].bool else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withFullDate]
        let buckets = reply["buckets"].array.enumerated().compactMap { offset, bucket -> Insights.Bucket? in
            let start = String(bucket["start"].text.prefix(10))
            guard let date = formatter.date(from: start) else { return nil }
            return Insights.Bucket(id: offset, start: date, count: bucket["count"].int ?? 0)
        }
        return Insights(unit: reply["bucket_unit"].string ?? "day", buckets: buckets,
                        totalPlays: reply["total_plays"].int ?? 0, totalMinutes: reply["total_minutes"].double ?? 0,
                        uniqueTracks: reply["unique_tracks"].int ?? 0, uniqueArtists: reply["unique_artists"].int ?? 0,
                        topTracks: Track.list(reply["top_tracks"]), topArtists: Artist.list(reply["top_artists"]))
    }

    // MARK: Playlists

    func createPlaylist(named name: String) async -> Playlist? {
        guard let client else { return nil }
        let reply = await client.request("music_playlist_create", ["name": .string(name)])
        guard reply["ok"].bool, !reply["playlist"].isNull else {
            app?.toasts.show(reply["msg"].string ?? "Couldn't create the playlist.", symbol: "exclamationmark.triangle.fill", style: .error)
            return nil
        }
        return Playlist(reply["playlist"])
    }

    func add(_ track: Track, to playlist: Playlist) async {
        guard let client else { return }
        let reply = await client.request("music_playlist_add_track", [
            "playlist_id": .string(playlist.id),
            "source": .string(track.source), "source_id": .string(track.sourceID),
            "title": .string(track.title), "artist": .string(track.artist),
            "duration_s": track.raw["duration_s"], "thumbnail_url": .string(track.artworkURL),
            "metadata": track.raw["metadata"].isNull ? [:] : track.raw["metadata"],
        ])
        if reply["ok"].bool {
            app?.toasts.show("Added to \(playlist.name)", symbol: "checkmark.circle.fill", style: .success)
            app?.sounds.play(.confirm)
            refreshPlaylists()
        } else {
            app?.toasts.show(reply["msg"].string ?? "Couldn't add to \(playlist.name).", symbol: "exclamationmark.triangle.fill", style: .error)
        }
    }

    func remove(_ track: Track, from playlist: Playlist) async -> Bool {
        guard let client else { return false }
        let reply = await client.request("music_playlist_remove_track", [
            "playlist_id": .string(playlist.id), "track_id": .string(track.rowID),
        ])
        if reply["ok"].bool { refreshPlaylists() }
        return reply["ok"].bool
    }

    func rename(_ playlist: Playlist, to name: String) {
        client?.send("music_playlist_rename", ["id": .string(Self.id("plr")), "playlist_id": .string(playlist.id), "name": .string(name)])
    }

    func delete(_ playlist: Playlist) {
        let type = playlist.isSmart ? "music_smart_playlist_delete" : "music_playlist_delete"
        client?.send(type, ["id": .string(Self.id("pld")), "playlist_id": .string(playlist.id)])
    }

    func setPinned(_ playlist: Playlist, _ pinned: Bool) {
        client?.send("music_playlist_update_meta", ["id": .string(Self.id("plm")), "playlist_id": .string(playlist.id), "pinned": .bool(pinned)])
    }

    func artworkURL(for playlist: Playlist) -> URL? {
        guard !playlist.artworkKey.isEmpty, let client else { return nil }
        return client.authorizedURL("playlist-artwork/\(playlist.artworkKey)")
    }

    // MARK: Artists

    func isFollowing(_ artist: Artist) -> Bool {
        followed.contains { $0.artistID == artist.artistID && !artist.artistID.isEmpty || $0.name.caseInsensitiveCompare(artist.name) == .orderedSame }
    }

    func toggleFollow(_ artist: Artist) {
        let following = isFollowing(artist)
        client?.send(following ? "music_artist_unfollow" : "music_artist_follow", [
            "id": .string(Self.id("maf")), "artist_id": .string(artist.artistID),
            "name": .string(artist.name), "thumbnail_url": .string(artist.artworkURL),
        ])
        app?.sounds.play(following ? .tap : .confirm)
    }

    // MARK: Recents

    func forget(_ track: Track) {
        client?.send("music_recent_delete", ["id": .string(Self.id("mrd")), "scope": "track", "track_id": .string(track.rowID)])
    }

    // MARK: On this Mac

    func download(_ tracks: [Track], label: String) {
        let list = tracks.filter { !$0.isLocal }
        guard !list.isEmpty else { return }
        client?.send("music_download_track", ["id": .string(Self.id("dl")), "tracks": .array(list.map(\.raw))])
        app?.toasts.show(list.count == 1 ? "Downloading “\(list[0].title)”…" : "Downloading \(label)…", symbol: "arrow.down.circle", style: .info, sticky: true)
    }

    func addFolder(_ path: String) async {
        guard let client else { return }
        do {
            let result = try await client.native("add_music_folder", [.string(path)])
            if result["ok"].bool {
                client.send("music_local_scan", ["id": .string(Self.id("scan"))])
                local.scanning = true
            } else {
                app?.toasts.show(result["msg"].string ?? "That folder could not be added.", symbol: "folder.badge.questionmark", style: .error)
            }
        } catch {
            app?.toasts.show(error.localizedDescription, symbol: "exclamationmark.triangle.fill", style: .error)
        }
        refreshLocal()
    }

    func removeFolder(_ path: String) {
        client?.send("music_local_roots", ["id": .string(Self.id("roots")), "action": "remove", "path": .string(path)])
        refreshLocal()
    }

    func rescan() {
        client?.send("music_local_scan", ["id": .string(Self.id("scan"))])
        local.scanning = true
    }

    // MARK: Incoming

    func handle(_ message: JSON) {
        let type = message["type"].text
        switch type {
        case "music_library_index_result":
            guard message["ok"].bool else { return }
            tracks = Track.list(message["tracks"])
            artists = Artist.list(message["artists"])
            albums = Album.list(message["albums"])
            followed = Artist.list(message["followed_artists"])
            loadedOnce = true
        case "music_artist_followed", "music_artist_unfollowed", "music_followed_artists_result":
            if case .array = message["followed_artists"] { followed = Artist.list(message["followed_artists"]) }
        case "music_recent_result", "music_recent_deleted":
            if case .array = message["tracks"] { recent = Track.list(message["tracks"]) }
        case "music_top_artists_result":
            if message["ok"].bool { topArtists = Artist.list(message["artists"]) }
        case "music_queue_session_list_result", "music_queue_session_saved", "music_queue_session_deleted":
            let last = message["sessions"].array.first { $0["is_last"].bool }
            if let last {
                lastSession = (Track.list(last["tracks"]), last["index"].int ?? 0)
            }
        case "music_status":
            if message["ok"].bool, !message["ytmusic_available"].bool {
                catalogNote = message["ytdlp_available"].bool
                    ? "Artist and album browsing needs ytmusicapi on this computer."
                    : "Streaming needs yt-dlp on this computer."
            }
        case "music_local_status_result", "music_local_roots_result", "music_local_scan_result":
            if type == "music_local_scan_result" {
                local.scanning = false
                refreshLibrary()
            }
            if message["ok"].bool, case .array(let roots) = message["roots"] {
                local.roots = roots.map { LocalStatus.Root(path: $0["path"].text, trackCount: $0["track_count"].int ?? 0, error: $0["last_error"].text) }
                local.tracks = message["tracks"].int ?? local.tracks
                local.missing = message["missing"].int ?? 0
                local.bytes = message["bytes"].int ?? local.bytes
                local.loaded = true
            } else if !message["ok"].bool, let reason = message["msg"].string {
                app?.toasts.show(reason, symbol: "exclamationmark.triangle.fill", style: .error)
            }
        case "music_download_result":
            let done = message["ok"].bool
            app?.toasts.show(done ? "Saved to this Mac" : (message["msg"].string ?? "The download did not finish."),
                             symbol: done ? "checkmark.circle.fill" : "exclamationmark.triangle.fill", style: done ? .success : .error)
            refreshLocal()
            refreshLibrary()
        default:
            if type.hasPrefix("music_playlist") || type.hasPrefix("music_smart_playlist") {
                if case .array = message["playlists"] { playlists = Playlist.list(message["playlists"]) }
            }
            if type == "music_data_cleared", message["ok"].bool { refreshAll() }
        }
        if type == "music_now_playing", message["state"].text == "playing" {
            // A new play lands in Recents on the server; keep Home current.
            scheduleRecentRefresh()
        }
    }

    @ObservationIgnored private var recentRefresh: Task<Void, Never>?
    private func scheduleRecentRefresh() {
        recentRefresh?.cancel()
        recentRefresh = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard let self, !Task.isCancelled else { return }
            self.refreshRecent()
            self.client?.send("music_top_artists", ["id": .string(Self.id("mta"))])
        }
    }
}
