import AppKit
import SwiftUI

@available(macOS 14, *)
struct RainetteApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @AppStorage(Preference.appearance) private var appearance = "system"

    var body: some Scene {
        Window("Rainette Music", id: "main") {
            RootView()
                .environment(delegate.model)
                .environment(delegate.model.player)
                .environment(delegate.model.library)
                .environment(delegate.model.router)
                .frame(minWidth: 900, minHeight: 600)
                .onAppear { applyAppearance() }
                .onChange(of: appearance) { applyAppearance() }
        }
        .defaultSize(width: 1240, height: 820)
        .windowToolbarStyle(.unified(showsTitle: false))
        .commands { RainetteCommands(model: delegate.model) }

        Window("Mini Player", id: "mini") {
            MiniPlayerView()
                .environment(delegate.model)
                .environment(delegate.model.player)
                .environment(delegate.model.router)
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentSize)
        .defaultPosition(.topTrailing)

        Settings {
            SettingsView()
                .environment(delegate.model)
                .environment(delegate.model.player)
                .environment(delegate.model.library)
                .environment(delegate.model.phone)
                .environment(delegate.model.router)
        }
    }

    private func applyAppearance() {
        switch appearance {
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        default: NSApp.appearance = nil
        }
    }
}

@available(macOS 14, *)
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    private var keyMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { await model.start() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [model] in
            if !model.mainWindowOpen { model.showMainWindow?() }
        }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handleKey(event) ?? event
        }
    }

    /// Music keeps playing with no window open, the way Music.app does.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !model.mainWindowOpen { model.showMainWindow?() }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.shutdown()
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        let player = model.player
        if let track = player.current {
            let title = NSMenuItem(title: "\(track.title) — \(track.artist)", action: nil, keyEquivalent: "")
            title.isEnabled = false
            menu.addItem(title)
            menu.addItem(.separator())
        }
        menu.addItem(withTitle: player.showsPause ? "Pause" : "Play", action: #selector(dockToggle), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Next", action: #selector(dockNext), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Previous", action: #selector(dockPrevious), keyEquivalent: "").target = self
        return menu
    }

    @objc private func dockToggle() { model.player.togglePlayPause() }
    @objc private func dockNext() { model.player.next() }
    @objc private func dockPrevious() { model.player.previous() }

    /// Space plays and pauses unless someone is typing.
    private func handleKey(_ event: NSEvent) -> NSEvent? {
        let modifiers = event.modifierFlags.intersection([.command, .option, .control, .shift])
        guard event.keyCode == 49, modifiers.isEmpty, !event.isARepeat,
              !(event.window?.firstResponder is NSText),
              event.window?.attachedSheet == nil,
              NSApp.modalWindow == nil,
              !(event.window?.firstResponder is NSControl),
              model.player.hasTrack else { return event }
        model.sounds.play(model.player.showsPause ? .pause : .play)
        model.player.togglePlayPause()
        return nil
    }
}

@available(macOS 14, *)
struct RainetteCommands: Commands {
    let model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        let _ = registerOpener()
        CommandGroup(after: .appInfo) {
            Button("Relaunch in Classic Interface") { ClassicInterface.relaunchClassic() }
        }
        CommandMenu("Controls") {
            Button(model.player.showsPause ? "Pause" : "Play") { model.player.togglePlayPause() }
                .disabled(!model.player.hasTrack)
            Button("Next") { model.player.next() }
                .keyboardShortcut(.rightArrow, modifiers: .command)
            Button("Previous") { model.player.previous() }
                .keyboardShortcut(.leftArrow, modifiers: .command)
            Divider()
            // Never lowers a boost (above 100%) set elsewhere.
            Button("Increase Volume") { model.player.setVolume(max(model.player.volume, min(1, model.player.volume + 0.1))) }
                .keyboardShortcut(.upArrow, modifiers: .command)
            Button("Decrease Volume") { model.player.setVolume(model.player.volume - 0.1) }
                .keyboardShortcut(.downArrow, modifiers: .command)
            Divider()
            Picker("Repeat", selection: Binding(get: { model.player.repeatMode }, set: { model.player.setRepeat($0) })) {
                ForEach(RepeatMode.allCases, id: \.self) { mode in Text(mode.label).tag(mode) }
            }
            Button("Shuffle Up Next") { model.player.shuffleUpNext() }
                .disabled(model.player.upNext.isEmpty)
        }
        CommandGroup(after: .windowArrangement) {
            Button("Mini Player") { openWindow(id: "mini") }
                .keyboardShortcut("m", modifiers: [.command, .option])
            Button("Now Playing") { withAnimation(.smooth) { model.router.showNowPlaying.toggle() } }
                .keyboardShortcut("p", modifiers: [.command, .option])
                .disabled(!model.player.hasTrack)
        }
        CommandGroup(after: .sidebar) {
            Button("Show Library") { openWindow(id: "main") }
                .keyboardShortcut("0", modifiers: .command)
            Button("Back") { model.router.back() }
                .keyboardShortcut("[", modifiers: .command)
                .disabled(model.router.path.isEmpty)
            Button("Search") { model.router.selection = .search }
                .keyboardShortcut("f", modifiers: .command)
        }
    }

    private func registerOpener() {
        let open = openWindow
        model.showMainWindow = { open(id: "main") }
    }
}
