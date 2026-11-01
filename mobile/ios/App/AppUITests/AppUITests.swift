import XCTest

/// The iPhone app on a simulator, used the way a person uses it: search, play,
/// seek, leave the app, come back, skip. It asks the real source over the
/// internet, so like mobile/scripts/emulator_check.py on Android it is run by
/// hand rather than in CI (see mobile/README.md). With RAINETTE_OUT set (as
/// TEST_RUNNER_RAINETTE_OUT), it leaves timings, and a screenshot and the
/// accessibility tree of any step that fails, in that folder.
final class PlaysOnThePhone: XCTestCase {
    private let app = XCUIApplication()
    private let out = ProcessInfo.processInfo.environment["RAINETTE_OUT"]
    private var seek: XCUIElement { app.sliders["Seek"].firstMatch }

    override func setUp() {
        continueAfterFailure = false
        app.launch()
    }

    func testFindPlaySeekLeaveAndSkip() throws {
        app.buttons["Search"].firstMatch.tap()
        let field = app.searchFields["Search music"]
        XCTAssert(field.waitForExistence(timeout: 30))
        field.tap()
        // "Get Lucky" is the song the first iPhone build could not play.
        field.typeText("daft punk get lucky\n")

        let song = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Play '")).firstMatch
        wait("search results", timeout: 20) { song.exists }
        let started = Date()
        song.tap()
        app.buttons["Open the now playing screen"].firstMatch.tap()
        wait("the song to play") { (position()?.at ?? 0) >= 2 }
        note("playing after \(Int(Date().timeIntervalSince(started)))s")

        // Seeking far in has to fetch from the middle of the file.
        let length = try XCTUnwrap(position()).of
        let from = seek.coordinate(withNormalizedOffset: CGVector(dx: 0.02, dy: 0.5))
        from.press(forDuration: 0.2, thenDragTo: seek.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)))
        wait("the seek to land") { (position()?.at ?? 0) >= length * 2 / 5 }
        let beforeLeaving = try XCTUnwrap(position()).at
        note("seeked to \(beforeLeaving)s of \(length)s")

        // Away from the app the music goes on, and the page catches up on return.
        XCUIDevice.shared.press(.home)
        sleep(15)
        app.activate()
        wait("the song to have kept playing") { (position()?.at ?? 0) >= beforeLeaving + 12 }
        note("back at \(position()?.at ?? -1)s after 15s away")

        app.buttons["Next track"].firstMatch.tap()
        wait("the next song to play") { (position().map { $0.at >= 1 && $0.at < 30 }) ?? false }

        let failures = app.staticTexts.matching(NSPredicate(
            format: "label CONTAINS[c] 'expired' OR label CONTAINS[c] 'stopped unexpectedly' OR label CONTAINS[c] 'failed'"))
        XCTAssertEqual(failures.count, 0, failures.allElementsBoundByIndex.map(\.label).joined(separator: "; "))
    }

    /// "1:23 of 4:09", the seek slider's own description, in seconds.
    private func position() -> (at: Int, of: Int)? {
        guard seek.exists, let value = seek.value as? String else { return nil }
        let parts = value.components(separatedBy: " of ").map(seconds)
        guard parts.count == 2, let at = parts[0], let of = parts[1] else { return nil }
        return (at, of)
    }

    private func seconds(_ text: String) -> Int? {
        let parts = text.split(separator: ":").map { Int($0) }
        guard parts.count >= 2, parts.allSatisfy({ $0 != nil }) else { return nil }
        return parts.reduce(0) { $0 * 60 + ($1 ?? 0) }
    }

    private func wait(_ what: String, timeout: TimeInterval = 40, until done: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if done() { return }
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        }
        if let out {
            try? app.debugDescription.write(toFile: "\(out)/failed.txt", atomically: true, encoding: .utf8)
            try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: "\(out)/failed.png"))
        }
        XCTFail("Waited \(Int(timeout))s for \(what)")
    }

    private func note(_ line: String) {
        guard let out, let data = "\(line)\n".data(using: .utf8) else { return }
        let path = "\(out)/summary.txt"
        if let handle = FileHandle(forWritingAtPath: path) {
            handle.seekToEndOfFile()
            handle.write(data)
            handle.closeFile()
        } else {
            FileManager.default.createFile(atPath: path, contents: data)
        }
    }
}
