import Foundation

@MainActor
final class RainetteClient: NSObject {
    let handshake: EngineHandshake
    var onMessage: (JSON) -> Void = { _ in }
    var onOpen: () -> Void = {}

    private(set) var isOpen = false
    private var task: URLSessionWebSocketTask?
    private var session: URLSession!
    private var outbox: [Data] = []
    private var pending: [String: (type: String, resume: (JSON) -> Void)] = [:]
    private var closedForGood = false

    init(handshake: EngineHandshake) {
        self.handshake = handshake
        super.init()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 120
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: .main)
    }

    var base: URL { handshake.baseURL }

    /// A URL on the engine that the page-level token check accepts.
    func authorizedURL(_ path: String) -> URL {
        var components = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        components.queryItems = (components.queryItems ?? []) + [URLQueryItem(name: "token", value: handshake.token)]
        return components.url!
    }

    func connect() {
        guard !closedForGood else { return }
        var components = URLComponents(url: base.appendingPathComponent("ws"), resolvingAgainstBaseURL: false)!
        components.scheme = "ws"
        components.queryItems = [URLQueryItem(name: "token", value: handshake.token)]
        let task = session.webSocketTask(with: components.url!)
        // Library and queue payloads run past the 1 MB default.
        task.maximumMessageSize = 64 * 1024 * 1024
        self.task = task
        task.resume()
        receive(on: task)
    }

    func close() {
        closedForGood = true
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        // A session holds its delegate (this client) until invalidated.
        session.invalidateAndCancel()
        isOpen = false
        for (_, waiter) in pending { waiter.resume(["ok": false, "msg": "Disconnected"]) }
        pending.removeAll()
    }

    // MARK: Sending

    func send(_ type: String, _ fields: JSONObject = [:]) {
        var message = fields
        message["type"] = .string(type)
        let data = JSON.object(message).encoded()
        if isOpen, let task {
            task.send(.string(String(decoding: data, as: UTF8.self))) { _ in }
        } else {
            outbox.append(data)
            if outbox.count > 60 { outbox.removeFirst() }
        }
    }

    /// Send a request and wait for the reply carrying its id.
    func request(_ type: String, _ fields: JSONObject = [:], timeout: TimeInterval = 15) async -> JSON {
        let id = fields["id"]?.string ?? "\(type)_\(UUID().uuidString.prefix(8))"
        var message = fields
        message["id"] = .string(id)
        return await withCheckedContinuation { continuation in
            var finished = false
            let resume: (JSON) -> Void = { value in
                guard !finished else { return }
                finished = true
                continuation.resume(returning: value)
            }
            pending[id] = (type, resume)
            send(type, message)
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                guard let self, let waiter = self.pending.removeValue(forKey: id) else { return }
                waiter.resume(["ok": false, "msg": "The request timed out."])
            }
        }
    }

    /// Call a whitelisted desktop method (main.NATIVE_API_METHODS).
    func native(_ method: String, _ args: [JSON] = []) async throws -> JSON {
        var request = URLRequest(url: base.appendingPathComponent("native/\(method)"))
        request.httpMethod = "POST"
        request.timeoutInterval = 900
        request.setValue(handshake.token, forHTTPHeaderField: "X-Rainette-Token")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = JSON.object(["args": .array(args)]).encoded()
        let (data, _) = try await session.data(for: request)
        let reply = JSON(data: data) ?? .null
        guard reply["ok"].bool else {
            throw NSError(domain: "Rainette", code: 1, userInfo: [NSLocalizedDescriptionKey: reply["msg"].string ?? "That did not work."])
        }
        return reply["result"]
    }

    // MARK: Receiving

    private func receive(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, task === self.task else { return }
                switch result {
                case .success(let message):
                    switch message {
                    case .string(let text): self.handle(Data(text.utf8))
                    case .data(let data): self.handle(data)
                    @unknown default: break
                    }
                    self.receive(on: task)
                case .failure:
                    self.dropped()
                }
            }
        }
    }

    private func handle(_ data: Data) {
        guard let message = JSON(data: data) else { return }
        if let id = message["id"].string, let waiter = pending[id], message["type"].text != waiter.type {
            pending.removeValue(forKey: id)
            waiter.resume(message)
        }
        onMessage(message)
    }

    private func opened() {
        isOpen = true
        let queued = outbox
        outbox.removeAll()
        for data in queued { task?.send(.string(String(decoding: data, as: UTF8.self))) { _ in } }
        onOpen()
    }

    private func dropped() {
        isOpen = false
        task = nil
        guard !closedForGood else { return }
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            self?.connect()
        }
    }
}

extension RainetteClient: URLSessionWebSocketDelegate {
    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        Task { @MainActor in if webSocketTask === self.task { self.opened() } }
    }

    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                                didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        Task { @MainActor in if webSocketTask === self.task { self.dropped() } }
    }
}
