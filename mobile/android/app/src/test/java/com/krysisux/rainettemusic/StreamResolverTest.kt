package com.krysisux.rainettemusic

import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.schabi.newpipe.extractor.exceptions.ContentNotAvailableException
import org.schabi.newpipe.extractor.exceptions.GeographicRestrictionException
import org.schabi.newpipe.extractor.exceptions.ParsingException
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException
import java.io.IOException
import java.net.UnknownHostException

@UnstableApi
@RunWith(RobolectricTestRunner::class)
class StreamResolverTest {

    private fun candidate(itag: Int, bitrate: Int, suffix: String, url: String = "https://r1.example/$itag", isUrl: Boolean = true) =
        StreamResolver.Candidate(url, itag, bitrate, suffix, isUrl)

    @Test
    fun `a quick player answer gives its direct audio addresses and nothing else`() {
        val answer = JSONObject(
            """{"playabilityStatus":{"status":"OK"},"streamingData":{"adaptiveFormats":[
              {"itag":140,"mimeType":"audio/mp4; codecs=\"mp4a.40.2\"","averageBitrate":129000,"url":"https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=140"},
              {"itag":137,"mimeType":"video/mp4","bitrate":4000000,"url":"https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=137"},
              {"itag":139,"mimeType":"audio/mp4","signatureCipher":"s=x&url=y"}]}}""".trimIndent(),
        )
        val best = StreamResolver.pickAudio(StreamResolver.parsePlayer(answer))
        assertEquals("https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=140", best?.url)
    }

    @Test
    fun `anything but a playable answer takes the full path`() {
        try {
            StreamResolver.parsePlayer(JSONObject("""{"playabilityStatus":{"status":"LOGIN_REQUIRED"}}"""))
            fail("a refusal is not an empty list")
        } catch (e: StreamResolver.ResolveException) {
            assertEquals(StreamResolver.SOURCE_CHANGED, e.code)
        }
    }

    @Test
    fun `AAC wins over a higher-bitrate Opus, and the best AAC wins among AAC`() {
        val picked = StreamResolver.pickAudio(listOf(
            candidate(251, 160, "webm"), candidate(139, 48, "m4a"), candidate(140, 128, "m4a"),
        ))
        assertEquals(140, picked?.itag)
    }

    @Test
    fun `Opus is used when there is no AAC`() {
        assertEquals(251, StreamResolver.pickAudio(listOf(candidate(250, 70, "webm"), candidate(251, 160, "webm")))?.itag)
    }

    @Test
    fun `manifests and non-https URLs are never picked`() {
        assertNull(StreamResolver.pickAudio(listOf(
            candidate(140, 128, "m4a", isUrl = false),
            candidate(141, 256, "m4a", url = "http://plain.example/141"),
        )))
    }

    @Test
    fun `expiry comes from the URL, with a default when absent or already past`() {
        val now = 1_700_000_000_000L
        assertEquals(1_793_129_119_000L, StreamResolver.expiresAt("https://x/videoplayback?expire=1793129119&ei=a", now))
        assertEquals(now + 3_600_000L, StreamResolver.expiresAt("https://x/videoplayback?ei=a", now))
        assertEquals(now + 3_600_000L, StreamResolver.expiresAt("https://x/videoplayback?expire=1", now))
    }

    @Test
    fun `video ids are read from catalog URLs and nothing else is accepted`() {
        assertEquals("wU26xVT_vBU", StreamResolver.videoIdFrom("https://music.youtube.com/watch?v=wU26xVT_vBU"))
        assertEquals("wU26xVT_vBU", StreamResolver.videoIdFrom("https://www.youtube.com/watch?list=x&v=wU26xVT_vBU&t=3"))
        assertNull(StreamResolver.videoIdFrom("https://www.youtube.com/watch?v=short"))
        assertNull(StreamResolver.videoIdFrom(null))
        assertTrue(StreamResolver.isValidId("a-b_C123456"))
        assertTrue(!StreamResolver.isValidId("../../etc/x"))
    }

    @Test
    fun `an invalid id is refused before any network`() {
        try {
            StreamResolver.resolve("not an id")
            fail("expected a refusal")
        } catch (e: StreamResolver.ResolveException) {
            assertEquals(StreamResolver.INVALID, e.code)
        }
    }

    @Test
    fun `failures are told apart by what they mean to a listener`() {
        assertEquals(StreamResolver.REGION, StreamResolver.classify(GeographicRestrictionException("x")).code)
        assertEquals(StreamResolver.RATE_LIMITED, StreamResolver.classify(ReCaptchaException("x", "u")).code)
        assertEquals(StreamResolver.UNAVAILABLE, StreamResolver.classify(ContentNotAvailableException("x")).code)
        assertEquals(StreamResolver.OFFLINE, StreamResolver.classify(UnknownHostException("youtube.com")).code)
        assertEquals(StreamResolver.OFFLINE, StreamResolver.classify(IOException("reset")).code)
        // What an unknown id actually throws (seen in the JVM proof of concept).
        assertEquals(StreamResolver.SOURCE_CHANGED, StreamResolver.classify(ParsingException("JSON response is too short")).code)
        val already = StreamResolver.ResolveException(StreamResolver.UNSUPPORTED, "x")
        assertSame(already, StreamResolver.classify(already))
    }

    @Test
    fun `only rainette URIs are resolved, and only for valid ids`() {
        assertEquals("abcdefghijk", PlaybackService.idOf(PlaybackService.streamUri("abcdefghijk")))
        assertNull(PlaybackService.idOf(Uri.parse("https://evil.example/abcdefghijk")))
        assertNull(PlaybackService.idOf(Uri.parse("rainette://youtube/bad")))
        val untouched = DataSpec(Uri.parse("https://example.invalid/a.m4a"))
        assertSame(untouched, PlaybackService.resolveSpec(untouched))
    }
}
