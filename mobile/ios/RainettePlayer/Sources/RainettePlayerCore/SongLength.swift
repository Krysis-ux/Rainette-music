import Foundation

/// How long a song really is, read from the first piece of its file.
///
/// The source's audio is fragmented MP4 whose `mvhd` already holds the whole
/// length, and AVPlayer adds the fragments on top of it: every song read as
/// about twice its length, so CarPlay and the lock screen showed a doubled
/// timeline and the player played silence through the second half. The `sidx`
/// index sums the fragments and is the true length; `mvhd` stands in when a
/// file has none.
enum SongLength {
    static func seconds(in data: Data) -> Double? {
        let bytes = [UInt8](data)
        var fromHeader: Double?
        for (kind, body) in boxes(bytes, 0..<bytes.count) {
            if kind == "sidx" { return sidx(bytes, body) }
            if kind == "moov", let mvhd = boxes(bytes, body).first(where: { $0.0 == "mvhd" }) {
                fromHeader = header(bytes, mvhd.1)
            }
        }
        return fromHeader
    }

    /// The boxes directly inside `range`, as (type, body). Stops at the first
    /// one that runs past the data, which is where the piece ends.
    private static func boxes(_ bytes: [UInt8], _ range: Range<Int>) -> [(String, Range<Int>)] {
        var found: [(String, Range<Int>)] = []
        var at = range.lowerBound
        while at + 8 <= range.upperBound {
            let size = Int(uint(bytes, at, 4))
            guard size >= 8, at + size <= range.upperBound else { break }
            found.append((String(decoding: bytes[at + 4..<at + 8], as: UTF8.self), at + 8..<at + size))
            at += size
        }
        return found
    }

    private static func sidx(_ bytes: [UInt8], _ body: Range<Int>) -> Double? {
        let at = body.lowerBound
        guard body.count >= 12 else { return nil }
        let timescale = uint(bytes, at + 8, 4)
        var entry = at + 12 + (bytes[at] == 0 ? 8 : 16) + 2
        guard timescale > 0, entry + 2 <= body.upperBound else { return nil }
        let count = Int(uint(bytes, entry, 2))
        entry += 2
        var total: UInt64 = 0
        for _ in 0..<count {
            guard entry + 12 <= body.upperBound else { return nil }
            total += uint(bytes, entry + 4, 4)
            entry += 12
        }
        return total > 0 ? Double(total) / Double(timescale) : nil
    }

    private static func header(_ bytes: [UInt8], _ body: Range<Int>) -> Double? {
        let at = body.lowerBound
        let long = bytes[at] == 1
        guard body.count >= (long ? 32 : 20) else { return nil }
        let timescale = uint(bytes, at + (long ? 20 : 12), 4)
        let duration = uint(bytes, at + (long ? 24 : 16), long ? 8 : 4)
        return timescale > 0 && duration > 0 ? Double(duration) / Double(timescale) : nil
    }

    private static func uint(_ bytes: [UInt8], _ at: Int, _ width: Int) -> UInt64 {
        bytes[at..<at + width].reduce(0) { $0 << 8 | UInt64($1) }
    }
}
