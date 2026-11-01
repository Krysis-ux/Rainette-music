import AVFoundation
import Foundation

/// Serves one song to AVPlayer, in bounded ranges.
///
/// AVPlayer asks for "the rest of the file" in one open-ended request, and the
/// source throttles exactly that: measured at 36 KB/s for a whole track, against
/// ~4 MB/s in 1 MB ranges (mobile/AUDIT.md §6). So the player is handed a
/// private address (`rainette-yt://youtube/<id>`) it cannot fetch itself, and
/// this answers its requests a megabyte at a time, the way the Android app's
/// ChunkedDataSource does.
///
/// It also owns the source's failures: an address that expires (403/410) is
/// resolved afresh at most twice, and a lost connection is retried with backoff
/// (2, 4, 8, 16, 30 s) before the song is given up on. Meanwhile AVPlayer just
/// waits, which reads as buffering, the honest state.
public final class ChunkedLoader: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    public static let scheme = "rainette-yt"
    static let chunkBytes: Int64 = 1 << 20
    static let backoff: [UInt64] = [2, 4, 8, 16, 30]
    static let maxRefreshes = 2

    public let id: String
    public let queue = DispatchQueue(label: "rainette.loader")
    private let resolver: StreamResolver
    private let session: URLSession
    /// Loader queue only.
    private var tasks: [ObjectIdentifier: Task<Void, Never>] = [:]
    private var length: Int64?
    /// Why the song could not be served, for the player to report; AVPlayer
    /// itself only ever says "the operation could not be completed". Loader
    /// queue only; read it through `reason()`.
    private var failure: PlayerError?

    public init(id: String, resolver: StreamResolver = .shared, session: URLSession = .shared) {
        self.id = id
        self.resolver = resolver
        self.session = session
    }

    public static func url(for id: String) -> URL {
        URL(string: "\(scheme)://youtube/\(id)")!
    }

    public func resourceLoader(_ resourceLoader: AVAssetResourceLoader,
                               shouldWaitForLoadingOfRequestedResource request: AVAssetResourceLoadingRequest) -> Bool {
        let key = ObjectIdentifier(request)
        tasks[key] = Task { [weak self] in
            await self?.serve(request)
            self?.queue.async { self?.tasks[key] = nil }
        }
        return true
    }

    public func resourceLoader(_ resourceLoader: AVAssetResourceLoader,
                               didCancel request: AVAssetResourceLoadingRequest) {
        tasks.removeValue(forKey: ObjectIdentifier(request))?.cancel()
    }

    public func reason() -> PlayerError? {
        queue.sync { failure }
    }

    /// Stop every request in flight; the item is being thrown away.
    public func cancelAll() {
        queue.async { [self] in
            tasks.values.forEach { $0.cancel() }
            tasks.removeAll()
        }
    }

    private func serve(_ request: AVAssetResourceLoadingRequest) async {
        let wanted = request.dataRequest
        var offset = wanted.map { $0.currentOffset != 0 ? $0.currentOffset : $0.requestedOffset } ?? 0
        let end: Int64? = wanted.map { $0.requestsAllDataToEndOfResource ? nil : $0.requestedOffset + Int64($0.requestedLength) } ?? 2
        do {
            while true {
                try Task.checkCancellation()
                if let total = length, offset >= total { break }
                if let end, offset >= end { break }
                var upTo = offset + Self.chunkBytes
                if let end { upTo = min(upTo, end) }
                if let total = length { upTo = min(upTo, total) }

                let (bytes, total) = try await fetch(from: offset, through: upTo - 1)
                try Task.checkCancellation()
                queue.sync {
                    if length == nil { length = total }
                    if let info = request.contentInformationRequest, info.contentLength == 0 {
                        info.contentType = AVFileType.m4a.rawValue
                        info.contentLength = total
                        info.isByteRangeAccessSupported = true
                    }
                    if !bytes.isEmpty { request.dataRequest?.respond(with: bytes) }
                }
                if bytes.isEmpty || wanted == nil { break }
                offset += Int64(bytes.count)
            }
            queue.async { request.finishLoading() }
        } catch is CancellationError {
            // AVPlayer withdrew the request; it is not waiting on an answer.
        } catch {
            let reason = PlayerError.classify(error)
            if reason.code == PlayerError.canceled { return }
            queue.async { [self] in
                failure = reason
                request.finishLoading(with: reason)
            }
        }
    }

    /// Bytes `from...through` and the whole file's length.
    private func fetch(from: Int64, through: Int64) async throws -> (Data, Int64) {
        var refreshes = 0
        var drops = 0
        while true {
            try Task.checkCancellation()
            let stream = try await resolver.resolve(id, force: refreshes > 0)
            var request = URLRequest(url: stream.url, timeoutInterval: 15)
            request.setValue("bytes=\(from)-\(through)", forHTTPHeaderField: "Range")
            request.setValue(stream.userAgent, forHTTPHeaderField: "User-Agent")
            do {
                let (data, response) = try await session.data(for: request)
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                switch status {
                case 206:
                    let range = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Range")
                    guard let total = Self.totalLength(range) else { throw PlayerError(PlayerError.sourceChanged) }
                    return (data, total)
                case 200:
                    // The range was ignored and the whole file came back.
                    let total = Int64(data.count)
                    guard from < total else { return (Data(), total) }
                    return (data.subdata(in: Int(from)..<Int(min(through + 1, total))), total)
                case 416:
                    return (Data(), length ?? from)
                case 403, 410:
                    guard refreshes < Self.maxRefreshes else { throw PlayerError(PlayerError.expired) }
                    refreshes += 1
                    // A song with an advert in front of it is refused for the
                    // advert's length, so the next ask waits a moment first.
                    try await Task.sleep(nanoseconds: 2_000_000_000)
                    await resolver.invalidate(id)
                    continue
                default:
                    throw PlayerError.http(status)
                }
            } catch let error as URLError where error.code != .cancelled && drops < Self.backoff.count {
                try await Task.sleep(nanoseconds: Self.backoff[drops] * 1_000_000_000)
                drops += 1
            }
        }
    }

    /// The total in "bytes 0-1023/5242880".
    static func totalLength(_ contentRange: String?) -> Int64? {
        guard let total = contentRange?.split(separator: "/").last else { return nil }
        return Int64(total)
    }
}
