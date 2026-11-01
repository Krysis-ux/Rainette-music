import AVFoundation
import Foundation
import MediaPlayer
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

/// The lock screen, Control Center, headphones and the car: what they show,
/// and the buttons they send back. Every button goes through the same engine
/// methods the page uses, so the page and the lock screen always agree.
@MainActor
final class NowPlaying {
    private weak var engine: PlayerEngine?
    private var artworkFor = ""
    private var artwork: MPMediaItemArtwork?

    init(engine: PlayerEngine) {
        self.engine = engine
        let commands = MPRemoteCommandCenter.shared()
        commands.playCommand.addTarget { [weak self] _ in self?.engine?.resume(); return .success }
        commands.pauseCommand.addTarget { [weak self] _ in self?.engine?.pause(); return .success }
        commands.togglePlayPauseCommand.addTarget { [weak self] _ in
            guard let engine = self?.engine else { return .commandFailed }
            if engine.wantsToPlay { engine.pause() } else { engine.resume() }
            return .success
        }
        commands.nextTrackCommand.addTarget { [weak self] _ in self?.engine?.skip(1); return .success }
        commands.previousTrackCommand.addTarget { [weak self] _ in self?.engine?.skip(-1); return .success }
        commands.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            self?.engine?.seek(to: event.positionTime)
            return .success
        }
        #if os(iOS)
        let center = NotificationCenter.default
        center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let info = note.userInfo ?? [:]
            Task { @MainActor in self?.interrupted(info) }
        }
        center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] note in
            let reason = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt).flatMap(AVAudioSession.RouteChangeReason.init)
            Task { @MainActor in
                // Headphones pulled out: the system has already paused the player.
                if reason == .oldDeviceUnavailable { self?.engine?.pausedBySystem() }
            }
        }
        #endif
    }

    /// Ask for the long-lived playback session: plays with the screen locked
    /// and the silent switch on, and is what puts us on the lock screen.
    func activate() {
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default, policy: .longFormAudio)
        try? session.setActive(true)
        #endif
    }

    #if os(iOS)
    private var resumeAfterInterruption = false

    private func interrupted(_ info: [AnyHashable: Any]) {
        guard let raw = info[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw), let engine else { return }
        switch type {
        case .began:
            resumeAfterInterruption = engine.wantsToPlay
            engine.pausedBySystem()
        case .ended:
            let options = (info[AVAudioSessionInterruptionOptionKey] as? UInt).map(AVAudioSession.InterruptionOptions.init)
            if resumeAfterInterruption, options?.contains(.shouldResume) == true { engine.resume() }
            resumeAfterInterruption = false
        @unknown default:
            break
        }
    }
    #endif

    func update(_ song: QueueItem?, _ state: PlayerSnapshot) {
        let center = MPNowPlayingInfoCenter.default()
        guard let song else {
            center.nowPlayingInfo = nil
            return
        }
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: song.title,
            MPMediaItemPropertyArtist: song.artist,
            MPMediaItemPropertyPlaybackDuration: state.durationS,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: state.positionS,
            MPNowPlayingInfoPropertyPlaybackRate: state.playing && !state.loading ? 1.0 : 0.0,
        ]
        if artworkFor == song.thumbnailURL, let artwork { info[MPMediaItemPropertyArtwork] = artwork }
        center.nowPlayingInfo = info
        loadArtwork(for: song.thumbnailURL)
    }

    private func loadArtwork(for address: String) {
        guard address != artworkFor else { return }
        artworkFor = address
        artwork = nil
        guard address.hasPrefix("https://"), let url = URL(string: address) else { return }
        Task { [weak self] in
            guard let (data, _) = try? await URLSession.shared.data(from: url) else { return }
            #if canImport(UIKit)
            guard let image = UIImage(data: data) else { return }
            #else
            guard let image = NSImage(data: data) else { return }
            #endif
            guard let self, self.artworkFor == address else { return }
            self.artwork = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
            if var info = MPNowPlayingInfoCenter.default().nowPlayingInfo {
                info[MPMediaItemPropertyArtwork] = self.artwork
                MPNowPlayingInfoCenter.default().nowPlayingInfo = info
            }
        }
    }
}
