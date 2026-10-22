import SwiftUI

@available(macOS 14, *)
struct NowPlayingView: View {
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @Environment(LibraryModel.self) private var library
    @Environment(Router.self) private var router
    @State private var lyrics: Lyrics?
    @State private var lyricsFor = ""
    @State private var loadingLyrics = false

    var body: some View {
        @Bindable var router = router
        let track = player.current
        ZStack {
            ArtworkBackdrop(url: track?.artworkURL ?? "", blur: 90, dim: 0.45).ignoresSafeArea()
            HStack(spacing: 56) {
                VStack(spacing: 26) {
                    ArtworkView(url: track?.artworkURL ?? "", pixels: 900, cornerRadius: 18, seed: track?.title ?? "")
                        .frame(maxWidth: 420, maxHeight: 420)
                        .shadow(color: .black.opacity(0.45), radius: 40, y: 22)
                        .scaleEffect(player.isPlaying ? 1 : 0.94)
                        .animation(.spring(response: 0.5, dampingFraction: 0.75), value: player.isPlaying)
                    VStack(spacing: 6) {
                        Text(track?.title ?? "").font(.system(size: 26, weight: .bold)).lineLimit(2).multilineTextAlignment(.center)
                        if let artist = track?.artistRef {
                            Button(artist.name) { router.showNowPlaying = false; router.open(.artist(artist)) }
                                .buttonStyle(.plain)
                                .font(.system(size: 17, weight: .medium))
                                .foregroundStyle(.white.opacity(0.75))
                        }
                    }
                    Scrubber(tint: .white).frame(maxWidth: 440)
                    TransportControls(size: .large)
                    HStack(spacing: 14) {
                        VolumeControl().frame(width: 180)
                        Spacer().frame(width: 20)
                        sleepMenu
                        Toggle(isOn: $router.showLyrics.animation(.smooth)) { Image(systemName: "quote.bubble") }
                            .toggleStyle(.button)
                            .help("Lyrics")
                    }
                    .foregroundStyle(.white.opacity(0.85))
                }
                .frame(maxWidth: 480)
                if router.showLyrics {
                    LyricsPanel(lyrics: lyrics, loading: loadingLyrics)
                        .frame(maxWidth: 520)
                        .transition(.move(edge: .trailing).combined(with: .opacity))
                }
            }
            .padding(48)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .overlay(alignment: .topLeading) {
            Button { router.showNowPlaying = false } label: { Image(systemName: "chevron.down") }
                .buttonStyle(GlassIconButtonStyle(size: 34))
                .keyboardShortcut(.cancelAction)
                .padding(.leading, 20)
                .padding(.top, 44)
                .help("Close")
        }
        .environment(\.colorScheme, .dark)
        .task(id: router.showLyrics ? (track?.key ?? "") : "") { await loadLyrics() }
    }

    private var sleepMenu: some View {
        Menu {
            if player.sleepTimerEnd != nil || player.sleepAtEndOfTrack {
                Button("Turn Off Sleep Timer") { player.cancelSleepTimer() }
                Divider()
            }
            ForEach([15, 30, 45, 60], id: \.self) { minutes in
                Button("In \(minutes) Minutes") { player.startSleepTimer(minutes: minutes); app.sounds.play(.confirm) }
            }
            Button("At End of This Song") { player.sleepAtEndOfCurrentTrack(); app.sounds.play(.confirm) }
        } label: {
            if let end = player.sleepTimerEnd {
                Label { Text(end, style: .timer).monospacedDigit() } icon: { Image(systemName: "moon.zzz.fill") }
            } else {
                Image(systemName: player.sleepAtEndOfTrack ? "moon.zzz.fill" : "moon.zzz")
            }
        }
        .menuStyle(.button)
        .buttonStyle(.borderless)
        .fixedSize()
        .help("Sleep Timer")
    }

    private func loadLyrics() async {
        guard router.showLyrics, let track = player.current, track.key != lyricsFor else { return }
        lyricsFor = track.key
        loadingLyrics = true
        lyrics = nil
        let found = await library.lyrics(for: track)
        guard player.current?.key == track.key else { return }
        withAnimation(.smooth) { lyrics = found ?? Lyrics(notFound: true); loadingLyrics = false }
    }
}

/// Lyrics that follow the song when they are timed, and scroll freely when not.
@available(macOS 14, *)
struct LyricsPanel: View {
    let lyrics: Lyrics?
    let loading: Bool
    @Environment(PlayerModel.self) private var player

    var body: some View {
        Group {
            if loading {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let lyrics, !lyrics.lines.isEmpty {
                synced(lyrics.lines)
            } else if let lyrics, !lyrics.plain.isEmpty {
                ScrollView {
                    Text(lyrics.plain).font(.system(size: 20, weight: .semibold)).lineSpacing(10)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 40)
                }
                .mask(fade)
            } else {
                VStack(spacing: 10) {
                    Image(systemName: lyrics?.instrumental == true ? "pianokeys" : "quote.bubble").font(.system(size: 30))
                    Text(lyrics?.instrumental == true ? "Instrumental" : "No lyrics for this song").font(.system(size: 15, weight: .medium))
                }
                .foregroundStyle(.white.opacity(0.6))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .foregroundStyle(.white)
    }

    private func synced(_ lines: [Lyrics.Line]) -> some View {
        TimelineView(.animation(minimumInterval: 0.2, paused: !player.isPlaying)) { context in
            let now = player.livePosition(at: context.date) + 0.25
            let active = lines.lastIndex { $0.time <= now } ?? -1
            ScrollViewReader { proxy in
                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 18) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { offset, line in
                            Text(line.text.isEmpty ? "• • •" : line.text)
                                .font(.system(size: 26, weight: .bold))
                                .foregroundStyle(.white.opacity(offset == active ? 1 : 0.35))
                                .blur(radius: offset == active || abs(offset - active) > 4 ? 0 : 0.4)
                                .scaleEffect(offset == active ? 1 : 0.97, anchor: .leading)
                                .animation(.smooth(duration: 0.4), value: active)
                                .id(offset)
                                .onTapGesture {
                                    if player.duration > 0 { player.seek(toFraction: line.time / player.duration) }
                                }
                        }
                    }
                    .padding(.vertical, 220)
                }
                .mask(fade)
                .onChange(of: active) { _, value in
                    guard value >= 0 else { return }
                    withAnimation(.smooth(duration: 0.6)) { proxy.scrollTo(value, anchor: .center) }
                }
            }
        }
    }

    private var fade: some View {
        LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.18),
                               .init(color: .black, location: 0.82), .init(color: .clear, location: 1)],
                       startPoint: .top, endPoint: .bottom)
    }
}

/// A small floating player that can sit above other apps.
@available(macOS 14, *)
struct MiniPlayerView: View {
    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    @AppStorage("miniPlayerOnTop") private var onTop = true
    @Environment(\.dismissWindow) private var dismissWindow
    @State private var hovering = false

    var body: some View {
        let track = player.current
        ZStack {
            ArtworkBackdrop(url: track?.artworkURL ?? "", blur: 40, dim: 0.35)
            HStack(spacing: 14) {
                ArtworkView(url: track?.artworkURL ?? "", pixels: 240, cornerRadius: 10, seed: track?.title ?? "")
                    .frame(width: 76, height: 76)
                    .shadow(color: .black.opacity(0.35), radius: 10, y: 5)
                VStack(alignment: .leading, spacing: 6) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(track?.title ?? "Not Playing").font(.system(size: 14, weight: .semibold)).lineLimit(1)
                        Text(track?.artist ?? "Rainette Music").font(.system(size: 12)).foregroundStyle(.white.opacity(0.7)).lineLimit(1)
                    }
                    TransportControls(size: .compact, showsRepeat: false)
                    Scrubber(showsTimes: false, tint: .white)
                }
            }
            .padding(14)
        }
        .foregroundStyle(.white)
        .frame(width: 340, height: 104)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(alignment: .topTrailing) {
            if hovering {
                HStack(spacing: 6) {
                    Button { onTop.toggle() } label: { Image(systemName: onTop ? "pin.fill" : "pin") }
                        .help(onTop ? "Stop Floating on Top" : "Float on Top")
                    Button { dismissWindow(id: "mini") } label: { Image(systemName: "xmark") }
                        .help("Close Mini Player")
                }
                .buttonStyle(GlassIconButtonStyle(size: 22))
                .padding(7)
                .transition(.opacity)
            }
        }
        .onHover { inside in withAnimation(.snappy(duration: 0.2)) { hovering = inside } }
        .environment(\.colorScheme, .dark)
        .background(PanelWindow(floating: onTop))
        .ignoresSafeArea()
    }
}

@available(macOS 14, *)
private struct PanelWindow: NSViewRepresentable {
    let floating: Bool

    func makeNSView(context: Context) -> NSView { NSView() }

    func updateNSView(_ view: NSView, context: Context) {
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            if !window.styleMask.contains(.borderless) || window.styleMask.contains(.titled) {
                window.styleMask = [.borderless, .fullSizeContentView]
                window.isOpaque = false
                window.backgroundColor = .clear
                window.hasShadow = true
                window.isMovableByWindowBackground = true
                window.collectionBehavior.formUnion([.fullScreenAuxiliary, .canJoinAllSpaces])
            }
            window.level = floating ? .floating : .normal
            window.invalidateShadow()
        }
    }
}
