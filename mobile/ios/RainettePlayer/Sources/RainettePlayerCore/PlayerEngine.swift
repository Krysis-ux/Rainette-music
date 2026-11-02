import AVFoundation
import Foundation
import Network

/// One song in the queue, as the page names it. Never a URL to play.
public struct QueueItem: Sendable, Equatable {
    public let sourceId: String
    public let title: String
    public let artist: String
    public let thumbnailURL: String
    public let durationS: Double?

    public init(sourceId: String, title: String, artist: String, thumbnailURL: String, durationS: Double?) {
        self.sourceId = sourceId
        self.title = title
        self.artist = artist
        self.thumbnailURL = thumbnailURL
        self.durationS = durationS
    }

    public var json: [String: Any] {
        ["source": "youtube", "source_id": sourceId, "title": title, "artist": artist, "thumbnail_url": thumbnailURL]
    }
}

public enum RepeatMode: String, Sendable {
    case off, all, one
}

/// What the page is told, field for field what the Android plugin sends.
public struct PlayerSnapshot: Sendable, Equatable {
    public var generation = 0
    public var playing = false
    public var loading = false
    public var ended = false
    public var index = -1
    public var sourceId = ""
    public var positionS = 0.0
    public var durationS = 0.0
    public var count = 0

    public var json: [String: Any] {
        ["generation": generation, "playing": playing, "loading": loading, "ended": ended, "index": index,
         "source_id": sourceId, "position_s": positionS, "duration_s": durationS, "count": count]
    }
}

/// The on-device player: a queue of songs played by AVPlayer, which keeps going
/// with the screen off and the page asleep, the way the Android app's media
/// service does. The page drives it through `RainettePlayerPlugin`; the lock
/// screen and headphones drive it through `NowPlaying`. Both go through these
/// same methods, so they always agree.
///
/// Every play request carries the page's `generation`, echoed on every state,
/// so a slow answer for a song the page has moved past is dropped there.
@MainActor
public final class PlayerEngine {
    public static let maxQueue = 1000
    static let playTimeout: UInt64 = 60
    /// Previous restarts the song once it is further in than this.
    static let restartAfter = 3.0

    public var onState: (PlayerSnapshot) -> Void = { _ in }
    public var onError: (PlayerError, PlayerSnapshot) -> Void = { _, _ in }

    public let player = AVPlayer()
    public private(set) var queue: [QueueItem] = []
    public private(set) var index = -1
    public private(set) var generation = 0
    public private(set) var repeatMode = RepeatMode.off
    public private(set) var wantsToPlay = false
    public private(set) var ended = false

    private let resolver: StreamResolver
    private var item: AVPlayerItem?
    private var loader: ChunkedLoader?
    private var startAt: Double?
    private var itemWatch: [NSKeyValueObservation] = []
    private var playerWatch: [NSKeyValueObservation] = []
    private var pending: Pending?
    private var tick: Timer?
    private let network = NWPathMonitor()
    private var online = true
    lazy var nowPlaying = NowPlaying(engine: self)

    private struct Pending {
        let generation: Int
        let sourceId: String
        let continuation: CheckedContinuation<PlayerSnapshot, Error>
    }

    public init(resolver: StreamResolver = .shared) {
        self.resolver = resolver
        playerWatch.append(player.observe(\.timeControlStatus) { [weak self] _, _ in
            Task { @MainActor in self?.changed() }
        })
        NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            let finished = note.object as AnyObject?
            Task { @MainActor in self?.itemEnded(finished) }
        }
        network.pathUpdateHandler = { [weak self] path in
            let up = path.status == .satisfied
            Task { @MainActor in self?.networkChanged(online: up) }
        }
        network.start(queue: .main)
        _ = nowPlaying
    }

    // MARK: - What the page asks for

    /// Replace the queue and start (or prepare) `index`. Returns once that song
    /// has loaded, which is what a desktop handoff waits on before pausing
    /// the computer.
    public func play(_ items: [QueueItem], at start: Int, from position: Double = 0,
                     startPaused: Bool = false, generation requested: Int) async throws -> PlayerSnapshot {
        guard !items.isEmpty, items.count <= Self.maxQueue, items.indices.contains(start) else {
            throw PlayerError(PlayerError.invalid, "That queue cannot be played.")
        }
        guard items.allSatisfy({ StreamResolver.isValidId($0.sourceId) }) else {
            throw PlayerError(PlayerError.invalid, "Only streamed songs can play here.")
        }
        settlePending(.failure(PlayerError(PlayerError.canceled, "A newer track replaced this one.")))
        generation = requested
        queue = items
        wantsToPlay = !startPaused
        load(start, from: max(0, position))
        let sourceId = items[start].sourceId
        return try await withCheckedThrowingContinuation { continuation in
            pending = Pending(generation: requested, sourceId: sourceId, continuation: continuation)
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: Self.playTimeout * 1_000_000_000)
                guard let self, self.pending?.generation == requested else { return }
                self.settlePending(.failure(PlayerError(PlayerError.offline, "The song took too long to start.")))
            }
        }
    }

    /// Swap everything around the current song without interrupting it. Refused
    /// when the current song is not where the page thinks it is, so the page
    /// falls back to a fresh `play` instead of the two queues drifting apart.
    public func setQueue(_ items: [QueueItem], at position: Int, generation requested: Int?) throws -> PlayerSnapshot {
        guard items.count <= Self.maxQueue, items.indices.contains(position),
              queue.indices.contains(index), items[position].sourceId == queue[index].sourceId else {
            throw PlayerError(PlayerError.mismatch, "The queue moved on.")
        }
        queue = items
        index = position
        if let requested { generation = requested }
        prefetchNext()
        return changed()
    }

    @discardableResult
    public func resume() -> PlayerSnapshot {
        guard queue.indices.contains(index) else { return snapshot() }
        wantsToPlay = true
        activateSession()
        if item?.status == .failed {
            load(index, from: seconds())
        } else {
            if ended {
                ended = false
                player.seek(to: .zero)
            }
            player.play()
        }
        return changed()
    }

    @discardableResult
    public func pause() -> PlayerSnapshot {
        wantsToPlay = false
        player.pause()
        return changed()
    }

    @discardableResult
    public func stop() -> PlayerSnapshot {
        settlePending(.failure(PlayerError(PlayerError.canceled, "Playback moved elsewhere.")))
        wantsToPlay = false
        dropItem()
        queue = []
        index = -1
        ended = false
        return changed()
    }

    @discardableResult
    public func seek(to position: Double) -> PlayerSnapshot {
        ended = false
        let time = CMTime(seconds: max(0, position), preferredTimescale: 1000)
        if item?.status == .readyToPlay {
            player.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
                Task { @MainActor in self?.changed() }
            }
        } else {
            startAt = max(0, position)
        }
        return changed()
    }

    /// Next, or previous. Previous restarts a song more than three seconds in,
    /// and repeat-all wraps, as on the notification of every phone player.
    @discardableResult
    public func skip(_ offset: Int) -> PlayerSnapshot {
        guard queue.indices.contains(index) else { return snapshot() }
        if offset > 0 {
            if index + 1 < queue.count { load(index + 1) } else if repeatMode == .all { load(0) }
        } else if seconds() > Self.restartAfter || (index == 0 && repeatMode != .all) {
            return seek(to: 0)
        } else {
            load(index > 0 ? index - 1 : queue.count - 1)
        }
        return changed()
    }

    @discardableResult
    public func setRepeat(_ mode: RepeatMode) -> PlayerSnapshot {
        repeatMode = mode
        return changed()
    }

    @discardableResult
    public func setVolume(_ volume: Double) -> PlayerSnapshot {
        player.volume = Float(min(1, max(0, volume)))
        return changed()
    }

    public func snapshot() -> PlayerSnapshot {
        let current = queue.indices.contains(index) ? queue[index] : nil
        let itemLength = item?.duration.seconds ?? .nan
        let ready = item?.status == .readyToPlay
        let waiting = player.timeControlStatus == .waitingToPlayAtSpecifiedRate
        var state = PlayerSnapshot()
        state.generation = generation
        state.index = current == nil ? -1 : index
        state.sourceId = current?.sourceId ?? ""
        state.count = queue.count
        state.ended = ended
        state.playing = current != nil && wantsToPlay && !ended && item?.status != .failed
        state.loading = current != nil && !ended && item?.status != .failed && (!ready || (wantsToPlay && waiting))
        state.positionS = current == nil ? 0 : seconds()
        state.durationS = itemLength.isFinite && itemLength > 0 ? itemLength : (current?.durationS ?? 0)
        return state
    }

    // MARK: - The song in the player

    private func load(_ at: Int, from position: Double = 0) {
        // A play still waiting on another song is over: the queue moved on.
        // Left waiting, it would only ever settle with the new song's failure
        // or the timeout, and the page would show it loading until then.
        if let waiting = pending, waiting.sourceId != queue[at].sourceId {
            settlePending(.failure(PlayerError(PlayerError.canceled, "Skipped before it started.")))
        }
        dropItem()
        index = at
        ended = false
        startAt = position > 0 ? position : nil
        let song = queue[at]
        let loader = ChunkedLoader(id: song.sourceId, resolver: resolver)
        let asset = AVURLAsset(url: ChunkedLoader.url(for: song.sourceId))
        asset.resourceLoader.setDelegate(loader, queue: loader.queue)
        let item = AVPlayerItem(asset: asset)
        self.loader = loader
        self.item = item
        itemWatch = [item.observe(\.status) { [weak self] observed, _ in
            Task { @MainActor in self?.statusChanged(observed) }
        }]
        player.replaceCurrentItem(with: item)
        if wantsToPlay {
            activateSession()
            player.play()
        } else {
            player.pause()
        }
        prefetchNext()
        changed()
    }

    private func dropItem() {
        itemWatch = []
        loader?.cancelAll()
        loader = nil
        item = nil
        player.replaceCurrentItem(with: nil)
    }

    private func statusChanged(_ observed: AVPlayerItem) {
        guard observed === item else { return }
        switch observed.status {
        case .readyToPlay:
            if let start = startAt {
                startAt = nil
                player.seek(to: CMTime(seconds: start, preferredTimescale: 1000), toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
                    Task { @MainActor in self?.loaded() }
                }
            } else {
                loaded()
            }
        case .failed:
            let reason = loader?.reason() ?? PlayerError.classify(observed.error ?? PlayerError(PlayerError.serviceFailed))
            if pending != nil {
                settlePending(.failure(reason))
            } else {
                onError(reason, snapshot())
            }
            changed()
        default:
            break
        }
    }

    private func loaded() {
        let state = changed()
        if let waiting = pending, waiting.generation == generation, waiting.sourceId == state.sourceId {
            settlePending(.success(state))
        }
    }

    private func itemEnded(_ finished: AnyObject?) {
        guard let finished, finished === item else { return }
        if repeatMode == .one {
            player.seek(to: .zero)
            player.play()
        } else if index + 1 < queue.count {
            load(index + 1)
        } else if repeatMode == .all {
            load(0)
        } else {
            ended = true
            wantsToPlay = false
            changed()
        }
    }

    /// Back online: whatever stalled or failed meanwhile is asked for again at
    /// once, rather than at the loader's next backoff step.
    private func networkChanged(online up: Bool) {
        let wasOffline = !online
        online = up
        guard up, wasOffline, item != nil, queue.indices.contains(index) else { return }
        if item?.status == .failed || player.timeControlStatus == .waitingToPlayAtSpecifiedRate {
            load(index, from: seconds())
        }
    }

    /// Warm the next song: its address, and its first megabyte in the song
    /// cache, so a skip or the end of this one starts it at once.
    private func prefetchNext() {
        guard index + 1 < queue.count else { return }
        let next = queue[index + 1].sourceId
        let resolver = self.resolver
        Task.detached { await ChunkedLoader(id: next, resolver: resolver).warm() }
    }

    private func seconds() -> Double {
        let now = player.currentTime().seconds
        return now.isFinite ? max(0, now) : (startAt ?? 0)
    }

    private func settlePending(_ result: Result<PlayerSnapshot, Error>) {
        guard let waiting = pending else { return }
        pending = nil
        waiting.continuation.resume(with: result)
    }

    // MARK: - Telling everyone

    @discardableResult
    private func changed() -> PlayerSnapshot {
        let state = snapshot()
        onState(state)
        nowPlaying.update(queue.indices.contains(index) ? queue[index] : nil, state)
        scheduleTick(state.playing && !state.loading)
        return state
    }

    /// While playing, the page and the lock screen hear the position once a second.
    private func scheduleTick(_ moving: Bool) {
        guard moving != (tick != nil) else { return }
        tick?.invalidate()
        tick = moving ? Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.changed() }
        } : nil
    }

    private func activateSession() {
        nowPlaying.activate()
    }

    /// The system paused us (a call, headphones pulled out): the page must not
    /// go on showing a pause button for music that has stopped.
    func pausedBySystem() {
        wantsToPlay = false
        changed()
    }
}
