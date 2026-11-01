import Foundation
import Testing
@testable import RainettePlayerCore

struct SearchParsing {
    private func fixture() throws -> Data {
        let url = try #require(Bundle.module.url(forResource: "search-songs", withExtension: "json", subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    @Test func songsComeOutShapedLikeTheComputers() throws {
        let songs = try MusicSearch.parse(fixture())
        #expect(songs.count == 3)
        let first = try #require(songs.first)
        #expect(first.sourceId == "wU26xVT_vBU")
        #expect(first.title == "One More Time (Radio Edit)")
        #expect(first.artist == "Daft Punk")
        #expect(first.durationS == 321)
        #expect(first.thumbnailURL.hasPrefix("https://"))
        #expect(first.json["source"] as? String == "youtube")
        #expect(first.json["duration_s"] as? Int == 321)
    }

    @Test func anAnswerOfAnotherShapeIsAFailureNotAnEmptyResult() {
        #expect(throws: PlayerError(PlayerError.searchFailed)) { try MusicSearch.parse(Data("{}".utf8)) }
    }

    @Test(arguments: [("3:45", 225), ("1:02:03", 3723), ("Discovery", nil), ("12", nil)])
    func lengths(text: String, seconds: Int?) {
        #expect(MusicSearch.seconds(text) == seconds)
    }
}

struct PickingAudio {
    private func candidate(_ ext: String, _ bitrate: Int, video: Bool = false) -> AudioCandidate {
        AudioCandidate(url: URL(string: "https://example.invalid/\(ext)/\(bitrate)")!, fileExtension: ext,
                       bitrate: bitrate, hasAudio: true, hasVideo: video)
    }

    @Test func theBestAACWinsAndWebMIsNeverChosen() {
        let picked = StreamResolver.rankAudio([
            candidate("webm", 160_000), candidate("m4a", 48_000), candidate("m4a", 128_000), candidate("mp4", 900_000, video: true),
        ])
        #expect(picked.map(\.bitrate) == [128_000, 48_000])
    }

    @Test func nothingPlayableIsEmpty() {
        #expect(StreamResolver.rankAudio([candidate("webm", 160_000)]).isEmpty)
    }

    private func signed(_ client: String, _ bitrate: Int) -> AudioCandidate {
        AudioCandidate(url: URL(string: "https://x.googlevideo.com/videoplayback?c=\(client)&b=\(bitrate)")!,
                       fileExtension: "m4a", bitrate: bitrate, hasAudio: true, hasVideo: false)
    }

    @Test func aClientTheSourceRefusesIsPassedOverForOneItServes() async throws {
        let ranked = [signed("TVHTML5", 130_000), signed("TVHTML5", 50_000), signed("VISIONOS", 128_000)]
        var asked: [String] = []
        let stream = try await StreamResolver.firstServed(ranked, now: Date()) { stream in
            asked.append(stream.url.query ?? "")
            return StreamResolver.client(of: stream.url) == "VISIONOS"
        }
        #expect(StreamResolver.client(of: stream.url) == "VISIONOS")
        // The second TV address is never asked for: that client already said no.
        #expect(asked == ["c=TVHTML5&b=130000", "c=VISIONOS&b=128000"])
    }

    @Test func everyClientRefusedIsAnExpiredStream() async {
        await #expect(throws: PlayerError(PlayerError.expired)) {
            try await StreamResolver.firstServed([self.signed("TVHTML5", 130_000)], now: Date()) { _ in false }
        }
    }

    @Test func expiryComesFromTheAddressOrAConservativeDefault() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let signed = URL(string: "https://x.googlevideo.com/videoplayback?expire=1021600&c=IOS")!
        #expect(StreamResolver.expiresAt(signed, now: now) == Date(timeIntervalSince1970: 1_021_600))
        let bare = URL(string: "https://x.googlevideo.com/videoplayback")!
        #expect(StreamResolver.expiresAt(bare, now: now) == now.addingTimeInterval(3600))
    }

    @Test func eachAddressIsAskedForAsTheClientItWasSignedFor() {
        let ios = URL(string: "https://x.googlevideo.com/videoplayback?c=IOS")!
        #expect(StreamResolver.userAgent(for: ios).hasPrefix("com.google.ios.youtube/"))
        let vr = URL(string: "https://x.googlevideo.com/videoplayback?c=ANDROID_VR")!
        #expect(StreamResolver.userAgent(for: vr).contains("oculus"))
    }

    @Test(arguments: ["dQw4w9WgXcQ", "a-b_c-d_e-f"])
    func validIds(id: String) { #expect(StreamResolver.isValidId(id)) }

    @Test(arguments: ["", "short", "dQw4w9WgXcQ1", "../etc/pass", "dQw4w9WgXc?"])
    func invalidIds(id: String) { #expect(!StreamResolver.isValidId(id)) }
}

struct Loading {
    @Test func theTotalLengthIsReadFromTheContentRange() {
        #expect(ChunkedLoader.totalLength("bytes 0-1023/5242880") == 5_242_880)
        #expect(ChunkedLoader.totalLength(nil) == nil)
    }

    @Test(arguments: [(403, "expired"), (410, "expired"), (429, "rate_limited"), (404, "unavailable"), (500, "offline")])
    func statusesBecomeCodesThePageWords(status: Int, code: String) {
        #expect(PlayerError.http(status).code == code)
    }

    @Test func aDroppedConnectionIsOfflineAndAWithdrawnRequestIsCanceled() {
        #expect(PlayerError.classify(URLError(.notConnectedToInternet)).code == "offline")
        #expect(PlayerError.classify(URLError(.cancelled)).code == "canceled")
    }
}

@MainActor
struct EngineRules {
    private let songs = (1...3).map {
        QueueItem(sourceId: String(format: "song%07d", $0), title: "Song \($0)", artist: "A", thumbnailURL: "", durationS: 200)
    }

    @Test func aQueueOfSomethingElseIsRefused() async {
        let engine = PlayerEngine()
        let bad = [QueueItem(sourceId: "file:///etc", title: "", artist: "", thumbnailURL: "", durationS: nil)]
        await #expect(throws: PlayerError.self) { try await engine.play(bad, at: 0, generation: 1) }
        #expect(engine.queue.isEmpty)
    }

    @Test func aQueueEditForASongThatIsNotPlayingIsRefused() {
        let engine = PlayerEngine()
        #expect(throws: PlayerError(PlayerError.mismatch, "The queue moved on.")) {
            try engine.setQueue(songs, at: 0, generation: 2)
        }
    }

    @Test func aNewerPlayCancelsOneStillLoading() async throws {
        let engine = PlayerEngine()
        engine.setVolume(0)
        let first = Task { try await engine.play(songs, at: 0, startPaused: true, generation: 1) }
        await Task.yield()
        let second = Task { try await engine.play(songs, at: 1, startPaused: true, generation: 2) }
        await Task.yield()
        await #expect(throws: PlayerError.self) { _ = try await first.value }
        #expect(engine.generation == 2)
        #expect(engine.snapshot().sourceId == "song0000002")
        engine.stop()
        _ = await second.result
    }
}
