import Foundation

/// Songs already heard, kept on disk in the same 1 MB pieces the loader asks
/// for, so a replay starts at once, needs no address from the source, and
/// plays with no connection. It lives in Caches, which iOS may clear under
/// storage pressure, and keeps the most recently played `limitBytes`.
///
/// A piece is only ever joined to pieces of the same file: the whole file's
/// length is stored beside them, and a download that reports another length
/// (the source now serving a different encoding) replaces what was kept.
public final class SongCache: @unchecked Sendable {
    public static let shared = SongCache()
    static let limitBytes: Int64 = 400 << 20

    private let root: URL
    private let lock = NSLock()

    public init(root: URL = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("rainette-songs", isDirectory: true)) {
        self.root = root
    }

    /// Piece `index` of song `id`, and the whole file's length, if kept.
    public func piece(_ id: String, _ index: Int64) -> (Data, Int64)? {
        lock.lock()
        defer { lock.unlock() }
        let folder = root.appendingPathComponent(id, isDirectory: true)
        guard let total = length(folder),
              let data = try? Data(contentsOf: folder.appendingPathComponent(String(index))) else { return nil }
        // Played again: the last to be trimmed.
        try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: folder.path)
        return (data, total)
    }

    public func store(_ id: String, _ index: Int64, _ data: Data, total: Int64) {
        guard !data.isEmpty, total > 0 else { return }
        lock.lock()
        defer { lock.unlock() }
        let manager = FileManager.default
        let folder = root.appendingPathComponent(id, isDirectory: true)
        if let kept = length(folder), kept != total { try? manager.removeItem(at: folder) }
        let isNew = !manager.fileExists(atPath: folder.path)
        try? manager.createDirectory(at: folder, withIntermediateDirectories: true)
        try? data.write(to: folder.appendingPathComponent(String(index)), options: .atomic)
        try? String(total).write(to: folder.appendingPathComponent("length"), atomically: true, encoding: .utf8)
        if isNew { trim() }
    }

    private func length(_ folder: URL) -> Int64? {
        (try? String(contentsOf: folder.appendingPathComponent("length"), encoding: .utf8)).flatMap { Int64($0) }
    }

    /// Least recently played songs out first, until the cache fits.
    private func trim() {
        let manager = FileManager.default
        let keys: [URLResourceKey] = [.contentModificationDateKey, .fileSizeKey]
        guard let folders = try? manager.contentsOfDirectory(at: root, includingPropertiesForKeys: keys) else { return }
        var songs = folders.map { folder -> (url: URL, used: Date, bytes: Int64) in
            let files = (try? manager.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.fileSizeKey])) ?? []
            let bytes = files.reduce(Int64(0)) { $0 + Int64((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
            let used = (try? folder.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return (folder, used, bytes)
        }.sorted { $0.used < $1.used }
        var total = songs.reduce(Int64(0)) { $0 + $1.bytes }
        while total > Self.limitBytes, !songs.isEmpty {
            let oldest = songs.removeFirst()
            try? manager.removeItem(at: oldest.url)
            total -= oldest.bytes
        }
    }
}
