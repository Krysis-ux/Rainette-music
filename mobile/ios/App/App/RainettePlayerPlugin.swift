import AVFoundation
import AVKit
import Capacitor
import Foundation
import RainettePlayerCore
import UIKit

/// The page's only door into native playback on iPhone.
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
        CAPPluginMethod(name: "configure", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "outputs", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "pickOutput", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "showOutputPicker", returnType: CAPPluginReturnPromise),
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
    /// The system's device picker, kept in the view so its sheet can stay up. Main thread only.
    private var routePicker: AVRoutePickerView?

    override public func load() {
        // Closing the app from the app switcher closes the music with it.
        NotificationCenter.default.addObserver(forName: UIApplication.willTerminateNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { _ = self?.engine.stop() }
        }
        // Headphones in, AirPods connected, a car: tell the page where the sound goes.
        NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.send("route", Self.route()) }
        }
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

    // MARK: - Speed, quality and outputs

    /// Playback speed and streaming quality, from the page's settings.
    @objc func configure(_ call: CAPPluginCall) {
        let rate = Float(min(2, max(0.5, call.getDouble("rate") ?? 1)))
        let saver = call.getString("quality") == "saver"
        Task { @MainActor in
            await StreamResolver.shared.setDataSaver(saver)
            call.resolve(engine.setRate(rate).json)
        }
    }

    /// Where the sound is going now. iOS lets an app see the route, not choose
    /// among Bluetooth devices, so the choosing happens in `showOutputPicker`.
    @objc func outputs(_ call: CAPPluginCall) {
        DispatchQueue.main.async { call.resolve(Self.route()) }
    }

    /// Nothing to pick directly on iPhone; the system picker does it.
    @objc func pickOutput(_ call: CAPPluginCall) {
        showOutputPicker(call)
    }

    /// The system's own list of AirPlay and Bluetooth devices.
    @objc func showOutputPicker(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self, let view = self.bridge?.viewController?.view else {
                return call.reject("The device list isn't available.", PlayerError.invalid)
            }
            let picker = self.routePicker ?? AVRoutePickerView(frame: CGRect(x: 0, y: 0, width: 1, height: 1))
            if picker.superview == nil {
                picker.alpha = 0.011
                view.addSubview(picker)
                self.routePicker = picker
            }
            picker.layoutIfNeeded()
            guard let button = Self.firstButton(in: picker) else {
                return call.reject("The device list isn't available.", PlayerError.invalid)
            }
            // Newer iOS wires the picker to the button's primary action, older to touch up.
            button.sendActions(for: button.allControlEvents.contains(.primaryActionTriggered) ? .primaryActionTriggered : .touchUpInside)
            call.resolve()
        }
    }

    /// The picker's own button, wherever this iOS version nests it.
    static func firstButton(in view: UIView) -> UIButton? {
        for child in view.subviews {
            if let button = child as? UIButton { return button }
            if let nested = firstButton(in: child) { return nested }
        }
        return nil
    }

    static func route() -> [String: Any] {
        let outputs = AVAudioSession.sharedInstance().currentRoute.outputs
        var devices: [[String: Any]] = outputs.map {
            ["id": $0.uid, "name": $0.portName, "kind": kind(of: $0.portType), "active": true]
        }
        // The phone itself is always somewhere to play; choosing it goes through the picker.
        if !devices.contains(where: { ($0["kind"] as? String) == "speaker" }) {
            devices.append(["id": "speaker", "name": "iPhone", "kind": "speaker", "active": devices.isEmpty])
        }
        let current = devices.first { ($0["active"] as? Bool) == true } ?? devices[0]
        return ["current": current, "devices": devices, "can_pick": false]
    }

    static func kind(of port: AVAudioSession.Port) -> String {
        switch port {
        case .bluetoothA2DP, .bluetoothLE, .bluetoothHFP: return "bluetooth"
        case .headphones, .headsetMic: return "headphones"
        case .airPlay: return "airplay"
        case .carAudio: return "car"
        case .usbAudio: return "usb"
        case .HDMI: return "hdmi"
        default: return "speaker"
        }
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
