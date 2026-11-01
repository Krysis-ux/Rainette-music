import Foundation
import YouTubeKit

/// A failure, carrying one of the codes the page knows how to word
/// (`pwa/src/source-provider.js`). They are the same codes the Android app
/// sends, so the shared interface never needs to know which phone it is on.
public struct PlayerError: Error, Equatable, Sendable, LocalizedError {
    public let code: String
    public let message: String

    public static let offline = "offline"
    public static let unavailable = "unavailable"
    public static let region = "region"
    public static let restricted = "restricted"
    public static let rateLimited = "rate_limited"
    public static let unsupported = "unsupported"
    public static let sourceChanged = "source_changed"
    public static let searchFailed = "search_failed"
    public static let invalid = "invalid"
    public static let expired = "expired"
    public static let canceled = "canceled"
    public static let mismatch = "mismatch"
    public static let serviceFailed = "service_failed"

    public init(_ code: String, _ message: String? = nil) {
        self.code = code
        self.message = message ?? PlayerError.wording(code)
    }

    public var errorDescription: String? { message }

    public static func wording(_ code: String) -> String {
        switch code {
        case offline: return "No internet connection."
        case expired: return "The stream expired and could not be renewed."
        case unavailable: return "This song is no longer available."
        case region: return "This song isn't available in your country."
        case restricted: return "This song needs a signed-in account to play."
        case rateLimited: return "The source is asking this phone to slow down. Try again shortly."
        case unsupported: return "This phone can't play this song's format."
        case searchFailed: return "Search did not answer."
        case invalid: return "That can't be played here."
        default: return "Playback stopped unexpectedly."
        }
    }

    /// Anything thrown on the way to a song, as one of the codes above. The
    /// type alone is not trusted for "unavailable": an id YouTube does not know
    /// often fails as a parse error, which is reported as `source_changed`.
    public static func classify(_ error: Error) -> PlayerError {
        if let own = error as? PlayerError { return own }
        if error is CancellationError { return PlayerError(canceled) }
        if let source = error as? YouTubeKitError {
            switch source {
            case .videoUnavailable, .videoPrivate, .recordingUnavailable, .liveStreamError:
                return PlayerError(unavailable)
            case .videoAgeRestricted, .membersOnly:
                return PlayerError(restricted)
            case .videoRegionBlocked:
                return PlayerError(region)
            default:
                return PlayerError(sourceChanged, "This song couldn't be read from the source.")
            }
        }
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            return ns.code == NSURLErrorCancelled ? PlayerError(canceled) : PlayerError(offline)
        }
        return PlayerError(sourceChanged, "This song couldn't be read from the source.")
    }

    /// An HTTP status from the source, as a code.
    public static func http(_ status: Int) -> PlayerError {
        switch status {
        case 429: return PlayerError(rateLimited)
        case 403, 410: return PlayerError(expired)
        case 404: return PlayerError(unavailable)
        default: return PlayerError(offline, "The connection dropped.")
        }
    }
}
