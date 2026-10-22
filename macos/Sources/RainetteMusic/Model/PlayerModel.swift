import Foundation
import Observation

/// The transport as this app sees it, and every command it can send.
@available(macOS 14, *)
@Observable @MainActor
final class PlayerModel {
    var queue: [Track] = []
    var index: Int = -1
    var state: PlaybackState = .idle
    var isPlaying = false
    var repeatMode: RepeatMode = .off
    var errorReason = ""
    var duration: Double = 0
    private(set) var position: Double = 0
    private(set) var positionStamp = Date()
    var volume: Double = 1

    /// Who owns the audio (music_playback_target), as the server records it.
    var ownerIsDesktop = true
    var ownerName = ""
    private var ownerRevision = 0
    var transferPending = false

    var eqOn = false
    var eqGains: [Double] = Array(repeating: 0, count: 5)

    var sleepTimerEnd: Date?
    var sleepAtEndOfTrack = false

    @ObservationIgnored weak var app: AppModel?
    @ObservationIgnored private var lastPlay: (tracks: [Track], index: Int, restoreState: String)?
    @ObservationIgnored private var sessionSaveTask: Task<Void, Never>?
    @ObservationIgnored private var sleepTask: Task<Void, Never>?
    @ObservationIgnored private var lastTrackKey: String?
    @ObservationIgnored private var lastErrorKey: String?
    @ObservationIgnored private var eqEditedAt = Date.distantPast

    var current: Track? { queue.indices.contains(index) ? queue[index] : nil }
    var hasTrack: Bool { current != nil }
    var showsPause: Bool { isPlaying || state == .loading }
    var upNext: ArraySlice<Track> { index + 1 < queue.count ? queue[(index + 1)...] : [] }

    func livePosition(at date: Date = Date()) -> Double {
        guard isPlaying else { return position }
        let advanced = position + date.timeIntervalSince(positionStamp)
        return duration > 0 ? min(duration, advanced) : advanced
    }

    func isCurrent(_ track: Track) -> Bool { current?.key == track.key }

    private func send(_ type: String, _ fields: JSONObject = [:]) { app?.client?.send(type, fields) }
    private func control(_ action: String, _ fields: JSONObject = [:]) {
        var message = fields
        message["action"] = .string(action)
        send("music_remote_control", message)
    }

    // MARK: Commands

    func play(_ tracks: [Track], startingAt start: Int = 0) {
        let playable = tracks.filter(\.isPlayable)
        guard !playable.isEmpty else { return }
        let startTrack = tracks.indices.contains(start) ? tracks[start] : playable[0]
        let startIndex = playable.firstIndex(of: startTrack) ?? 0
        lastPlay = (playable, startIndex, "loading")
        // Optimistic: the bar shows the song the instant it is pressed.
        queue = playable
        index = startIndex
        state = .loading
        isPlaying = false
        setPosition(0)
        duration = playable[startIndex].duration
        send("music_remote_play", ["tracks": .array(playable.map(\.raw)), "index": .number(Double(startIndex))])
    }

    func shuffle(_ tracks: [Track]) {
        play(tracks.shuffled(), startingAt: 0)
    }

    /// Press on a track that may already be the current one.
    func playOrToggle(_ track: Track, in list: [Track]) {
        if isCurrent(track), state != .error { togglePlayPause(); return }
        play(list.isEmpty ? [track] : list, startingAt: list.firstIndex(of: track) ?? 0)
    }

    func togglePlayPause() {
        guard hasTrack else { return }
        if showsPause { pause() } else { resume() }
    }

    func pause() {
        let at = livePosition()   // before isPlaying stops the clock
        isPlaying = false
        if state == .playing || state == .loading { state = .paused }
        setPosition(at)
        control("pause")
    }

    func resume() {
        guard hasTrack else { return }
        isPlaying = true
        state = .playing
        setPosition(position)
        control("play")
    }

    func next() { control("next") }
    func previous() { control("prev") }

    func seek(toFraction fraction: Double) {
        let clamped = min(max(fraction, 0), 1)
        if duration > 0 { setPosition(clamped * duration) }
        control("seek", ["ratio": .number(clamped)])
    }

    func cycleRepeat() { setRepeat(repeatMode.next) }

    func setRepeat(_ mode: RepeatMode) {
        repeatMode = mode
        control("set_repeat", ["mode": .string(mode.rawValue)])
    }

    func setVolume(_ value: Double) {
        volume = min(max(value, 0), 1.5)
        app?.audio.setStorage("rw.mp.volume", String(volume))
        control("set_volume", ["value": .number(volume)])
    }

    func playNext(_ track: Track) {
        if !hasTrack { play([track]); return }
        control("queue_add_next", ["track": track.raw])
    }

    func addToQueue(_ track: Track) {
        if !hasTrack { play([track]); return }
        control("queue_add_end", ["track": track.raw])
    }

    func playQueueIndex(_ position: Int) { control("queue_play_index", ["index": .number(Double(position))]) }
    func removeFromQueue(_ position: Int) { control("queue_remove", ["index": .number(Double(position))]) }
    func moveInQueue(from: Int, to: Int) { control("queue_move", ["from": .number(Double(from)), "to": .number(Double(to))]) }
    func shuffleUpNext() { control("queue_shuffle") }
    func clearUpNext() { control("queue_clear_up_next") }

    /// The player page's own range and presets (EQ_MIN/EQ_MAX, PRESETS).
    static let eqRange: ClosedRange<Double> = -12...12
    static let eqPresets: [String: [Double]] = [
        "Flat": [0, 0, 0, 0, 0], "Bass Boost": [8, 4, 0, 0, 1],
        "Vocal": [-2, 0, 4, 3, 1], "Treble": [0, 0, 0, 4, 8],
    ]

    func setEQ(on: Bool) { eqOn = on; control("eq_set_on", ["on": .bool(on)]) }

    /// Moving a band turns the EQ on, as it does in the player page.
    func setEQBand(_ band: Int, gain: Double) {
        guard eqGains.indices.contains(band) else { return }
        eqGains[band] = gain
        eqOn = true
        eqEditedAt = Date()
        control("eq_set_band", ["index": .number(Double(band)), "gain": .number(gain)])
    }

    func applyEQPreset(_ name: String) {
        if let gains = Self.eqPresets[name] {
            eqGains = gains
            eqOn = true
            eqEditedAt = Date()
        }
        control("eq_apply_preset", ["preset": .string(name)])
    }

    func requestState() {
        control("queue_request_state")
        control("eq_request_state")
        send("music_playback_target_get", ["id": .string("pt_\(UUID().uuidString.prefix(6))")])
    }

    // MARK: Sleep timer

    func startSleepTimer(minutes: Int) {
        cancelSleepTimer()
        let end = Date().addingTimeInterval(TimeInterval(minutes * 60))
        sleepTimerEnd = end
        sleepTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(minutes) * 60 * 1_000_000_000)
            guard let self, !Task.isCancelled else { return }
            self.pause()
            self.sleepTimerEnd = nil
        }
    }

    func sleepAtEndOfCurrentTrack() {
        cancelSleepTimer()
        sleepAtEndOfTrack = true
    }

    func cancelSleepTimer() {
        sleepTask?.cancel()
        sleepTask = nil
        sleepTimerEnd = nil
        sleepAtEndOfTrack = false
    }

    // MARK: Hand-off

    /// Move playback to a paired phone.
    func transfer(toPhone deviceID: String, name: String) async -> String? {
        guard let client = app?.client, current != nil else { return "Start a song before moving playback to your phone." }
        guard !transferPending else { return nil }
        transferPending = true
        defer { transferPending = false }
        let wasPlaying = isPlaying
        let reply = await client.request("music_output_transfer", [
            "source_device_id": "desktop",
            "target_device_id": .string(deviceID),
            "queue": .array(queue.map(\.raw)),
            "index": .number(Double(max(0, index))),
            "current_time": .number(livePosition()),
            "playing": .bool(wasPlaying),
            "repeat": .string(repeatMode.rawValue),
            "loop": .bool(repeatMode != .off),
        ], timeout: 35)
        guard reply["ok"].bool else {
            return reply["msg"].string ?? "The phone did not accept playback in time."
        }
        if wasPlaying { control("pause", ["reason": "output_transfer"]) }
        ownerIsDesktop = false
        ownerName = name
        return nil
    }

    // MARK: Incoming

    func handle(_ message: JSON) {
        switch message["type"].text {
        case "music_now_playing": applyNowPlaying(message)
        case "music_progress": applyProgress(message)
        case "music_request_state": answerStateRequest()
        case "music_eq_state":
            eqOn = message["on"].bool
            let gains = message["gains"].array.compactMap(\.double)
            if gains.count == eqGains.count, Date().timeIntervalSince(eqEditedAt) > 0.6 { eqGains = gains }
        case "music_playback_target", "music_playback_target_result": applyTarget(message)
        case "music_remote_control":
            if message["action"].text == "set_volume", let value = message["value"].double { volume = min(max(value, 0), 1.5) }
        default: break
        }
    }

    private func applyNowPlaying(_ message: JSON) {
        let incomingState = PlaybackState(rawValue: message["state"].text) ?? (message["playing"].bool ? .playing : .paused)
        if case .array(let raw) = message["queue"] {
            // A cold engine reload answers `idle` with nothing in it before it has asked for the queue.
            if raw.isEmpty, incomingState == .idle, !(lastPlay?.tracks.isEmpty ?? true) { return }
            let tracks = raw.map(Track.init)
            queue = tracks
            index = message["index"].int ?? -1
            if tracks.isEmpty { lastPlay = nil }
            else {
                let restore = [.playing, .paused, .loading].contains(incomingState) ? incomingState.rawValue : (message["playing"].bool ? "playing" : "paused")
                lastPlay = (tracks, max(0, index), restore)
            }
            scheduleSessionSave()
        }
        // Absent means "no opinion" (the phone has no repeat control), not off.
        if let mode = RepeatMode(rawValue: message["repeat"].text) { repeatMode = mode }
        else if case .bool(let loop) = message["loop"] { repeatMode = loop ? .all : .off }
        let enteringError = incomingState == .error && (state != .error || current?.key != lastErrorKey)
        state = incomingState
        isPlaying = message["playing"].bool
        errorReason = message["error_reason"].text
        let reportedDuration = message["duration"].double ?? 0
        duration = reportedDuration > 0 ? reportedDuration : (current?.duration ?? 0)
        setPosition(message["current_time"].double ?? 0)

        let key = current?.key
        if key != lastTrackKey {
            let hadTrack = lastTrackKey != nil
            lastTrackKey = key
            if sleepAtEndOfTrack, hadTrack, key != nil {
                sleepAtEndOfTrack = false
                pause()
            }
        }
        if enteringError, let track = current {
            lastErrorKey = track.key
            app?.toasts.show(errorReason.isEmpty ? "Couldn't play “\(track.title)”." : "Couldn't play “\(track.title)” — \(errorReason)",
                             symbol: "exclamationmark.triangle.fill", style: .error)
        }
        if incomingState != .error { lastErrorKey = nil }
    }

    private func applyProgress(_ message: JSON) {
        let sourceID = message["source_id"].text
        if !sourceID.isEmpty, let current, current.sourceID != sourceID { return }
        let reported = message["duration"].double ?? 0
        if reported > 0 { duration = reported }
        let playing = message["playing"].bool
        if current != nil {
            isPlaying = playing
            if playing { state = .playing } else if state == .playing { state = .paused }
        }
        setPosition(message["current_time"].double ?? 0)
    }

    private func applyTarget(_ message: JSON) {
        let revision = message["revision"].int ?? 0
        if revision != 0, revision <= ownerRevision { return }
        ownerRevision = revision
        ownerIsDesktop = (message["owner_kind"].string ?? "desktop") == "desktop"
        ownerName = ownerIsDesktop ? message["sink_name"].text : (message["owner_name"].string ?? "a phone")
    }

    /// The engine page (re)started and asked for the queue it should hold.
    private func answerStateRequest() {
        guard let lastPlay, !lastPlay.tracks.isEmpty else { return }
        send("music_remote_play", [
            "tracks": .array(lastPlay.tracks.map(\.raw)),
            "index": .number(Double(lastPlay.index)),
            "restore_state": .string(lastPlay.restoreState),
            "playing": .bool(lastPlay.restoreState == "playing"),
        ])
    }

    private func setPosition(_ value: Double) {
        position = max(0, value)
        positionStamp = Date()
    }

    /// Keep "Last session" current so a relaunch can pick up where this left off.
    private func scheduleSessionSave() {
        sessionSaveTask?.cancel()
        let tracks = queue.filter(\.isPlayable)
        guard !tracks.isEmpty else { return }
        let at = max(0, index)
        sessionSaveTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 900_000_000)
            guard let self, !Task.isCancelled else { return }
            self.send("music_queue_session_save", [
                "id": .string("qsl_\(UUID().uuidString.prefix(8))"),
                "name": "Last session",
                "tracks": .array(tracks.map(\.raw)),
                "index": .number(Double(at)),
                "is_last": true,
            ])
        }
    }
}
