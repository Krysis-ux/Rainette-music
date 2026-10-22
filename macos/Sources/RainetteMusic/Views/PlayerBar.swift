import AppKit
import SwiftUI

@available(macOS 14, *)
struct PlayerBar: View {
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(Router.self) private var router
    @State private var showQueue = false
    @State private var showOutput = false
    @State private var showEQ = false

    var body: some View {
        HStack(spacing: 18) {
            nowPlaying.frame(minWidth: 170, maxWidth: 260, alignment: .leading)
            Spacer(minLength: 0)
            VStack(spacing: 4) {
                TransportControls(size: .regular)
                Scrubber().frame(maxWidth: 420)
            }
            .frame(minWidth: 240, maxWidth: 440)
            Spacer(minLength: 0)
            HStack(spacing: 4) {
                barButton("quote.bubble", help: "Lyrics", active: router.showLyrics && router.showNowPlaying) {
                    router.showLyrics = true
                    router.showNowPlaying = true
                    app.sounds.play(.open)
                }
                barButton("list.bullet", help: "Up Next", active: showQueue) { showQueue.toggle(); app.sounds.play(.open) }
                    .popover(isPresented: $showQueue, arrowEdge: .top) { QueuePopover() }
                barButton(player.ownerIsDesktop ? "hifispeaker" : "iphone", help: "Play On", active: showOutput) { showOutput.toggle(); app.sounds.play(.open) }
                    .popover(isPresented: $showOutput, arrowEdge: .top) { OutputPopover() }
                barButton("slider.vertical.3", help: "Equalizer", active: showEQ || player.eqOn) { showEQ.toggle(); app.sounds.play(.open) }
                    .popover(isPresented: $showEQ, arrowEdge: .top) { EqualizerPanel().padding(20).frame(width: 380) }
                VolumeControl().frame(width: 104)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(height: 74)
        .glassSurface(in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .shadow(color: .black.opacity(0.16), radius: 22, y: 10)
    }

    private var nowPlaying: some View {
        HStack(spacing: 11) {
            if let track = player.current {
                ArtworkView(url: track.artworkURL, pixels: 160, cornerRadius: 7, seed: track.title)
                    .frame(width: 50, height: 50)
                    .shadow(color: .black.opacity(0.2), radius: 6, y: 3)
                    .overlay {
                        if player.state == .loading {
                            RoundedRectangle(cornerRadius: 7).fill(.black.opacity(0.35))
                            ProgressView().controlSize(.small).tint(.white)
                        }
                    }
                VStack(alignment: .leading, spacing: 2) {
                    Text(track.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    Text(player.state == .error ? "Couldn't play this song" : track.artist)
                        .font(.system(size: 12))
                        .foregroundStyle(player.state == .error ? Color.orange : .secondary)
                        .lineLimit(1)
                }
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { router.showLyrics = false; router.showNowPlaying = true; app.sounds.play(.open) }
        .help("Open Now Playing")
    }

    private func barButton(_ symbol: String, help: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(active ? Color.accentColor : .secondary)
                .frame(width: 30, height: 30)
                .background(Circle().fill(active ? Color.accentColor.opacity(0.14) : .clear))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help(help)
    }
}

/// Repeat, previous, play/pause, next.
@available(macOS 14, *)
struct TransportControls: View {
    enum Size { case compact, regular, large }
    var size: Size = .regular
    var showsRepeat = true
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player

    var body: some View {
        HStack(spacing: spacing) {
            if showsRepeat {
                Button { player.shuffleUpNext(); app.sounds.play(.tap) } label: { Image(systemName: "shuffle") }
                    .buttonStyle(TransportButtonStyle(size: small))
                    .disabled(player.upNext.count < 2)
                    .help("Shuffle Up Next")
            }
            Button { app.sounds.play(.skip); player.previous() } label: { Image(systemName: "backward.fill") }
                .buttonStyle(TransportButtonStyle(size: medium))
                .help("Previous")
            Button { app.sounds.play(player.showsPause ? .pause : .play); player.togglePlayPause() } label: {
                Image(systemName: player.showsPause ? "pause.fill" : "play.fill")
                    .contentTransition(.symbolEffect(.replace.downUp))
            }
            .buttonStyle(TransportButtonStyle(size: main, prominent: true))
            .help(player.showsPause ? "Pause" : "Play")
            Button { app.sounds.play(.skip); player.next() } label: { Image(systemName: "forward.fill") }
                .buttonStyle(TransportButtonStyle(size: medium))
                .help("Next")
            if showsRepeat {
                Button { player.cycleRepeat(); app.sounds.play(.tap) } label: {
                    Image(systemName: player.repeatMode.symbol)
                        .foregroundStyle(player.repeatMode == .off ? Color.secondary : Color.accentColor)
                        .contentTransition(.symbolEffect(.replace))
                }
                .buttonStyle(TransportButtonStyle(size: small))
                .help(player.repeatMode.label)
            }
        }
        .disabled(!player.hasTrack)
    }

    private var spacing: CGFloat { size == .large ? 30 : (size == .compact ? 10 : 16) }
    private var small: CGFloat { size == .large ? 18 : 13 }
    private var medium: CGFloat { size == .large ? 26 : (size == .compact ? 15 : 17) }
    private var main: CGFloat { size == .large ? 40 : (size == .compact ? 20 : 24) }
}

@available(macOS 14, *)
struct TransportButtonStyle: ButtonStyle {
    let size: CGFloat
    var prominent = false
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: size, weight: prominent ? .bold : .semibold))
            .frame(width: size * 1.7, height: size * 1.7)
            .background(Circle().fill(Color.primary.opacity(hovering ? 0.08 : 0)))
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed ? 0.86 : (hovering && prominent ? 1.06 : 1))
            .animation(.snappy(duration: 0.18), value: configuration.isPressed)
            .animation(.snappy(duration: 0.18), value: hovering)
            .onHover { hovering = $0 }
    }
}

/// A thin seek bar whose position moves smoothly between the engine's ticks.
@available(macOS 14, *)
struct Scrubber: View {
    var showsTimes = true
    var tint: Color = .primary
    @Environment(PlayerModel.self) private var player
    @State private var dragFraction: Double?
    @State private var hovering = false

    var body: some View {
        TimelineView(.animation(minimumInterval: 0.25, paused: !player.isPlaying || dragFraction != nil)) { context in
            let duration = player.duration
            let live = player.livePosition(at: context.date)
            let fraction = dragFraction ?? (duration > 0 ? live / duration : 0)
            HStack(spacing: 8) {
                if showsTimes { time(dragFraction.map { $0 * duration } ?? live).frame(width: 40, alignment: .trailing) }
                GeometryReader { proxy in
                    let height: CGFloat = hovering || dragFraction != nil ? 6 : 4
                    ZStack(alignment: .leading) {
                        Capsule().fill(tint.opacity(0.16)).frame(height: height)
                        Capsule().fill(tint.opacity(0.85)).frame(width: max(height, proxy.size.width * min(max(fraction, 0), 1)), height: height)
                        if hovering || dragFraction != nil {
                            Circle().fill(.white).shadow(color: .black.opacity(0.3), radius: 2, y: 1)
                                .frame(width: 12, height: 12)
                                .offset(x: proxy.size.width * min(max(fraction, 0), 1) - 6)
                        }
                    }
                    .frame(maxHeight: .infinity)
                    .contentShape(Rectangle())
                    .gesture(DragGesture(minimumDistance: 0)
                        .onChanged { value in dragFraction = min(max(value.location.x / max(proxy.size.width, 1), 0), 1) }
                        .onEnded { value in
                            let target = min(max(value.location.x / max(proxy.size.width, 1), 0), 1)
                            player.seek(toFraction: target)
                            dragFraction = nil
                        })
                }
                .frame(height: 14)
                .animation(.snappy(duration: 0.15), value: hovering)
                .onHover { hovering = $0 }
                if showsTimes { time(duration).frame(width: 40, alignment: .leading) }
            }
        }
        .disabled(player.duration <= 0)
        .accessibilityElement()
        .accessibilityLabel("Playback position")
        .accessibilityValue(Format.time(player.livePosition()))
    }

    private func time(_ seconds: Double) -> some View {
        Text(Format.time(seconds)).font(.system(size: 10.5, weight: .medium).monospacedDigit()).foregroundStyle(.secondary)
    }
}

@available(macOS 14, *)
struct VolumeControl: View {
    @Environment(PlayerModel.self) private var player
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 6) {
            Button { player.setVolume(player.volume > 0 ? 0 : 0.8) } label: {
                Image(systemName: symbol).font(.system(size: 12)).foregroundStyle(.secondary).frame(width: 18)
                    .contentTransition(.symbolEffect(.replace))
            }
            .buttonStyle(.plain)
            .help(player.volume > 0 ? "Mute" : "Unmute")
            GeometryReader { proxy in
                let fraction = min(player.volume, 1)
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.16)).frame(height: hovering ? 6 : 4)
                    Capsule().fill(Color.primary.opacity(0.8)).frame(width: proxy.size.width * fraction, height: hovering ? 6 : 4)
                }
                .frame(maxHeight: .infinity)
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                    let next = min(max(value.location.x / max(proxy.size.width, 1), 0), 1)
                    if (next == 0 || next == 1), next != min(player.volume, 1) {
                        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
                    }
                    player.setVolume(next)
                })
            }
            .frame(height: 14)
            .onHover { inside in withAnimation(.snappy(duration: 0.15)) { hovering = inside } }
        }
        .accessibilityElement()
        .accessibilityLabel("Volume")
        .accessibilityValue("\(Int(player.volume * 100)) percent")
        .accessibilityAdjustableAction { direction in
            player.setVolume(player.volume + (direction == .increment ? 0.1 : -0.1))
        }
    }

    private var symbol: String {
        switch player.volume {
        case 0: return "speaker.slash.fill"
        case ..<0.34: return "speaker.wave.1.fill"
        case ..<0.67: return "speaker.wave.2.fill"
        default: return "speaker.wave.3.fill"
        }
    }
}

// MARK: - Popovers

@available(macOS 14, *)
struct QueuePopover: View {
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Up Next").font(.system(size: 17, weight: .bold))
                Spacer()
                Button { player.shuffleUpNext(); app.sounds.play(.tap) } label: { Image(systemName: "shuffle") }
                    .buttonStyle(.borderless).help("Shuffle Up Next").disabled(player.upNext.count < 2)
                Button { player.clearUpNext(); app.sounds.play(.tap) } label: { Text("Clear") }
                    .buttonStyle(.borderless).disabled(player.upNext.isEmpty)
            }
            .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 10)
            if let current = player.current {
                HStack(spacing: 10) {
                    ArtworkView(url: current.artworkURL, pixels: 120, cornerRadius: 5, seed: current.title).frame(width: 38, height: 38)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(current.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                        Text(current.artist).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    PlayingIndicator(playing: player.isPlaying)
                }
                .padding(.horizontal, 16).padding(.bottom, 10)
            }
            Divider()
            if player.upNext.isEmpty {
                Text("Nothing up next. Use Play Next or Add to Queue on any song.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 120)
                    .multilineTextAlignment(.center)
                    .padding()
            } else {
                List {
                    ForEach(Array(player.upNext.enumerated()), id: \.offset) { offset, track in
                        let index = player.index + 1 + offset
                        HStack(spacing: 10) {
                            ArtworkView(url: track.artworkURL, pixels: 100, cornerRadius: 4, seed: track.title).frame(width: 32, height: 32)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(track.title).font(.system(size: 12.5, weight: .medium)).lineLimit(1)
                                Text(track.artist).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                            }
                            Spacer()
                            Text(Format.time(track.duration)).font(.system(size: 11).monospacedDigit()).foregroundStyle(.tertiary)
                        }
                        .contentShape(Rectangle())
                        .onTapGesture(count: 2) { app.sounds.play(.play); player.playQueueIndex(index) }
                        .contextMenu {
                            Button("Play Now") { player.playQueueIndex(index) }
                            Button("Remove from Queue", role: .destructive) { player.removeFromQueue(index) }
                        }
                    }
                    .onMove { source, destination in
                        guard let from = source.first else { return }
                        let to = destination > from ? destination - 1 : destination
                        player.moveInQueue(from: player.index + 1 + from, to: player.index + 1 + to)
                    }
                    .onDelete { offsets in
                        for offset in offsets.sorted(by: >) { player.removeFromQueue(player.index + 1 + offset) }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
        .frame(width: 360, height: 460)
    }
}

@available(macOS 14, *)
struct OutputPopover: View {
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(Router.self) private var router
    @Environment(\.openSettings) private var openSettings
    @Environment(\.dismiss) private var dismiss
    @State private var failure: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Play On").font(.system(size: 15, weight: .bold)).padding(.horizontal, 6).padding(.bottom, 4)
            option(symbol: "macbook", title: "This Mac", detail: player.ownerName.isEmpty || !player.ownerIsDesktop ? "" : player.ownerName,
                   selected: player.ownerIsDesktop) {}
            ForEach(app.phone.devices) { phone in
                let active = !player.ownerIsDesktop && player.ownerName == phone.name
                option(symbol: "iphone", title: phone.name, detail: player.transferPending ? "Connecting…" : (active ? "Playing" : "Paired phone"),
                       selected: active) {
                    Task {
                        failure = await player.transfer(toPhone: phone.id, name: phone.name)
                        if failure == nil { app.sounds.play(.confirm); dismiss() }
                    }
                }
                .disabled(player.transferPending || active)
            }
            if let failure {
                Label(failure, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 11)).foregroundStyle(.orange).padding(.horizontal, 6)
            }
            Divider().padding(.vertical, 4)
            Button { router.settingsTab = .phone; openSettings(); dismiss() } label: {
                Label(app.phone.devices.isEmpty ? "Pair a Phone…" : "Phone Settings…", systemImage: "qrcode")
            }
            .buttonStyle(.borderless).padding(.horizontal, 6)
            Button {
                NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Sound-Settings.extension")!)
            } label: { Label("Sound Settings…", systemImage: "speaker.wave.2") }
                .buttonStyle(.borderless).padding(.horizontal, 6)
        }
        .padding(12)
        .frame(width: 280)
        .task { await app.phone.refreshDevices() }
    }

    private func option(symbol: String, title: String, detail: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: symbol).font(.system(size: 16)).frame(width: 30, height: 30)
                    .background(Circle().fill(selected ? Color.accentColor : Color.primary.opacity(0.08)))
                    .foregroundStyle(selected ? .white : .primary)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.system(size: 13, weight: .medium))
                    if !detail.isEmpty { Text(detail).font(.system(size: 11)).foregroundStyle(.secondary) }
                }
                Spacer()
                if selected { Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)).foregroundStyle(Color.accentColor) }
            }
            .padding(6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
