import Foundation

/// One song from a search, shaped like the computer's catalog results so the
/// page treats it exactly as it treats theirs.
public struct SongResult: Sendable, Equatable {
    public let sourceId: String
    public let title: String
    public let artist: String
    public let durationS: Int?
    public let thumbnailURL: String

    public var json: [String: Any] {
        var song: [String: Any] = [
            "source": "youtube",
            "source_id": sourceId,
            "title": title,
            "artist": artist,
            "thumbnail_url": thumbnailURL,
            "metadata": ["source_detail": "on-device"],
        ]
        if let durationS { song["duration_s"] = durationS }
        return song
    }
}

/// Song search on this phone, asking YouTube Music directly with the same
/// "songs only" filter the Android app's extractor uses. Nothing goes through
/// a Rainette server or anybody else's.
public enum MusicSearch {
    static let endpoint = URL(string: "https://music.youtube.com/youtubei/v1/search?prettyPrint=false")!
    static let clientVersion = "1.20251020.01.00"
    /// YouTube Music's own "Songs" filter.
    static let songsOnly = "EgWKAQIIAWoMEA4QChADEAQQCRAF"
    static let limit = 30

    public static func search(_ query: String, session: URLSession = .shared) async throws -> [SongResult] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 200 else {
            throw PlayerError(PlayerError.invalid, "Type something to search for.")
        }
        var request = URLRequest(url: endpoint, timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("https://music.youtube.com", forHTTPHeaderField: "Origin")
        request.setValue(StreamResolver.safari, forHTTPHeaderField: "User-Agent")
        let body: [String: Any] = [
            "context": ["client": ["clientName": "WEB_REMIX", "clientVersion": clientVersion, "hl": "en", "gl": "US"]],
            "query": trimmed,
            "params": songsOnly,
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw PlayerError.classify(error)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 429 { throw PlayerError(PlayerError.rateLimited) }
        guard status == 200 else { throw PlayerError(PlayerError.searchFailed) }
        return try parse(data)
    }

    /// The songs in a search answer. A shape this does not recognise is a
    /// search that failed, not an empty one: "nothing matched" would be a lie.
    public static func parse(_ data: Data) throws -> [SongResult] {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let tabs = dig(root, "contents", "tabbedSearchResultsRenderer", "tabs") as? [[String: Any]],
              let sections = dig(tabs.first ?? [:], "tabRenderer", "content", "sectionListRenderer", "contents") as? [[String: Any]]
        else { throw PlayerError(PlayerError.searchFailed) }

        let rows = sections
            .compactMap { dig($0, "musicShelfRenderer", "contents") as? [[String: Any]] }
            .flatMap { $0 }
            .compactMap { $0["musicResponsiveListItemRenderer"] as? [String: Any] }
        return Array(rows.compactMap(song).prefix(limit))
    }

    private static func song(_ row: [String: Any]) -> SongResult? {
        let id = (dig(row, "playlistItemData", "videoId") as? String)
            ?? (dig(row, "overlay", "musicItemThumbnailOverlayRenderer", "content", "musicPlayButtonRenderer",
                    "playNavigationEndpoint", "watchEndpoint", "videoId") as? String)
        guard let id, StreamResolver.isValidId(id) else { return nil }

        let columns = (row["flexColumns"] as? [[String: Any]] ?? []).map { column in
            dig(column, "musicResponsiveListItemFlexColumnRenderer", "text", "runs") as? [[String: Any]] ?? []
        }
        let title = (columns.first ?? []).compactMap { $0["text"] as? String }.joined()
        // "Artist, Artist • Album • 3:45": the artists are everything before
        // the first separator, and the length is the last thing that reads as one.
        let details = (columns.count > 1 ? columns[1] : []).compactMap { $0["text"] as? String }
        let artist = details.prefix { $0 != " • " }.joined()
        let duration = details.last.flatMap(seconds)

        let thumbnails = dig(row, "thumbnail", "musicThumbnailRenderer", "thumbnail", "thumbnails") as? [[String: Any]] ?? []
        let thumbnail = thumbnails
            .max { ($0["width"] as? Int ?? 0) < ($1["width"] as? Int ?? 0) }?["url"] as? String ?? ""

        return SongResult(
            sourceId: id,
            title: title.isEmpty ? "(untitled)" : title,
            artist: artist.replacingOccurrences(of: " - Topic", with: ""),
            durationS: duration,
            thumbnailURL: thumbnail.hasPrefix("https://") ? thumbnail : ""
        )
    }

    /// "3:45" or "1:02:03" in seconds; nil for anything else.
    static func seconds(_ text: String) -> Int? {
        let parts = text.split(separator: ":").map { Int($0) }
        guard (2...3).contains(parts.count), parts.allSatisfy({ $0 != nil }) else { return nil }
        return parts.reduce(0) { $0 * 60 + ($1 ?? 0) }
    }

    private static func dig(_ object: [String: Any], _ path: String...) -> Any? {
        var current: Any? = object
        for key in path { current = (current as? [String: Any])?[key] }
        return current
    }
}
