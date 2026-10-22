import AppKit

enum ClassicInterface {
    /// Sticks until deleted with `defaults delete`; the documented escape hatch.
    static let defaultsKey = "UseClassicInterface"
    /// Set by the menu item, consumed by the next launch.
    static let onceKey = "UseClassicInterfaceOnce"

    static var requested: Bool {
        let defaults = UserDefaults.standard
        if defaults.bool(forKey: onceKey) {
            defaults.removeObject(forKey: onceKey)
            return true
        }
        return defaults.bool(forKey: defaultsKey) || ProcessInfo.processInfo.environment["RAINETTE_CLASSIC"] == "1"
    }

    /// Become the engine in its classic, windowed mode.
    static func launch() -> Never {
        guard let command = EngineLocator.command(engineMode: false) else {
            let alert = NSAlert()
            alert.messageText = "Rainette Music can't start"
            alert.informativeText = EngineError.notFound.errorDescription ?? ""
            alert.runModal()
            exit(1)
        }
        if let directory = command.directory { FileManager.default.changeCurrentDirectoryPath(directory.path) }
        let argv = ([command.executable.path] + command.arguments).map { strdup($0) } + [nil]
        execv(command.executable.path, argv)
        perror("execv")
        exit(1)
    }

    /// Relaunch once in the classic interface; the launch after is native again.
    static func relaunchClassic() {
        UserDefaults.standard.set(true, forKey: onceKey)
        relaunch()
    }

    static func relaunch() {
        let path = Bundle.main.bundlePath
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        // Wait for this instance to be gone, or `open` just re-activates it.
        task.arguments = ["-c", "while kill -0 \(getpid()) 2>/dev/null; do sleep 0.2; done; open \"\(path)\""]
        try? task.run()
        NSApp.terminate(nil)
    }
}
