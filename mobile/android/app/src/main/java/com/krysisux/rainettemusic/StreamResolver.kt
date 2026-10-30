package com.krysisux.rainettemusic

import org.schabi.newpipe.extractor.Image
import org.schabi.newpipe.extractor.NewPipe
import org.schabi.newpipe.extractor.ServiceList
import org.schabi.newpipe.extractor.downloader.Downloader
import org.schabi.newpipe.extractor.downloader.Request
import org.schabi.newpipe.extractor.downloader.Response
import org.schabi.newpipe.extractor.exceptions.AgeRestrictedContentException
import org.schabi.newpipe.extractor.exceptions.ContentNotAvailableException
import org.schabi.newpipe.extractor.exceptions.GeographicRestrictionException
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException
import org.schabi.newpipe.extractor.exceptions.SignInConfirmNotBotException
import org.schabi.newpipe.extractor.exceptions.UnsupportedContentInCountryException
import org.schabi.newpipe.extractor.localization.Localization
import org.schabi.newpipe.extractor.services.youtube.YoutubeParsingHelper
import org.schabi.newpipe.extractor.services.youtube.linkHandler.YoutubeSearchQueryHandlerFactory
import org.schabi.newpipe.extractor.stream.StreamInfo
import org.schabi.newpipe.extractor.stream.StreamInfoItem
import java.io.IOException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.net.UnknownHostException
import java.util.concurrent.ConcurrentHashMap

/**
 * Search and stream resolution, entirely on this phone.
 *
 * NewPipeExtractor talks to YouTube directly through [UrlDownloader]; nothing
 * goes through a Rainette server, a computer, or anybody else's proxy. Every
 * failure leaves here as a [ResolveException] with one of the codes the web
 * layer knows how to word (see `pwa/src/source-provider.js`).
 */
object StreamResolver {

    data class Resolved(val url: String, val userAgent: String, val mimeType: String, val expiresAtMs: Long)

    /** What [pickAudio] needs to know about a stream, without NewPipe's types. */
    data class Candidate(val url: String, val itag: Int, val bitrate: Int, val suffix: String, val isUrl: Boolean)

    class ResolveException(val code: String, message: String, cause: Throwable? = null) :
        IOException("$code: $message", cause)

    const val OFFLINE = "offline"
    const val UNAVAILABLE = "unavailable"
    const val REGION = "region"
    const val RESTRICTED = "restricted"
    const val RATE_LIMITED = "rate_limited"
    const val UNSUPPORTED = "unsupported"
    const val SOURCE_CHANGED = "source_changed"
    const val SEARCH_FAILED = "search_failed"
    const val INVALID = "invalid"

    /** Refresh a cached URL this long before it expires, not at the edge. */
    private const val EXPIRY_MARGIN_MS = 10 * 60 * 1000L
    /** Used when a URL carries no `expire` parameter. Upstream URLs live ~6 h. */
    private const val DEFAULT_LIFETIME_MS = 60 * 60 * 1000L
    private const val SEARCH_LIMIT = 30

    private val ID = Regex("^[A-Za-z0-9_-]{11}$")
    private val cache = ConcurrentHashMap<String, Resolved>()

    @Volatile
    private var initialised = false

    private fun init() {
        if (initialised) return
        synchronized(this) {
            if (!initialised) {
                NewPipe.init(UrlDownloader)
                initialised = true
            }
        }
    }

    fun isValidId(id: String) = ID.matches(id)

    /** A playable audio URL for [videoId], from cache unless [force] or near expiry. */
    fun resolve(videoId: String, force: Boolean = false, now: Long = System.currentTimeMillis()): Resolved {
        if (!isValidId(videoId)) throw ResolveException(INVALID, "Not a track this app can play.")
        if (!force) cache[videoId]?.takeIf { it.expiresAtMs - now > EXPIRY_MARGIN_MS }?.let { return it }
        init()
        val info = try {
            StreamInfo.getInfo(ServiceList.YouTube, "https://www.youtube.com/watch?v=$videoId")
        } catch (e: Exception) {
            throw classify(e)
        }
        val candidates = info.audioStreams.map {
            Candidate(it.content, it.itag, it.averageBitrate, it.format?.suffix.orEmpty(), it.isUrl)
        }
        val best = pickAudio(candidates)
            ?: throw ResolveException(UNSUPPORTED, "This song has no audio this phone can play.")
        val resolved = Resolved(
            url = best.url,
            userAgent = userAgentFor(best.url),
            mimeType = if (best.suffix == "m4a") "audio/mp4" else "audio/webm",
            expiresAtMs = expiresAt(best.url, now),
        )
        cache[videoId] = resolved
        return resolved
    }

    /**
     * Fail every request still in flight. Called when the network is lost: a
     * socket opened on the old network does not error, it hangs until its read
     * timeout -- measured at 30 s on an emulator -- and the music with it.
     */
    fun abortInFlight() = UrlDownloader.abortAll()

    /** Forget a URL the source has refused, so the next open resolves afresh. */
    fun invalidate(videoId: String) {
        cache.remove(videoId)
    }

    /** Song search, shaped like the computer's catalog results so playlists match. */
    fun search(query: String): List<Map<String, Any?>> {
        val q = query.trim().take(200)
        if (q.isEmpty()) return emptyList()
        init()
        val yt = ServiceList.YouTube
        val items = try {
            val handler = yt.searchQHFactory.fromQuery(q, listOf(YoutubeSearchQueryHandlerFactory.MUSIC_SONGS), "")
            yt.getSearchExtractor(handler).also { it.fetchPage() }.initialPage.items
        } catch (e: Exception) {
            val classified = classify(e)
            throw if (classified.code == SOURCE_CHANGED) ResolveException(SEARCH_FAILED, "Search did not answer.", e)
            else classified
        }
        return items.filterIsInstance<StreamInfoItem>().take(SEARCH_LIMIT).mapNotNull { normalize(it) }
    }

    private fun normalize(item: StreamInfoItem): Map<String, Any?>? {
        val id = videoIdFrom(item.url) ?: return null
        return mapOf(
            "source" to "youtube",
            "source_id" to id,
            "title" to item.name.ifBlank { "(untitled)" },
            "artist" to item.uploaderName.orEmpty().removeSuffix(" - Topic"),
            "duration_s" to item.duration.takeIf { it > 0 },
            "thumbnail_url" to largest(item.thumbnails),
            "metadata" to mapOf("source_detail" to "on-device"),
        )
    }

    /** `https://music.youtube.com/watch?v=ID&…` → ID. A regex, not Uri, so it runs in JVM tests. */
    fun videoIdFrom(url: String?): String? =
        url?.let { Regex("[?&]v=([A-Za-z0-9_-]{11})(?:&|$)").find(it)?.groupValues?.get(1) }

    private fun largest(images: List<Image>): String =
        images.filter { it.url.startsWith("https://") }.maxByOrNull { it.height }?.url.orEmpty()

    /**
     * AAC first: both Media3 and AVFoundation decode it everywhere, and it is the
     * format the desktop serves phones too. Opus is the fallback, highest bitrate wins.
     */
    fun pickAudio(candidates: List<Candidate>): Candidate? {
        val usable = candidates.filter { it.isUrl && it.url.startsWith("https://") }
        return usable.filter { it.suffix == "m4a" }.maxByOrNull { it.bitrate }
            ?: usable.maxByOrNull { it.bitrate }
    }

    /** The `expire` query parameter, in ms, or a conservative default. */
    fun expiresAt(url: String, now: Long): Long {
        val seconds = Regex("[?&]expire=(\\d+)").find(url)?.groupValues?.get(1)?.toLongOrNull()
        return seconds?.times(1000)?.takeIf { it > now } ?: (now + DEFAULT_LIFETIME_MS)
    }

    /** Each URL is signed for one client; asking as another one is refused. */
    private fun userAgentFor(url: String): String = when {
        YoutubeParsingHelper.isAndroidStreamingUrl(url) -> YoutubeParsingHelper.getAndroidUserAgent(Localization.DEFAULT)
        YoutubeParsingHelper.isIosStreamingUrl(url) -> YoutubeParsingHelper.getIosUserAgent(Localization.DEFAULT)
        YoutubeParsingHelper.isVisionOsStreamingUrl(url) -> YoutubeParsingHelper.getVisionOsUserAgent(Localization.DEFAULT)
        else -> BROWSER_UA
    }

    /** Exception type alone is not enough: an unknown id surfaces as a ParsingException. */
    fun classify(e: Throwable): ResolveException = when (e) {
        is ResolveException -> e
        is GeographicRestrictionException, is UnsupportedContentInCountryException ->
            ResolveException(REGION, "This song isn't available in your country.", e)
        is AgeRestrictedContentException ->
            ResolveException(RESTRICTED, "This song needs a signed-in account to play.", e)
        is ReCaptchaException, is SignInConfirmNotBotException ->
            ResolveException(RATE_LIMITED, "The source is asking this phone to slow down. Try again shortly.", e)
        is ContentNotAvailableException ->
            ResolveException(UNAVAILABLE, "This song is no longer available.", e)
        is UnknownHostException, is SocketTimeoutException, is java.net.ConnectException ->
            ResolveException(OFFLINE, "No internet connection.", e)
        is IOException -> ResolveException(OFFLINE, "The connection dropped.", e)
        else -> ResolveException(SOURCE_CHANGED, "This song couldn't be read from the source.", e)
    }

    private const val BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"

    /** NewPipe's only network seam. Plain HttpURLConnection: no extra dependency. */
    private object UrlDownloader : Downloader() {
        private val inFlight = ConcurrentHashMap.newKeySet<HttpURLConnection>()

        fun abortAll() = inFlight.forEach { it.disconnect() }

        override fun execute(request: Request): Response {
            val connection = URL(request.url()).openConnection() as HttpURLConnection
            inFlight += connection
            try {
                connection.connectTimeout = 10_000
                connection.readTimeout = 15_000
                connection.requestMethod = request.httpMethod()
                connection.setRequestProperty("User-Agent", BROWSER_UA)
                for ((name, values) in request.headers()) {
                    connection.setRequestProperty(name, values.joinToString(", "))
                }
                request.dataToSend()?.let { body ->
                    connection.doOutput = true
                    connection.outputStream.use { it.write(body) }
                }
                val code = connection.responseCode
                if (code == 429) throw ReCaptchaException("rate limited", request.url())
                val stream = if (code >= 400) connection.errorStream else connection.inputStream
                val body = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                val headers = connection.headerFields.filterKeys { it != null }
                return Response(code, connection.responseMessage, headers, body, connection.url.toString())
            } finally {
                inFlight -= connection
                connection.disconnect()
            }
        }
    }
}
