import Foundation

struct EngineHandshake: Equatable, Sendable {
    let port: Int
    let token: String
    let version: String

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }
}

enum EngineError: LocalizedError {
    case notFound
    case exitedEarly(Int32)
    case timedOut

    var errorDescription: String? {
        switch self {
        case .notFound: return "Rainette's music engine is missing from this copy of the app. Reinstall Rainette Music."
        case .exitedEarly(let status): return "Rainette's music engine stopped while starting (code \(status))."
        case .timedOut: return "Rainette's music engine took too long to start."
        }
    }
}

enum EngineLocator {
    static let bundledName = "rainette-engine"

    struct Command {
        let executable: URL
        let arguments: [String]
        let directory: URL?
    }

    static func command(engineMode: Bool) -> Command? {
        let flag = engineMode ? ["--engine"] : []
        let env = ProcessInfo.processInfo.environment
        if let source = env["RAINETTE_SOURCE_DIR"], !source.isEmpty {
            let python = env["RAINETTE_PYTHON"].flatMap { $0.isEmpty ? nil : $0 } ?? "/usr/bin/python3"
            let directory = URL(fileURLWithPath: source)
            return Command(executable: URL(fileURLWithPath: python),
                           arguments: [directory.appendingPathComponent("main.py").path] + flag,
                           directory: directory)
        }
        let bundled = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/\(bundledName)")
        guard FileManager.default.isExecutableFile(atPath: bundled.path) else { return nil }
        return Command(executable: bundled, arguments: flag, directory: nil)
    }
}

/// Launches the engine and owns its lifetime.
@MainActor
final class EngineProcess {
    nonisolated static let handshakePrefix = "RAINETTE_ENGINE "
    nonisolated static let startupTimeout: TimeInterval = 90

    /// Called once when an engine that had handed over exits on its own.
    var onExit: ((Int32) -> Void)?

    private var process: Process?
    private var stdinPipe: Pipe?
    private var stopping = false
    private var handshaken = false

    var isRunning: Bool { process?.isRunning ?? false }

    func start() async throws -> EngineHandshake {
        guard let command = EngineLocator.command(engineMode: true) else { throw EngineError.notFound }
        stopping = false
        handshaken = false
        let process = Process()
        process.executableURL = command.executable
        process.arguments = command.arguments
        if let directory = command.directory { process.currentDirectoryURL = directory }
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        if let home = environment["RAINETTE_ENGINE_HOME"], !home.isEmpty { environment["HOME"] = home }
        process.environment = environment
        let stdin = Pipe()
        let stdout = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            Task { @MainActor in self?.engineExited(finished, status: status) }
        }
        try process.run()
        self.process = process
        self.stdinPipe = stdin

        do {
            let handshake = try await readHandshake(from: stdout.fileHandleForReading, process: process)
            handshaken = true
            return handshake
        } catch {
            stop()
            throw error
        }
    }

    /// Close the engine's stdin and give it a moment to shut its tunnel down.
    func stop() {
        stopping = true
        guard let process else { return }
        try? stdinPipe?.fileHandleForWriting.close()
        stdinPipe = nil
        let deadline = Date().addingTimeInterval(4)
        while process.isRunning && Date() < deadline { usleep(50_000) }
        if process.isRunning { process.terminate() }
        self.process = nil
    }

    private func abandon(_ starting: Process) {
        stopping = true
        starting.terminate()
    }

    private func engineExited(_ finished: Process, status: Int32) {
        guard finished === process else { return }
        process = nil
        stdinPipe = nil
        if handshaken && !stopping { onExit?(status) }
    }

    private func readHandshake(from handle: FileHandle, process: Process) async throws -> EngineHandshake {
        try await withThrowingTaskGroup(of: EngineHandshake.self) { group in
            group.addTask {
                for try await line in handle.bytes.lines {
                    guard line.hasPrefix(Self.handshakePrefix),
                          let json = JSON(data: Data(line.dropFirst(Self.handshakePrefix.count).utf8)),
                          let port = json["port"].int, let token = json["token"].string else { continue }
                    // Keep draining so a chatty engine never blocks on a full pipe.
                    Task.detached { for try await _ in handle.bytes.lines {} }
                    return EngineHandshake(port: port, token: token, version: json["version"].text)
                }
                throw EngineError.exitedEarly(process.isRunning ? -1 : process.terminationStatus)
            }
            group.addTask { [self] in
                try await Task.sleep(nanoseconds: UInt64(Self.startupTimeout * 1_000_000_000))
                await abandon(process)
                throw EngineError.timedOut
            }
            defer { group.cancelAll() }
            return try await group.next()!
        }
    }
}
