import AVFoundation
import Foundation
import Testing
@testable import RainettePlayerCore

/// Against the real source, from this Mac. Off by default; RAINETTE_LIVE=1.
/// These are what say the iPhone player can find, fetch and play a song
/// without a computer, short of running on an iPhone itself.
private let live = ProcessInfo.processInfo.environment["RAINETTE_LIVE"] == "1"

@Suite(.enabled(if: live), .serialized)
struct AgainstTheSource {
    @Test func searchFindsSongs() async throws {
        let songs = try await MusicSearch.search("daft punk one more time")
        #expect(songs.count >= 5)
        #expect(songs.contains { $0.artist == "Daft Punk" })
    }

    @Test func aSongResolvesToAudioThatComesInMegabytes() async throws {
        let id = try #require(try await MusicSearch.search("daft punk one more time").first?.sourceId)
        let stream = try await StreamResolver().resolve(id)
        var request = URLRequest(url: stream.url)
        request.setValue("bytes=0-1048575", forHTTPHeaderField: "Range")
        request.setValue(stream.userAgent, forHTTPHeaderField: "User-Agent")
        let started = Date()
        let (data, response) = try await URLSession.shared.data(for: request)
        let http = try #require(response as? HTTPURLResponse)
        #expect(http.statusCode == 206)
        #expect(data.count == 1_048_576)
        // An MP4 starts with its `ftyp` box.
        #expect(String(decoding: data[4..<8], as: UTF8.self) == "ftyp")
        print("1 MB in \(Date().timeIntervalSince(started))s from client \(stream.url.query?.components(separatedBy: "&").first { $0.hasPrefix("c=") } ?? "?")")
    }

    @MainActor @Test func thePlayerPlaysAndMovesOn() async throws {
        let found = try await MusicSearch.search("daft punk one more time")
        let queue = found.prefix(2).map {
            QueueItem(sourceId: $0.sourceId, title: $0.title, artist: $0.artist, thumbnailURL: $0.thumbnailURL, durationS: $0.durationS.map(Double.init))
        }
        let engine = PlayerEngine()
        engine.setVolume(0)
        let started = Date()
        let loaded = try await engine.play(Array(queue), at: 0, generation: 1)
        print("loaded in \(Date().timeIntervalSince(started))s")
        #expect(loaded.generation == 1)
        #expect(loaded.sourceId == queue[0].sourceId)

        try await Task.sleep(nanoseconds: 4_000_000_000)
        let later = engine.snapshot()
        #expect(later.playing)
        #expect(later.positionS > 2, "the song is moving: \(later.positionS)s")
        #expect(engine.player.timeControlStatus == .playing)

        // Next, the way the lock screen asks for it.
        engine.skip(1)
        var moved = engine.snapshot()
        for _ in 0..<40 where !(moved.index == 1 && moved.positionS > 1) {
            try await Task.sleep(nanoseconds: 500_000_000)
            moved = engine.snapshot()
        }
        #expect(moved.index == 1)
        #expect(moved.positionS > 1, "the next song plays: \(moved.positionS)s")

        // Seeking far in has to fetch from the middle of the file.
        engine.seek(to: 120)
        try await Task.sleep(nanoseconds: 3_000_000_000)
        #expect(engine.snapshot().positionS > 120)
        engine.stop()
    }
}
