import AppKit
import SwiftUI

@available(macOS 14, *)
struct SettingsView: View {
    @Environment(Router.self) private var router

    var body: some View {
        @Bindable var router = router
        TabView(selection: $router.settingsTab) {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }.tag(SettingsTab.general)
            PlaybackSettings().tabItem { Label("Playback", systemImage: "play.circle") }.tag(SettingsTab.playback)
            EqualizerPanel().padding(28).frame(width: 460).tabItem { Label("Equalizer", systemImage: "slider.vertical.3") }.tag(SettingsTab.equalizer)
            PhoneSettings().tabItem { Label("Phone", systemImage: "iphone.radiowaves.left.and.right") }.tag(SettingsTab.phone)
            UpdateSettings().tabItem { Label("Updates", systemImage: "arrow.down.app") }.tag(SettingsTab.updates)
        }
        .scenePadding()
        .frame(minWidth: 520)
    }
}

@available(macOS 14, *)
private struct GeneralSettings: View {
    @Environment(AppModel.self) private var app
    @AppStorage(Preference.appearance) private var appearance = "system"
    @AppStorage(Preference.layout) private var layout = "grid"
    @AppStorage(SoundEffects.enabledKey) private var soundsOn = false
    @AppStorage(SoundEffects.volumeKey) private var soundsVolume = 0.6

    var body: some View {
        Form {
            Picker("Appearance", selection: $appearance) {
                Text("Match System").tag("system"); Text("Light").tag("light"); Text("Dark").tag("dark")
            }
            Picker("Show collections as", selection: $layout) {
                Label("Grid", systemImage: "square.grid.2x2").tag("grid")
                Label("List", systemImage: "list.bullet").tag("list")
            }
            .pickerStyle(.inline)
            Section {
                Toggle("Play interface sounds", isOn: $soundsOn)
                    .onChange(of: soundsOn) { _, on in if on { app.sounds.warm(); app.sounds.play(.confirm) } }
                Slider(value: $soundsVolume, in: 0.1...1) {
                    Text("Volume")
                } minimumValueLabel: {
                    Image(systemName: "speaker.fill")
                } maximumValueLabel: {
                    Image(systemName: "speaker.wave.3.fill")
                } onEditingChanged: { editing in
                    if !editing { app.sounds.play(.tap) }
                }
                .disabled(!soundsOn)
                HStack(spacing: 8) {
                    ForEach([("Tap", SoundEffects.Effect.tap), ("Play", .play), ("Pause", .pause), ("Skip", .skip), ("Done", .confirm)], id: \.0) { name, effect in
                        Button(name) { app.sounds.play(effect, force: true) }.controlSize(.small)
                    }
                }
            } header: {
                Text("Sound Effects")
            } footer: {
                Text("Soft clicks when you press play, skip, or add a song. They follow the volume above, not your music.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Section {
                LabeledContent("Classic interface") {
                    Button("Relaunch in Classic") { ClassicInterface.relaunchClassic() }
                }
            } header: {
                Text("Interface")
            } footer: {
                Text("Opens the previous interface once. Rainette returns to this one the next time it starts.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
    }
}

@available(macOS 14, *)
private struct PlaybackSettings: View {
    @Environment(AppModel.self) private var app
    @AppStorage(Preference.fadePlayPause) private var fade = true
    @AppStorage(Preference.autoplaySimilar) private var autoplay = false

    var body: some View {
        Form {
            Toggle(isOn: $fade) {
                Text("Fade on play and pause")
                Text("A short volume ramp instead of a hard stop.")
            }
            Toggle(isOn: $autoplay) {
                Text("Keep playing similar music")
                Text("When the queue runs out, Rainette starts a mix from the last song.")
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
        .onChange(of: fade) { app.pushEngineSettings() }
        .onChange(of: autoplay) { app.pushEngineSettings() }
    }
}

// MARK: - Phone

@available(macOS 14, *)
private struct PhoneSettings: View {
    @Environment(PhoneModel.self) private var phone

    var body: some View {
        @Bindable var phone = phone
        Form {
            Section {
                Picker("Connect with", selection: Binding(get: { phone.selectedProvider }, set: { id in Task { await phone.applyProvider(id) } })) {
                    ForEach(phone.providers) { provider in
                        Text(provider.recommended ? "\(provider.label) (recommended)" : provider.label).tag(provider.id)
                    }
                }
                if let provider = phone.providers.first(where: { $0.id == phone.selectedProvider }), !provider.description.isEmpty {
                    Text(provider.description).font(.callout).foregroundStyle(.secondary)
                }
                if !phone.stabilityMessage.isEmpty {
                    Label(phone.stabilityMessage, systemImage: "info.circle").font(.callout).foregroundStyle(.secondary)
                }
                ForEach(PhoneModel.providerFields[phone.selectedProvider] ?? [], id: \.key) { field in
                    TextField(field.label, text: Binding(get: { phone.providerConfig[field.key] ?? "" }, set: { phone.providerConfig[field.key] = $0 }),
                              prompt: Text(field.placeholder))
                }
                if PhoneModel.providerFields[phone.selectedProvider] != nil {
                    Button("Save These Details") { Task { await phone.applyProvider(phone.selectedProvider) } }
                }
            } header: {
                Text("1 · Reach This Mac")
            }

            if !phone.setupAction.isEmpty || !phone.setupMessage.isEmpty {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Label(phone.setupMessage, systemImage: "checklist").font(.body.weight(.medium))
                        if !phone.setupDetail.isEmpty { Text(phone.setupDetail).font(.callout).foregroundStyle(.secondary) }
                    }
                    HStack {
                        if phone.setupCanFix {
                            Button(phone.setupInFlight ? "Working…" : phone.setupFixLabel) { phone.runSetupStep() }
                                .buttonStyle(.borderedProminent)
                                .disabled(phone.setupInFlight)
                        }
                        if let url = phone.setupURL {
                            Link(PhoneModel.setupLinkLabels[phone.setupAction] ?? "Open", destination: url)
                        }
                    }
                }
            }

            Section {
                HStack(spacing: 10) {
                    Circle().fill(phone.isRunning ? Color.green : (phone.phase == "error" ? .orange : Color.secondary.opacity(0.5)))
                        .frame(width: 9, height: 9)
                    Text(phone.connectionMessage.isEmpty ? phone.providerLabel : phone.connectionMessage)
                        .font(.callout).textSelection(.enabled)
                }
                if phone.helperRequired && !(phone.setupAction == "install" && phone.setupCanFix) {
                    HStack {
                        Text(phone.helperMessage).font(.callout).foregroundStyle(.secondary)
                        Spacer()
                        if !phone.helperReady {
                            Button(phone.helper["busy"].bool ? "Downloading…" : "Download") { Task { await phone.downloadHelper() } }
                                .disabled(phone.helper["busy"].bool)
                        }
                    }
                }
                Button(phone.isRunning ? "Turn Connection Off" : (phone.isBusy ? "Working…" : "Turn Connection On")) {
                    Task { await phone.toggleConnection() }
                }
                .disabled(phone.isBusy || (phone.helperRequired && !phone.helperReady && !phone.isRunning))
            }

            Section {
                if let invitation = phone.invitation {
                    HStack(alignment: .top, spacing: 18) {
                        if let qr = invitation.qr {
                            Image(nsImage: qr).interpolation(.none).resizable().frame(width: 156, height: 156)
                                .padding(8).background(.white, in: RoundedRectangle(cornerRadius: 12))
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            TimelineView(.periodic(from: .now, by: 1)) { context in
                                let remaining = Int(invitation.expiresAt.timeIntervalSince(context.date))
                                Text(remaining > 0 ? "Expires in \(remaining / 60):\(String(format: "%02d", remaining % 60))" : "This code expired. Create a new one.")
                                    .font(.callout.monospacedDigit()).foregroundStyle(remaining > 0 ? Color.secondary : .orange)
                            }
                            Button("Copy Link") {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(invitation.url, forType: .string)
                            }
                            Button("New Code") { Task { await phone.createInvitation() } }
                        }
                    }
                } else {
                    Button("Create Pairing Code") { Task { await phone.createInvitation() } }
                        .buttonStyle(.borderedProminent)
                }
                if !phone.message.isEmpty {
                    Text(phone.message).font(.callout)
                        .foregroundStyle(phone.tone == .error ? Color.orange : (phone.tone == .success ? Color.green : .secondary))
                }
            } header: {
                Text("2 · Pair Your Phone")
            }

            Section("Waiting for Approval") {
                if phone.pending.isEmpty {
                    Text("No phones are waiting.").foregroundStyle(.secondary)
                }
                ForEach(phone.pending) { request in
                    HStack {
                        Label(request.name, systemImage: "iphone")
                        Spacer()
                        Button("Reject") { Task { await phone.reject(request) } }
                        Button("Approve") { Task { await phone.approve(request) } }.buttonStyle(.borderedProminent)
                    }
                }
            }

            Section("Paired Phones") {
                if phone.devices.isEmpty {
                    Text("No phones are paired yet.").foregroundStyle(.secondary)
                }
                ForEach(phone.devices) { device in
                    HStack {
                        Label(device.name, systemImage: "iphone")
                        Spacer()
                        Button("Revoke", role: .destructive) { Task { await phone.revoke(device) } }
                    }
                }
            }

            Section("Advanced") {
                TextField("Phone app address", text: $phone.pwaURL)
                TextField("This Mac's public address", text: $phone.publicURL, prompt: Text("Filled in by the connection above"))
                Button("Save Addresses") { Task { await phone.saveAddresses() } }
            }
        }
        .formStyle(.grouped)
        .frame(width: 560, height: 640)
        .onAppear { phone.activate() }
        .onDisappear { phone.deactivate() }
        .background(WindowLifecycle(onShow: { phone.activate() }, onClose: { phone.deactivate() }))
    }
}

/// Reports the hosting window closing and coming back to the front.
@available(macOS 14, *)
private struct WindowLifecycle: NSViewRepresentable {
    let onShow: () -> Void
    let onClose: () -> Void

    func makeNSView(context: Context) -> Tracker {
        let tracker = Tracker()
        tracker.onShow = onShow
        tracker.onClose = onClose
        return tracker
    }

    func updateNSView(_ tracker: Tracker, context: Context) {}

    final class Tracker: NSView {
        var onShow: () -> Void = {}
        var onClose: () -> Void = {}
        private var observers: [NSObjectProtocol] = []

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            observers.forEach(NotificationCenter.default.removeObserver)
            observers.removeAll()
            guard let window else { return }
            let center = NotificationCenter.default
            observers.append(center.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.onClose() }
            })
            observers.append(center.addObserver(forName: NSWindow.didBecomeKeyNotification, object: window, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.onShow() }
            })
        }

        deinit { observers.forEach(NotificationCenter.default.removeObserver) }
    }
}

// MARK: - Updates

@available(macOS 14, *)
private struct UpdateSettings: View {
    @Environment(AppModel.self) private var app
    @State private var result: JSON = .null
    @State private var checking = false
    @State private var progress: JSON = .null
    @State private var error: String?

    var body: some View {
        Form {
            LabeledContent("Version", value: app.engineVersion.isEmpty ? "—" : app.engineVersion)
            Section {
                HStack {
                    Text(statusLine).foregroundStyle(.secondary)
                    Spacer()
                    if checking { ProgressView().controlSize(.small) }
                    Button("Check Now") { Task { await check() } }.disabled(checking || installing)
                }
                if result["status"].text == "update" {
                    if !result["notes"].text.isEmpty {
                        ScrollView { Text(result["notes"].text).font(.callout).frame(maxWidth: .infinity, alignment: .leading) }
                            .frame(maxHeight: 140)
                    }
                    if installing {
                        ProgressView(value: fraction) { Text(progressLabel).font(.callout) }
                    } else {
                        Button("Install \(result["latest"].text)") { Task { await install() } }.buttonStyle(.borderedProminent)
                    }
                }
                if let error { Text(error).font(.callout).foregroundStyle(.orange) }
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
        .task { await check() }
    }

    private var installing: Bool { ["downloading", "verifying", "launching", "installing"].contains(progress["phase"].text) }
    private var fraction: Double {
        let total = progress["total"].double ?? 0
        return total > 0 ? (progress["received"].double ?? 0) / total : 0
    }
    private var progressLabel: String {
        switch progress["phase"].text {
        case "downloading": return "Downloading…"
        case "verifying": return "Verifying the signature…"
        default: return "Installing — Rainette will reopen by itself."
        }
    }

    private var statusLine: String {
        switch result["status"].text {
        case "update": return "Rainette \(result["latest"].text) is available."
        case "current": return "Rainette is up to date."
        case "check_failed": return "Couldn't reach the update server."
        case "unavailable": return result["msg"].string ?? "Updates aren't available for this copy."
        default: return checking ? "Checking…" : ""
        }
    }

    private func check() async {
        guard let client = app.client else { return }
        checking = true
        result = (try? await client.native("check_for_updates")) ?? ["status": "check_failed"]
        checking = false
    }

    private func install() async {
        guard let client = app.client else { return }
        error = nil
        do {
            let started = try await client.native("apply_update", [.string(result["candidate_id"].text)])
            guard started["status"].text == "installing" else { error = started["msg"].string ?? "The update could not start."; return }
            while true {
                progress = try await client.native("update_progress")
                if progress["phase"].text == "failed" { error = progress["message"].string ?? "The update failed."; return }
                try await Task.sleep(nanoseconds: 250_000_000)
            }
        } catch {
            // The engine quitting to install the update ends this loop too.
            if progress["phase"].text != "installing" { self.error = error.localizedDescription }
        }
    }
}
