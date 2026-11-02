import Capacitor
import Foundation
import RainettePlayerCore
import UIKit

/// The page's only door into native playback on iPhone. Method for method it
/// is what RainettePlayerPlugin.kt is on Android, so pwa/src/native-engine.js
/// never needs to know which phone it is on. Nothing here accepts a URL to
/// play: the page names a song, and the player finds where it comes from.
///
/// Events go through `watch`, not Capacitor's listeners: `state` on every
/// change and each second while playing, `error` for a failure the player has
/// given up on. Both carry the page's `generation`.
@objc(RainettePlayerPlugin)
public class RainettePlayerPlugin: CAPPlugin, CAPBridgedPlugin, UIDocumentPickerDelegate {
    public let identifier = "RainettePlayerPlugin"
    public let jsName = "RainettePlayer"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "watch", returnType: CAPPluginReturnCallback),
        CAPPluginMethod(name: "capabilities", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "search", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "resolve", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "play", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setQueue", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "resume", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "pause", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "seek", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "skip", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setRepeat", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setVolume", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getState", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setSystemBarColor", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "installUpdate", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stageFile", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "saveFile", returnType: CAPPluginReturnPromise),
    ]

    /// Must match PROTOCOL in pwa/src/native-engine.js and RainettePlayerPlugin.kt.
    static let protocolVersion = 1

    @MainActor private lazy var engine: PlayerEngine = {
        let engine = PlayerEngine()
        engine.onState = { [weak self] state in self?.send("state", state.json) }
        engine.onError = { [weak self] error, state in
            self?.send("error", ["generation": state.generation, "code": error.code, "message": error.message,
                                 "index": state.index, "source_id": state.sourceId])
        }
        return engine
    }()

    /// The page's event channel, and which page instance owns it. Main thread only.
    private var watchers: [CAPPluginCall] = []
    private var watchingPage = ""
    /// The save waiting on the Files sheet, and the copy it is exporting. Main thread only.
    private var pendingSave: (call: CAPPluginCall, file: URL)?

    override public func load() {
        // A page that was asleep missed events; give it the truth on return.
        NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.send("state", self.engine.snapshot().json)
            }
        }
    }

    /// One page at a time: a new page id (a reload) replaces the old one's callbacks.
    @objc func watch(_ call: CAPPluginCall) {
        let page = call.getString("page") ?? ""
        call.keepAlive = true
        Task { @MainActor in
            if page != watchingPage {
                watchers.forEach { bridge?.releaseCall($0) }
                watchers.removeAll()
                watchingPage = page
            }
            watchers.append(call)
            call.resolve(["event": "state"].merging(engine.snapshot().json) { $1 })
        }
    }

    @MainActor private func send(_ event: String, _ data: [String: Any]) {
        var payload = data
        payload["event"] = event
        watchers.forEach { $0.resolve(payload) }
    }

    @objc func capabilities(_ call: CAPPluginCall) {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? ""
        // A sideloaded iPhone app cannot install a newer copy of itself.
        call.resolve(["search": true, "playback": true, "protocol": Self.protocolVersion,
                      "version": version, "installs_updates": false])
    }

    @objc func search(_ call: CAPPluginCall) {
        let query = (call.getString("query") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty, query.count <= 200 else { return reject(call, PlayerError(PlayerError.invalid, "Type something to search for.")) }
        Task {
            do {
                let songs = try await MusicSearch.search(query)
                call.resolve(["songs": songs.map(\.json)])
            } catch {
                reject(call, PlayerError.classify(error))
            }
        }
    }

    /// An address for the web `<audio>` element, for queues it owns.
    @objc func resolve(_ call: CAPPluginCall) {
        let id = call.getString("source_id") ?? ""
        guard StreamResolver.isValidId(id) else { return reject(call, PlayerError(PlayerError.invalid, "Not a track this app can play.")) }
        let force = call.getBool("force_refresh") ?? false
        Task {
            do {
                let stream = try await StreamResolver.shared.resolve(id, force: force)
                // The User-Agent the address was issued to: the page's own
                // downloads have to present it, as the player does.
                call.resolve(["url": stream.url.absoluteString, "mime_type": "audio/mp4",
                              "expires_at_ms": Int(stream.expiresAt.timeIntervalSince1970 * 1000),
                              "user_agent": stream.userAgent])
            } catch {
                reject(call, PlayerError.classify(error))
            }
        }
    }

    /// Replace the queue and start it. Resolves only once the first song has loaded.
    @objc func play(_ call: CAPPluginCall) {
        guard let items = parseQueue(call) else { return }
        let index = call.getInt("index") ?? 0
        let position = max(0, call.getDouble("position_s") ?? 0)
        let paused = call.getBool("start_paused") ?? false
        let generation = call.getInt("generation") ?? 0
        Task { @MainActor in
            do {
                let state = try await engine.play(items, at: index, from: position, startPaused: paused, generation: generation)
                call.resolve(state.json)
            } catch {
                reject(call, PlayerError.classify(error))
            }
        }
    }

    @objc func setQueue(_ call: CAPPluginCall) {
        guard let items = parseQueue(call) else { return }
        let index = call.getInt("index") ?? -1
        let generation = call.getInt("generation")
        Task { @MainActor in
            do {
                call.resolve(try engine.setQueue(items, at: index, generation: generation).json)
            } catch {
                reject(call, PlayerError.classify(error))
            }
        }
    }

    @objc func resume(_ call: CAPPluginCall) { onEngine(call) { $0.resume() } }
    @objc func pause(_ call: CAPPluginCall) { onEngine(call) { $0.pause() } }
    @objc func stop(_ call: CAPPluginCall) { onEngine(call) { $0.stop() } }
    @objc func getState(_ call: CAPPluginCall) {
        let withQueue = call.getBool("with_queue") ?? false
        Task { @MainActor in
            var state = engine.snapshot().json
            if withQueue { state["queue"] = engine.queue.map(\.json) }
            call.resolve(state)
        }
    }

    @objc func seek(_ call: CAPPluginCall) {
        guard let seconds = call.getDouble("position_s") else { return reject(call, PlayerError(PlayerError.invalid, "No position given.")) }
        onEngine(call) { $0.seek(to: seconds) }
    }

    /// Next, or previous; previous restarts a song more than three seconds in.
    @objc func skip(_ call: CAPPluginCall) {
        let offset = call.getInt("offset") ?? 1
        onEngine(call) { $0.skip(offset > 0 ? 1 : -1) }
    }

    @objc func setRepeat(_ call: CAPPluginCall) {
        guard let mode = RepeatMode(rawValue: call.getString("mode") ?? "") else {
            return reject(call, PlayerError(PlayerError.invalid, "Unknown repeat mode."))
        }
        onEngine(call) { $0.setRepeat(mode) }
    }

    @objc func setVolume(_ call: CAPPluginCall) {
        let volume = call.getDouble("volume") ?? 1
        onEngine(call) { $0.setVolume(volume) }
    }

    /// The page's theme colour, for what shows behind the status bar and which
    /// colour the clock and battery are drawn in.
    @objc func setSystemBarColor(_ call: CAPPluginCall) {
        let raw = call.getString("color") ?? ""
        guard raw.range(of: "^#[0-9A-Fa-f]{6}$", options: .regularExpression) != nil,
              let value = Int(raw.dropFirst(), radix: 16) else { return reject(call, PlayerError(PlayerError.invalid, "Not a colour.")) }
        let red = CGFloat((value >> 16) & 0xFF) / 255, green = CGFloat((value >> 8) & 0xFF) / 255, blue = CGFloat(value & 0xFF) / 255
        let light = 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.5
        DispatchQueue.main.async { [weak self] in
            let color = UIColor(red: red, green: green, blue: blue, alpha: 1)
            self?.bridge?.viewController?.view.backgroundColor = color
            self?.bridge?.webView?.backgroundColor = color
            self?.bridge?.statusBarStyle = light ? .darkContent : .lightContent
            call.resolve()
        }
    }

    /// A sideloaded iPhone app cannot install a newer copy of itself, so an
    /// update opens the download page, where the sideloading app takes over.
    @objc func installUpdate(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            UIApplication.shared.open(Self.downloadPage) { opened in call.resolve(["opened": opened]) }
        }
    }

    static let downloadPage = URL(string: "https://rainette-music.vercel.app")!

    /// Hold a file's bytes (base64) for `saveFile`, replacing any held before.
    /// Two calls, as on Android, where a song carried in the call that opens
    /// the save screen is more than the system allows a waiting app to keep.
    @objc func stageFile(_ call: CAPPluginCall) {
        guard let data = Data(base64Encoded: call.getString("data") ?? ""), !data.isEmpty else {
            return reject(call, PlayerError(PlayerError.invalid, "Nothing to save."))
        }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("staged", isDirectory: true)
        do {
            try? FileManager.default.removeItem(at: folder)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try data.write(to: folder.appendingPathComponent("file"), options: .atomic)
            call.resolve()
        } catch {
            reject(call, PlayerError(PlayerError.invalid, "That file could not be prepared."))
        }
    }

    /// Save the staged file where the person chooses, through Files. A web
    /// view cannot save a page's file itself: a download link does nothing in
    /// one. Resolves `{ saved }`, false when the sheet was dismissed.
    @objc func saveFile(_ call: CAPPluginCall) {
        let staged = FileManager.default.temporaryDirectory.appendingPathComponent("staged/file")
        let unsafe = CharacterSet(charactersIn: "/\\:*?\"<>|")
        let cleaned = (call.getString("name") ?? "").components(separatedBy: unsafe).joined(separator: "_")
        let name = cleaned.isEmpty ? "Rainette file" : String(cleaned.prefix(120))
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let file = folder.appendingPathComponent(name)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try FileManager.default.moveItem(at: staged, to: file)
        } catch {
            return reject(call, PlayerError(PlayerError.invalid, "Nothing to save."))
        }
        DispatchQueue.main.async { [weak self] in
            guard let self, let host = self.bridge?.viewController else { return call.resolve(["saved": false]) }
            self.finishSave(saved: false)
            self.pendingSave = (call, file)
            let picker = UIDocumentPickerViewController(forExporting: [file], asCopy: true)
            picker.delegate = self
            host.present(picker, animated: true)
        }
    }

    public func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        finishSave(saved: true)
    }

    public func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
        finishSave(saved: false)
    }

    private func finishSave(saved: Bool) {
        guard let pending = pendingSave else { return }
        pendingSave = nil
        try? FileManager.default.removeItem(at: pending.file.deletingLastPathComponent())
        pending.call.resolve(["saved": saved])
    }

    // MARK: - Internals

    private func onEngine(_ call: CAPPluginCall, _ action: @escaping @MainActor (PlayerEngine) -> PlayerSnapshot) {
        Task { @MainActor in call.resolve(action(engine).json) }
    }

    private func reject(_ call: CAPPluginCall, _ error: PlayerError) {
        call.reject(error.message, error.code)
    }

    /// Untrusted input from the page becomes queue items here, or the call is refused.
    private func parseQueue(_ call: CAPPluginCall) -> [QueueItem]? {
        guard let raw = call.getArray("queue"), !raw.isEmpty, raw.count <= PlayerEngine.maxQueue else {
            reject(call, PlayerError(PlayerError.invalid, "That queue cannot be played."))
            return nil
        }
        var items: [QueueItem] = []
        for value in raw {
            let track = value as? JSObject ?? [:]
            let id = track["source_id"] as? String ?? ""
            guard StreamResolver.isValidId(id) else {
                reject(call, PlayerError(PlayerError.invalid, "Only streamed songs can play here."))
                return nil
            }
            let art = track["thumbnail_url"] as? String ?? ""
            items.append(QueueItem(
                sourceId: id,
                title: String((track["title"] as? String ?? "").prefix(300)),
                artist: String((track["artist"] as? String ?? "").prefix(300)),
                thumbnailURL: art.hasPrefix("https://") ? art : "",
                durationS: (track["duration_s"] as? NSNumber)?.doubleValue
            ))
        }
        return items
    }
}
