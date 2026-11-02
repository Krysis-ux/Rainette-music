import Foundation
import YouTubeKit

/// A song's audio: where it is, and how to ask for it.
public struct ResolvedStream: Sendable, Equatable {
    public let url: URL
    /// Each address is signed for one YouTube client, and a request that
    /// claims to be another one is refused.
    public let userAgent: String
    public let expiresAt: Date
}

/// What `rankAudio` needs to know about a stream, without YouTubeKit's types.
public struct AudioCandidate: Sendable, Equatable {
    public let url: URL
    public let fileExtension: String
    public let bitrate: Int
    public let hasAudio: Bool
    public let hasVideo: Bool

    public init(url: URL, fileExtension: String, bitrate: Int, hasAudio: Bool, hasVideo: Bool) {
        self.url = url
        self.fileExtension = fileExtension
        self.bitrate = bitrate
        self.hasAudio = hasAudio
        self.hasVideo = hasVideo
    }
}

/// Song id to playable audio, entirely on this phone. YouTubeKit talks to
/// YouTube directly. Its remote fallback, which would send every song to
/// somebody else's server, is never enabled: `methods` is always `[.local]`.
public actor StreamResolver {
    public static let shared = StreamResolver()

    /// Refresh a cached address this long before it expires, not at the edge.
    static let expiryMargin: TimeInterval = 10 * 60
    /// Used when an address carries no `expire`. Upstream addresses live ~6 h.
    static let defaultLifetime: TimeInterval = 60 * 60

    private var cache: [String: ResolvedStream] = [:]
    private var running: [String: Task<ResolvedStream, Error>] = [:]
    /// YouTube's visitor id, asked for once and reused, which the quick
    /// path needs to be answered at all.
    private var visitor: String?

    public init() {}

    public static func isValidId(_ id: String) -> Bool {
        id.range(of: "^[A-Za-z0-9_-]{11}$", options: .regularExpression) != nil
    }

    /// A playable address for `id`, from the cache unless `force` or near expiry.
    public func resolve(_ id: String, force: Bool = false) async throws -> ResolvedStream {
        guard Self.isValidId(id) else { throw PlayerError(PlayerError.invalid, "Not a track this app can play.") }
        let now = Date()
        if !force, let hit = cache[id], hit.expiresAt.timeIntervalSince(now) > Self.expiryMargin { return hit }
        // Two callers asking for the same song (the player and its prefetch)
        // share one extraction rather than racing two.
        if !force, let task = running[id] { return try await task.value }
        let task = Task { try await self.extract(id, now: now) }
        running[id] = task
        defer { running[id] = nil }
        do {
            let stream = try await task.value
            cache[id] = stream
            return stream
        } catch {
            throw PlayerError.classify(error)
        }
    }

    /// Forget an address the source refused, so the next request resolves afresh.
    public func invalidate(_ id: String) {
        cache[id] = nil
    }

    /// The quick path first, the full extraction only when it does not answer.
    private func extract(_ id: String, now: Date) async throws -> ResolvedStream {
        do {
            return try await quickResolve(id, now: now)
        } catch {
            visitor = nil
            return try await Self.fullExtract(id, now: now)
        }
    }

    /// One request, as YouTube's visionOS client, whose addresses are the ones
    /// the full extraction ended up serving anyway (the others are refused).
    /// Measured from a Mac: ~0.3 s, where the full extraction took 5–7 s,
    /// because that also downloads and runs YouTube's web player.
    private func quickResolve(_ id: String, now: Date) async throws -> ResolvedStream {
        let ranked = Self.rankAudio(try await Self.visionCandidates(id, visitor: try await visitorData()))
        guard !ranked.isEmpty else { throw PlayerError(PlayerError.sourceChanged) }
        return try await Self.firstServed(ranked, now: now, isServed: Self.isServed)
    }

    private func visitorData() async throws -> String {
        if let visitor { return visitor }
        var request = URLRequest(url: Self.visitorEndpoint, timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "context": ["client": ["clientName": "WEB", "clientVersion": "2.20260708.00.00", "hl": "en", "gl": "US"]],
        ])
        let (data, _) = try await URLSession.shared.data(for: request)
        let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        guard let raw = (root?["responseContext"] as? [String: Any])?["visitorData"] as? String,
              let value = raw.removingPercentEncoding, !value.isEmpty
        else { throw PlayerError(PlayerError.sourceChanged) }
        visitor = value
        return value
    }

    static let visitorEndpoint = URL(string: "https://www.youtube.com/youtubei/v1/visitor_id?prettyPrint=false")!
    static let playerEndpoint = URL(string: "https://www.youtube.com/youtubei/v1/player?prettyPrint=false")!
    static let visionClient: [String: Any] = [
        "clientName": "VISIONOS", "clientVersion": "1.02", "deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
        "osName": "visionOS", "osVersion": "26.5.23O471", "hl": "en", "gl": "US",
    ]
    static let visionUserAgent =
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"

    static func visionCandidates(_ id: String, visitor: String, session: URLSession = .shared) async throws -> [AudioCandidate] {
        var request = URLRequest(url: playerEndpoint, timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(visionUserAgent, forHTTPHeaderField: "User-Agent")
        request.setValue(visitor, forHTTPHeaderField: "X-Goog-Visitor-Id")
        var client = visionClient
        client["visitorData"] = visitor
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "context": ["client": client], "videoId": id, "contentCheckOk": true, "racyCheckOk": true,
        ])
        let (data, _) = try await session.data(for: request)
        return try parsePlayer(data)
    }

    /// The formats a player answer gives a direct address for. Anything but a
    /// playable answer is a reason to take the long way, not an empty list.
    public static func parsePlayer(_ data: Data) throws -> [AudioCandidate] {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (root["playabilityStatus"] as? [String: Any])?["status"] as? String == "OK",
              let formats = (root["streamingData"] as? [String: Any])?["adaptiveFormats"] as? [[String: Any]]
        else { throw PlayerError(PlayerError.sourceChanged) }
        return formats.compactMap { format in
            guard let raw = format["url"] as? String, let url = URL(string: raw),
                  let mime = format["mimeType"] as? String else { return nil }
            return AudioCandidate(url: url, fileExtension: mime.hasPrefix("audio/mp4") ? "m4a" : "webm",
                                  bitrate: format["averageBitrate"] as? Int ?? format["bitrate"] as? Int ?? 0,
                                  hasAudio: mime.hasPrefix("audio/"), hasVideo: mime.hasPrefix("video/"))
        }
    }

    private static func fullExtract(_ id: String, now: Date) async throws -> ResolvedStream {
        let streams = try await YouTube(videoID: id, methods: [.local]).streams
        let candidates = streams.map {
            AudioCandidate(url: $0.url, fileExtension: $0.fileExtension.rawValue,
                           bitrate: $0.averageBitrate ?? $0.bitrate ?? 0,
                           hasAudio: $0.includesAudioTrack, hasVideo: $0.includesVideoTrack)
        }
        let ranked = rankAudio(candidates)
        guard !ranked.isEmpty else {
            throw PlayerError(PlayerError.unsupported, "This song has no audio this phone can play.")
        }
        return try await firstServed(ranked, now: now, isServed: isServed)
    }

    /// Audio-only AAC, highest bitrate first. AVPlayer cannot play WebM, so
    /// Opus, which Android falls back to, is not an option here.
    public static func rankAudio(_ candidates: [AudioCandidate]) -> [AudioCandidate] {
        candidates
            .filter { $0.hasAudio && !$0.hasVideo && $0.fileExtension == "m4a" && $0.url.scheme == "https" }
            .sorted { $0.bitrate > $1.bitrate }
    }

    /// The best address the source will actually serve. YouTubeKit asks as
    /// several clients at once, and the source can refuse one client's
    /// addresses for a song while serving another's (measured: 403 for
    /// TVHTML5 and 206 for VISIONOS, same song, same minute), so the highest
    /// bitrate alone is not trusted. One refusal rules out that client.
    static func firstServed(_ ranked: [AudioCandidate], now: Date,
                            isServed: (ResolvedStream) async throws -> Bool) async throws -> ResolvedStream {
        var refused = Set<String>()
        for candidate in ranked where !refused.contains(client(of: candidate.url)) {
            let stream = ResolvedStream(url: candidate.url, userAgent: userAgent(for: candidate.url),
                                        expiresAt: expiresAt(candidate.url, now: now))
            if try await isServed(stream) { return stream }
            refused.insert(client(of: candidate.url))
        }
        throw PlayerError(PlayerError.expired)
    }

    /// One byte, asked for the way the player will ask for the rest.
    private static func isServed(_ stream: ResolvedStream) async throws -> Bool {
        var request = URLRequest(url: stream.url, timeoutInterval: 15)
        request.setValue("bytes=0-0", forHTTPHeaderField: "Range")
        request.setValue(stream.userAgent, forHTTPHeaderField: "User-Agent")
        let (_, response) = try await URLSession.shared.data(for: request)
        switch (response as? HTTPURLResponse)?.statusCode ?? 0 {
        case 206: return true
        case 403, 410: return false
        case let status: throw PlayerError.http(status)
        }
    }

    /// The `expire` query parameter, or a conservative default.
    public static func expiresAt(_ url: URL, now: Date) -> Date {
        let value = URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "expire" }?.value
        if let seconds = value.flatMap(TimeInterval.init), seconds > now.timeIntervalSince1970 {
            return Date(timeIntervalSince1970: seconds)
        }
        return now.addingTimeInterval(defaultLifetime)
    }

    /// The client an address was signed for is named in its `c` parameter.
    /// These are the identities YouTubeKit 0.4.10 asks as.
    public static func userAgent(for url: URL) -> String {
        switch client(of: url) {
        case "IOS": return "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)"
        case "IOS_MUSIC": return "com.google.ios.youtubemusic/5.21 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)"
        case "ANDROID_VR":
            return "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
        case "ANDROID": return "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"
        case "ANDROID_MUSIC": return "com.google.android.apps.youtube.music/5.16.51 (Linux; U; Android 11) gzip"
        case "TVHTML5":
            return "Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/2.4.0 TV Safari/538.1"
        default: return safari
        }
    }

    static func client(of url: URL) -> String {
        URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "c" }?.value ?? ""
    }

    static let safari = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15"
}
