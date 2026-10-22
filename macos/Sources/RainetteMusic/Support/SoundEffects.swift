import AppKit
import AVFoundation

/// Quiet interface sounds, off unless turned on in Settings.
@MainActor
final class SoundEffects {
    enum Effect: CaseIterable { case tap, play, pause, skip, confirm, open }

    static let shared = SoundEffects()
    static let enabledKey = "soundEffectsEnabled"
    static let volumeKey = "soundEffectsVolume"

    private let engine = AVAudioEngine()
    private var players: [AVAudioPlayerNode] = []
    private var buffers: [Effect: AVAudioPCMBuffer] = [:]
    private var nextPlayer = 0
    private let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1)!

    private var enabled: Bool { UserDefaults.standard.bool(forKey: Self.enabledKey) }
    private var volume: Float {
        let stored = UserDefaults.standard.object(forKey: Self.volumeKey) as? Double ?? 0.6
        return Float(min(max(stored, 0), 1))
    }

    private init() {
        for _ in 0..<4 {
            let player = AVAudioPlayerNode()
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: format)
            players.append(player)
        }
        for effect in Effect.allCases { buffers[effect] = render(effect) }
        let center = NotificationCenter.default
        center.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in SoundEffects.shared.warm() }
        }
        center.addObserver(forName: NSApplication.didResignActiveNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in SoundEffects.shared.engine.pause() }
        }
    }

    func warm() {
        guard enabled, !engine.isRunning else { return }
        try? engine.start()
    }

    func play(_ effect: Effect, force: Bool = false) {
        guard enabled || force, let buffer = buffers[effect] else { return }
        if !engine.isRunning { try? engine.start() }
        guard engine.isRunning else { return }
        let player = players[nextPlayer]
        nextPlayer = (nextPlayer + 1) % players.count
        player.volume = volume * 0.55
        player.stop()
        player.scheduleBuffer(buffer, at: nil, options: [])
        player.play()
    }

    // MARK: Synthesis

    private struct Partial {
        var frequency: Double
        var glide: Double = 0          // relative pitch change over `glideTime`
        var glideTime: Double = 0.05
        var amplitude: Double
        var delay: Double = 0
        var attack: Double = 0.002
        var decay: Double              // exponential time constant, seconds
    }

    func render(_ effect: Effect) -> AVAudioPCMBuffer {
        let partials: [Partial]
        var noise: (amplitude: Double, decay: Double) = (0, 0.01)
        switch effect {
        case .tap:
            partials = [Partial(frequency: 1_850, amplitude: 0.35, attack: 0.0008, decay: 0.010),
                        Partial(frequency: 3_300, amplitude: 0.12, attack: 0.0005, decay: 0.006)]
            noise = (0.05, 0.003)
        case .play:
            partials = [Partial(frequency: 660, glide: 0.12, amplitude: 0.30, attack: 0.004, decay: 0.07),
                        Partial(frequency: 1_320, glide: 0.12, amplitude: 0.08, attack: 0.004, decay: 0.05)]
        case .pause:
            partials = [Partial(frequency: 740, glide: -0.12, amplitude: 0.28, attack: 0.004, decay: 0.06),
                        Partial(frequency: 1_480, glide: -0.12, amplitude: 0.07, attack: 0.004, decay: 0.04)]
        case .skip:
            partials = [Partial(frequency: 1_250, glide: -0.06, amplitude: 0.26, attack: 0.001, decay: 0.018)]
            noise = (0.04, 0.004)
        case .confirm:
            partials = [Partial(frequency: 880, amplitude: 0.24, attack: 0.003, decay: 0.07),
                        Partial(frequency: 1_760, amplitude: 0.05, attack: 0.003, decay: 0.05),
                        Partial(frequency: 1_318.5, amplitude: 0.22, delay: 0.065, attack: 0.003, decay: 0.10),
                        Partial(frequency: 2_637, amplitude: 0.04, delay: 0.065, attack: 0.003, decay: 0.06)]
        case .open:
            partials = [Partial(frequency: 520, glide: 0.25, glideTime: 0.08, amplitude: 0.10, attack: 0.012, decay: 0.05)]
            noise = (0.05, 0.03)
        }
        let length = (partials.map { $0.delay + $0.decay * 7 }.max() ?? 0.1) + 0.01
        let frames = AVAudioFrameCount(length * format.sampleRate)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        let samples = buffer.floatChannelData![0]
        let rate = format.sampleRate
        var lowpass = 0.0
        var seed: UInt32 = 0x9E37_79B9
        var phases = Array(repeating: 0.0, count: partials.count)
        for frame in 0..<Int(frames) {
            let t = Double(frame) / rate
            var value = 0.0
            for (i, partial) in partials.enumerated() where t >= partial.delay {
                let local = t - partial.delay
                // Accumulated phase, so a pitch glide bends without a click.
                let frequency = partial.frequency * (1 + partial.glide * min(local / partial.glideTime, 1))
                phases[i] += 2 * .pi * frequency / rate
                let envelope = min(local / partial.attack, 1) * exp(-local / partial.decay)
                value += sin(phases[i]) * partial.amplitude * envelope
            }
            if noise.amplitude > 0 {
                seed = seed &* 1_664_525 &+ 1_013_904_223
                let white = Double(seed) / Double(UInt32.max) * 2 - 1
                lowpass += (white - lowpass) * 0.35
                value += lowpass * noise.amplitude * min(t / 0.001, 1) * exp(-t / noise.decay)
            }
            // A last short fade so no sound ends on a click of its own.
            let tail = min(1, (length - t) / 0.004)
            samples[frame] = Float(tanh(value * 1.2) * tail)
        }
        return buffer
    }
}
