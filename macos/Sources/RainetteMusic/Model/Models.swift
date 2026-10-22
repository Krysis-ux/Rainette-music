import Foundation

/// A song as the server describes it.
struct Track: Identifiable, Hashable {
    let raw: JSON

    init(_ raw: JSON) { self.raw = raw }

    var id: String { key }
    var key: String { "\(source):\(sourceID)" }
    var source: String { raw["source"].string ?? "youtube" }
    var sourceID: String { raw["source_id"].text }
    /// The library row id; playlist removal is addressed by it.
    var rowID: String { raw["id"].text }
    var title: String { raw["title"].string.flatMap { $0.isEmpty ? nil : $0 } ?? "Untitled" }
    var artist: String {
        if let name = raw["artist"].string, !name.isEmpty { return name }
        return raw["metadata"]["artists"][0]["name"].text
    }
    var artistID: String { raw["metadata"]["artist_id"].text }
    var albumTitle: String {
        let name = raw["metadata"]["album_name"].text
        return name.isEmpty ? raw["metadata"]["album"]["name"].text : name
    }
    var albumID: String {
        let id = raw["metadata"]["album_id"].text
        return id.isEmpty ? raw["metadata"]["album"]["id"].text : id
    }
    var duration: Double { raw["duration_s"].double ?? 0 }
    var artworkURL: String { raw["thumbnail_url"].text }
    var playCount: Int { raw["play_count"].int ?? 0 }
    var isLocal: Bool { source == "local" }
    var isPlayable: Bool { !sourceID.isEmpty }

    var artistRef: Artist? {
        artist.isEmpty ? nil : Artist(["id": .string(artistID), "name": .string(artist), "thumbnail_url": .string(artworkURL)])
    }

    var albumRef: Album? {
        albumTitle.isEmpty && albumID.isEmpty ? nil : Album([
            "id": .string(albumID), "title": .string(albumTitle), "artist": .string(artist),
            "thumbnail_url": .string(artworkURL),
        ])
    }

    static func list(_ json: JSON) -> [Track] {
        json.array.map(Track.init).filter(\.isPlayable)
    }
}

struct Artist: Identifiable, Hashable {
    let raw: JSON
    init(_ raw: JSON) { self.raw = raw }

    var id: String { artistID.isEmpty ? "name:\(name.lowercased())" : artistID }
    var artistID: String { raw["id"].string ?? raw["artist_id"].text }
    var name: String { raw["name"].string.flatMap { $0.isEmpty ? nil : $0 } ?? raw["artist"].string ?? "Unknown Artist" }
    var artworkURL: String { raw["thumbnail_url"].text }
    var subscribers: String { raw["subscribers"].text }
    var trackCount: Int { raw["track_count"].int ?? 0 }
    var playCount: Int { raw["play_count"].int ?? 0 }
    var description: String { raw["description"].text }

    static func list(_ json: JSON) -> [Artist] { json.array.map(Artist.init) }
}

struct Album: Identifiable, Hashable {
    let raw: JSON
    init(_ raw: JSON) { self.raw = raw }

    var id: String { albumID.isEmpty ? "title:\(artist.lowercased())|\(title.lowercased())" : albumID }
    var albumID: String { raw["id"].string.flatMap { $0.isEmpty ? nil : $0 } ?? raw["browse_id"].text }
    var title: String { raw["title"].string.flatMap { $0.isEmpty ? nil : $0 } ?? raw["name"].string ?? "Unknown Album" }
    var artist: String { raw["artist"].text }
    var artistID: String { raw["artist_id"].text }
    var year: String { raw["year"].text }
    var artworkURL: String { raw["thumbnail_url"].text }
    var releaseType: String { raw["release_type"].text }
    var trackCount: Int { raw["track_count"].int ?? tracks.count }
    /// Library albums carry their tracks; catalog ones are fetched on demand.
    var tracks: [Track] { Track.list(raw["tracks"]) }

    var kindLabel: String {
        switch releaseType.lowercased() {
        case "single": return "Single"
        case "ep": return "EP"
        default: return trackCount == 1 ? "Single" : "Album"
        }
    }

    var artistRef: Artist? {
        artist.isEmpty ? nil : Artist(["id": .string(artistID), "name": .string(artist)])
    }

    static func list(_ json: JSON) -> [Album] { json.array.map(Album.init) }
}

struct Playlist: Identifiable, Hashable {
    let raw: JSON
    init(_ raw: JSON) { self.raw = raw }

    var id: String { raw["id"].text }
    var name: String { raw["name"].string.flatMap { $0.isEmpty ? nil : $0 } ?? "Untitled Playlist" }
    var isSmart: Bool { raw["kind"].text == "smart" }
    var trackCount: Int { raw["track_count"].int ?? 0 }
    var isPinned: Bool { raw["pinned"].bool }
    var artworkKey: String { raw["artwork_key"].text }
    var description: String { raw["description"].text }
    var updatedAt: String { raw["updated_at"].text }

    static func list(_ json: JSON) -> [Playlist] { json.array.map(Playlist.init) }
}

/// What the player engine last said about the transport.
enum PlaybackState: String {
    case idle, loading, playing, paused, error
}

enum RepeatMode: String, CaseIterable {
    case off, all, one

    var next: RepeatMode {
        switch self { case .off: return .all; case .all: return .one; case .one: return .off }
    }

    var symbol: String { self == .one ? "repeat.1" : "repeat" }
    var label: String {
        switch self { case .off: return "Repeat Off"; case .all: return "Repeat All"; case .one: return "Repeat One" }
    }
}

enum Format {
    static func time(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "0:00" }
        let total = Int(seconds)
        let hours = total / 3600, minutes = (total % 3600) / 60, secs = total % 60
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, secs) : String(format: "%d:%02d", minutes, secs)
    }

    static func totalDuration(_ seconds: Double) -> String {
        let minutes = Int(seconds / 60)
        if minutes >= 60 { return "\(minutes / 60) hr \(minutes % 60) min" }
        return "\(max(minutes, seconds > 0 ? 1 : 0)) min"
    }

    static func count(_ n: Int, _ singular: String, _ plural: String? = nil) -> String {
        "\(n) \(n == 1 ? singular : (plural ?? singular + "s"))"
    }
}
