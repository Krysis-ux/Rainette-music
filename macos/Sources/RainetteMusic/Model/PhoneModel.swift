import AppKit
import Observation

/// Pairing a phone and making this Mac reachable from it.
@available(macOS 14, *)
@Observable @MainActor
final class PhoneModel {
    struct Provider: Identifiable, Hashable {
        let id: String
        let label: String
        let description: String
        let recommended: Bool
    }

    struct Device: Identifiable, Hashable {
        let id: String
        let name: String
    }

    struct Invitation {
        let url: String
        let qr: NSImage?
        let expiresAt: Date
        let endpointIsLocal: Bool
    }

    enum Tone { case neutral, success, error }

    /// Fields a provider needs typed in (PROVIDER_FIELDS in the web page).
    static let providerFields: [String: [(key: String, label: String, placeholder: String)]] = [
        "cloudflare-named": [("hostname", "Address (Rainette fills this in for you)", "music.example.com"),
                             ("tunnel_name", "Tunnel name (optional)", "rainette-my-computer")],
        "manual": [("public_url", "Your HTTPS address for this computer", "https://music-pc.example.com")],
    ]

    /// The link beside a step that is the person's to do (SETUP_ACTIONS).
    static let setupLinkLabels = ["install": "Get it", "signup": "Create a free account", "login": "Create a free account",
                                  "consent": "Allow it", "configure": "Open the dashboard", "provision": "Open the dashboard"]

    var providers: [Provider] = []
    var selectedProvider = ""
    var providerConfig: [String: String] = [:]
    var status: JSON = .null
    var invitation: Invitation?
    var pending: [Device] = []
    var devices: [Device] = []
    var message = ""
    var tone: Tone = .neutral
    var setupInFlight = false
    var pwaURL = ""
    var publicURL = ""

    @ObservationIgnored weak var app: AppModel?
    @ObservationIgnored private var tunnelPoll: Task<Void, Never>?
    @ObservationIgnored private var devicePoll: Task<Void, Never>?
    @ObservationIgnored private var active = false

    private func call(_ method: String, _ args: [JSON] = []) async throws -> JSON {
        guard let client = app?.client else { throw NSError(domain: "Rainette", code: 2, userInfo: [NSLocalizedDescriptionKey: "Rainette is still starting."]) }
        return try await client.native(method, args)
    }

    // MARK: Derived (mirrors tunnelMessage / helperMessage / stabilityMessage)

    var phase: String { status["phase"].text }
    var isRunning: Bool { status["running"].bool || phase == "running" }
    var isBusy: Bool { status["busy"].bool }
    var helper: JSON { status["helper"] }
    var helperRequired: Bool { helper["required"].isNull || helper["required"].bool }
    var helperReady: Bool { helper["ready"].bool }
    var providerLabel: String { status["provider_label"].string ?? "This connection" }
    var address: String { status["url"].string ?? status["public_url"].text }

    var connectionMessage: String {
        if phase == "setup" { return status["setup_message"].isNull ? "\(providerLabel) needs a little setup on this computer first." : "" }
        if phase == "running" { return "\(providerLabel) is live at \(status["url"].text) — your phone can reach this computer." }
        if phase == "error" { return status["message"].string ?? "\(providerLabel) could not be started." }
        if isBusy { return status["message"].string ?? "Opening the connection…" }
        if helperRequired && !helperReady { return "Download the Cloudflare helper first." }
        if !status["public_url"].text.isEmpty && !status["public_url_is_managed"].bool { return "Using your own address: \(status["public_url"].text)" }
        return "Ready to turn on \(providerLabel) for this computer."
    }

    var helperMessage: String {
        if !helperRequired { return helper["message"].string ?? "This option needs no download." }
        if helper["phase"].text == "ready" { return "The Cloudflare helper is ready on this computer." }
        if helper["phase"].text == "error" { return helper["message"].string ?? "The Cloudflare helper could not be downloaded." }
        if helper["busy"].bool { return helper["message"].string ?? "Downloading the Cloudflare helper…" }
        return "The Cloudflare helper is not here yet — Rainette downloads it for you, once."
    }

    var stabilityMessage: String {
        guard !status["provider"].text.isEmpty else { return "" }
        let address = status["stable_hostname"].bool
            ? "Address stays the same, so your phone only scans the code once."
            : "Address changes each time you restart Rainette, so your phone has to scan a new code."
        if case .bool(false) = status["public"] { return address + " Reachable only from your own devices, never from the public internet." }
        return address
    }

    var setupAction: String { status["setup_action"].text }
    var setupMessage: String { status["setup_message"].text }
    var setupDetail: String { status["setup_detail"].text }
    var setupCanFix: Bool { status["setup_can_fix"].bool }
    var setupFixLabel: String { status["setup_fix_label"].string ?? "Do this for me" }
    var setupURL: URL? {
        let url = status["setup_url"].text
        return url.hasPrefix("https://") && Self.setupLinkLabels[setupAction] != nil ? URL(string: url) : nil
    }

    // MARK: Lifecycle

    /// Start watching while the Phone pane is on screen.
    func activate() {
        guard !active else { return }
        active = true
        Task {
            await loadProviders()
            await loadConfig()
            await refreshTunnel()
        }
        devicePoll = Task { @MainActor [weak self] in
            while let self, self.active, !Task.isCancelled {
                await self.refreshDevices()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    func deactivate() {
        active = false
        tunnelPoll?.cancel()
        devicePoll?.cancel()
    }

    private func scheduleTunnelPoll() {
        tunnelPoll?.cancel()
        let working = isBusy || helper["busy"].bool
        let awaitingSetup = !setupAction.isEmpty && !working
        // A download is watched closely, a person finishing a sign-in patiently.
        let delay: UInt64 = working ? 1_200 : (awaitingSetup ? 2_000 : 15_000)
        tunnelPoll = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: delay * 1_000_000)
            guard let self, self.active, !Task.isCancelled else { return }
            await self.refreshTunnel()
        }
    }

    // MARK: Actions

    func refreshTunnel() async {
        do {
            let result = try await call("tunnel_status")
            guard result["ok"].bool else { throw NSError(domain: "Rainette", code: 3, userInfo: [NSLocalizedDescriptionKey: result["msg"].string ?? "The connection status is unavailable."]) }
            status = result
            if !result["provider"].text.isEmpty { selectedProvider = result["provider"].text }
        } catch {
            say(error.localizedDescription, .error)
        }
        if active { scheduleTunnelPoll() }
    }

    func refreshDevices() async {
        guard let state = try? await call("companion_management_state") else { return }
        pending = state["pending"].array.map { Device(id: $0["request_id"].text, name: $0["device_name"].string ?? "Unknown device") }
        devices = state["devices"].array.filter { !$0["revoked"].bool }
            .map { Device(id: $0["device_id"].text, name: $0["name"].string ?? "Paired phone") }
    }

    func loadProviders() async {
        guard let result = try? await call("tunnel_providers"), result["ok"].bool else { return }
        providers = result["providers"].array.map {
            Provider(id: $0["id"].text, label: $0["label"].text, description: $0["description"].text, recommended: $0["recommended"].bool)
        }
        selectedProvider = result["selected"].string ?? selectedProvider
        providerConfig = result["config"].object.compactMapValues(\.string)
    }

    func loadConfig() async {
        guard let config = try? await call("pwa_config_get"), config["ok"].bool else { return }
        pwaURL = config["pwa_url"].string ?? "https://music-pwa-web.vercel.app"
        publicURL = config["public_url"].text
    }

    func applyProvider(_ id: String) async {
        say("Saving this choice…", .neutral)
        do {
            let settings = JSON.object(providerConfig.mapValues { .string($0.trimmingCharacters(in: .whitespaces)) })
            let result = try await call("tunnel_set_provider", [.string(id), settings])
            guard result["ok"].bool else { throw NSError(domain: "Rainette", code: 4, userInfo: [NSLocalizedDescriptionKey: result["msg"].string ?? "Rainette could not switch to that option."]) }
            selectedProvider = result["provider"].string ?? id
            message = ""
            // Ask what is still missing now, so the checklist appears at once.
            _ = try? await call("tunnel_preflight")
            await refreshTunnel()
        } catch {
            say(error.localizedDescription, .error)
        }
    }

    func runSetupStep() {
        guard !setupInFlight, !setupAction.isEmpty else { return }
        setupInFlight = true
        let step = setupAction
        Task {
            defer { setupInFlight = false; Task { await refreshTunnel() } }
            do {
                let result = try await call("tunnel_setup_step", [.string(step), [:]])
                if !result["ok"].bool { say(result["msg"].string ?? "That step could not be finished.", .error) }
            } catch {
                say(error.localizedDescription, .error)
            }
        }
    }

    func downloadHelper() async {
        do {
            let result = try await call("tunnel_helper_download")
            if !result["ok"].bool { say(result["msg"].string ?? "cloudflared could not be downloaded.", .error) }
        } catch { say(error.localizedDescription, .error) }
        await refreshTunnel()
    }

    func toggleConnection() async {
        let stopping = isRunning
        if !stopping && helperRequired && !helperReady { say("Download the Cloudflare helper first.", .error); return }
        say(stopping ? "Closing the connection…" : "Opening the connection…", .neutral)
        do {
            let result = try await call(stopping ? "tunnel_stop" : "tunnel_start")
            if !result["ok"].bool { throw NSError(domain: "Rainette", code: 5, userInfo: [NSLocalizedDescriptionKey: result["msg"].string ?? "The connection could not be started."]) }
            message = ""
        } catch { say(error.localizedDescription, .error) }
        await refreshTunnel()
    }

    func saveAddresses() async {
        do {
            let result = try await call("pwa_config_set", [.string(pwaURL), .string(publicURL)])
            guard result["ok"].bool else { throw NSError(domain: "Rainette", code: 6, userInfo: [NSLocalizedDescriptionKey: result["msg"].string ?? "Could not save these addresses."]) }
            say("Addresses saved. Create a new pairing code to use them.", .success)
            await refreshTunnel()
        } catch { say(error.localizedDescription, .error) }
    }

    func createInvitation() async {
        say("Starting the secure companion service…", .neutral)
        do {
            let result = try await call("companion_create_invitation")
            guard result["ok"].bool else { throw NSError(domain: "Rainette", code: 7, userInfo: [NSLocalizedDescriptionKey: result["msg"].string ?? "Rainette could not create a pairing code."]) }
            let qr = Self.image(fromDataURL: result["pairing_qr_data_url"].text)
            let local = result["endpoint_is_local"].bool
            invitation = Invitation(url: result["pairing_url"].text, qr: qr,
                                    expiresAt: Date(timeIntervalSince1970: result["expires_at"].double ?? 0),
                                    endpointIsLocal: local)
            say(local
                ? "This computer has no secure address yet, so this code only works in a browser on this computer. Turn on a connection above, then create the code again."
                : "Scan this code with your phone's camera, then approve it here.",
                local ? .error : .success)
        } catch { say(error.localizedDescription, .error) }
    }

    func approve(_ device: Device) async { await manage("companion_approve_request", device, done: "\(device.name) approved") }
    func reject(_ device: Device) async { await manage("companion_reject_request", device, done: "Pairing request rejected") }
    func revoke(_ device: Device) async { await manage("companion_revoke_device", device, done: "\(device.name) revoked") }

    private func manage(_ method: String, _ device: Device, done: String) async {
        do {
            let result = try await call(method, [.string(device.id)])
            if case .bool(false) = result { throw NSError(domain: "Rainette", code: 8, userInfo: [NSLocalizedDescriptionKey: "That did not go through."]) }
            if case .bool(false) = result["ok"] { throw NSError(domain: "Rainette", code: 8, userInfo: [NSLocalizedDescriptionKey: result["msg"].string ?? "That did not go through."]) }
            say(done, .success)
            app?.sounds.play(.confirm)
        } catch { say(error.localizedDescription, .error) }
        await refreshDevices()
    }

    private func say(_ text: String, _ tone: Tone) {
        message = text
        self.tone = tone
    }

    private static func image(fromDataURL value: String) -> NSImage? {
        guard let comma = value.firstIndex(of: ","), let data = Data(base64Encoded: String(value[value.index(after: comma)...])) else { return nil }
        return NSImage(data: data)
    }
}
