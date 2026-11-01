package com.krysisux.rainettemusic

import android.content.ComponentName
import android.graphics.Color
import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import com.google.common.util.concurrent.ListenableFuture
import org.json.JSONObject
import java.util.concurrent.Executors

/** Final errors from the service's own player, with the cause chain intact. */
object PlaybackEvents {
    @Volatile
    var onError: ((code: String, final: Boolean) -> Unit)? = null

    fun publishError(code: String, final: Boolean) {
        onError?.invoke(code, final)
    }
}

/**
 * The page's only door into native playback. Every method takes plain data,
 * validates it, and drives [PlaybackService] through a MediaController, the
 * same way the notification and a Bluetooth headset do. Nothing here accepts a
 * URL to play: the page names a track, the service derives where it comes from.
 *
 * Events go through [watch], not Capacitor's addListener: `state` (on every
 * change, and each second while playing) and `error` (a failure the service
 * has given up on). Both carry the `generation` of the play request they
 * belong to, so the page can drop anything stale.
 */
@UnstableApi
@CapacitorPlugin(name = "RainettePlayer")
class RainettePlayerPlugin : Plugin() {

    private lateinit var controllerFuture: ListenableFuture<MediaController>
    private val io = Executors.newFixedThreadPool(2)
    private val main = Handler(Looper.getMainLooper())
    private var generation = 0
    private var pending: Pending? = null
    /** The page's event channel, and which page instance owns it. Main thread only. */
    private val watchers = mutableListOf<PluginCall>()
    private var watchingPage = ""

    private class Pending(val call: PluginCall, val sourceId: String, val generation: Int)

    override fun load() {
        val token = SessionToken(context, ComponentName(context, PlaybackService::class.java))
        controllerFuture = MediaController.Builder(context, token).buildAsync()
        withController { it.addListener(Events()) }
        PlaybackEvents.onError = { code, final -> main.post { onServiceError(code, final) } }
    }

    override fun handleOnDestroy() {
        PlaybackEvents.onError = null
        main.removeCallbacksAndMessages(null)
        MediaController.releaseFuture(controllerFuture)
        io.shutdownNow()
    }

    /* A page that was asleep missed events; give it the truth on return. */
    override fun handleOnResume() = withController { emitState(it) }

    /**
     * Subscribe the page to `state` and `error` events. Capacitor's own
     * listeners are wiped by `bridge.reset()` when the WebView reports the page
     * started, and on a Galaxy Note 8 that report arrived after the page had
     * registered them, so the page silently stopped hearing the service. A call
     * this plugin holds itself is untouched by the reset. One page at a time:
     * a new page id (a reload) replaces the old page's callbacks.
     */
    @PluginMethod(returnType = PluginMethod.RETURN_CALLBACK)
    fun watch(call: PluginCall) {
        val page = call.getString("page").orEmpty()
        call.setKeepAlive(true)
        main.post {
            if (page != watchingPage) {
                watchers.forEach { bridge.releaseCall(it) }
                watchers.clear()
                watchingPage = page
            }
            watchers += call
            withController { emitState(it) }
        }
    }

    private fun send(event: String, data: JSObject) {
        data.put("event", event)
        watchers.forEach { it.resolve(data) }
    }

    @PluginMethod
    fun capabilities(call: PluginCall) {
        call.resolve(JSObject().put("search", true).put("playback", true).put("protocol", PROTOCOL))
    }

    @PluginMethod
    fun search(call: PluginCall) {
        val query = call.getString("query").orEmpty().trim()
        if (query.isEmpty() || query.length > 200) return call.reject("Type something to search for.", StreamResolver.INVALID)
        io.execute {
            try {
                val songs = StreamResolver.search(query).map { JSObject(JSONObject(it).toString()) }
                call.resolve(JSObject().put("songs", JSArray(songs)))
            } catch (e: Exception) {
                reject(call, StreamResolver.classify(e))
            }
        }
    }

    /** A URL for the web `<audio>` element, for queues it owns (phone files mixed with songs). */
    @PluginMethod
    fun resolve(call: PluginCall) {
        val id = call.getString("source_id").orEmpty()
        if (!StreamResolver.isValidId(id)) return call.reject("Not a track this app can play.", StreamResolver.INVALID)
        val force = call.getBoolean("force_refresh", false) == true
        io.execute {
            try {
                val stream = StreamResolver.resolve(id, force)
                call.resolve(JSObject().put("url", stream.url).put("mime_type", stream.mimeType)
                    .put("expires_at_ms", stream.expiresAtMs))
            } catch (e: Exception) {
                reject(call, StreamResolver.classify(e))
            }
        }
    }

    /** Replace the queue and start it. Resolves only once the first track has loaded. */
    @PluginMethod
    fun play(call: PluginCall) {
        val items = parseQueue(call) ?: return
        val index = call.getInt("index", 0) ?: 0
        if (index !in items.indices) return call.reject("That track is not in the queue.", StreamResolver.INVALID)
        val positionMs = ((call.getDouble("position_s") ?: 0.0).coerceAtLeast(0.0) * 1000).toLong()
        val startPaused = call.getBoolean("start_paused", false) == true
        val requested = call.getInt("generation", 0) ?: 0
        withController { c ->
            pending?.call?.reject("A newer track replaced this one.", CANCELED)
            generation = requested
            pending = Pending(call, items[index].mediaId, requested)
            c.setMediaItems(items, index, positionMs)
            c.prepare()
            c.playWhenReady = !startPaused
            main.postDelayed({
                val stuck = pending
                if (stuck != null && stuck.generation == requested) {
                    pending = null
                    stuck.call.reject("The song took too long to start.", StreamResolver.OFFLINE)
                }
            }, PLAY_TIMEOUT_MS)
        }
    }

    /**
     * Swap everything around the current track without interrupting it. Refused
     * when the current track is not where the page thinks it is, so the page
     * falls back to a fresh [play] instead of the two queues drifting apart.
     */
    @PluginMethod
    fun setQueue(call: PluginCall) {
        val items = parseQueue(call) ?: return
        val index = call.getInt("index", -1) ?: -1
        withController { c ->
            val current = c.currentMediaItem?.mediaId
            if (index !in items.indices || current != items[index].mediaId) {
                return@withController call.reject("The queue moved on.", MISMATCH)
            }
            generation = call.getInt("generation", generation) ?: generation
            val at = c.currentMediaItemIndex
            if (at + 1 < c.mediaItemCount) c.removeMediaItems(at + 1, c.mediaItemCount)
            if (at > 0) c.removeMediaItems(0, at)
            c.addMediaItems(0, items.subList(0, index))
            c.addMediaItems(items.subList(index + 1, items.size))
            call.resolve(state(c))
        }
    }

    @PluginMethod
    fun resume(call: PluginCall) = withController { c ->
        // A player that stopped on an error needs preparing again before it can play.
        if (c.playbackState == Player.STATE_IDLE && c.mediaItemCount > 0) c.prepare()
        c.play()
        call.resolve(state(c))
    }

    @PluginMethod
    fun pause(call: PluginCall) = withController { c -> c.pause(); call.resolve(state(c)) }

    @PluginMethod
    fun stop(call: PluginCall) = withController { c ->
        pending?.call?.reject("Playback moved elsewhere.", CANCELED)
        pending = null
        c.stop()
        c.clearMediaItems()
        call.resolve(state(c))
    }

    @PluginMethod
    fun seek(call: PluginCall) {
        val seconds = call.getDouble("position_s") ?: return call.reject("No position given.", StreamResolver.INVALID)
        withController { c -> c.seekTo((seconds.coerceAtLeast(0.0) * 1000).toLong()); call.resolve(state(c)) }
    }

    /** Next, or previous; previous restarts a track more than three seconds in. */
    @PluginMethod
    fun skip(call: PluginCall) {
        val offset = call.getInt("offset", 1) ?: 1
        withController { c ->
            if (offset > 0) c.seekToNext() else c.seekToPrevious()
            call.resolve(state(c))
        }
    }

    @PluginMethod
    fun setRepeat(call: PluginCall) {
        val mode = when (call.getString("mode")) {
            "all" -> Player.REPEAT_MODE_ALL
            "one" -> Player.REPEAT_MODE_ONE
            "off" -> Player.REPEAT_MODE_OFF
            else -> return call.reject("Unknown repeat mode.", StreamResolver.INVALID)
        }
        withController { c -> c.repeatMode = mode; call.resolve(state(c)) }
    }

    @PluginMethod
    fun setVolume(call: PluginCall) {
        val volume = (call.getDouble("volume") ?: 1.0).coerceIn(0.0, 1.0).toFloat()
        withController { c -> c.volume = volume; call.resolve(state(c)) }
    }

    /** The current state; with `with_queue`, also the tracks it holds, for a page that just opened. */
    @PluginMethod
    fun getState(call: PluginCall) = withController { c ->
        val snapshot = state(c)
        if (call.getBoolean("with_queue", false) == true) {
            snapshot.put("queue", JSArray((0 until c.mediaItemCount).map { i ->
                val item = c.getMediaItemAt(i)
                JSObject()
                    .put("source", "youtube")
                    .put("source_id", item.mediaId)
                    .put("title", item.mediaMetadata.title?.toString().orEmpty())
                    .put("artist", item.mediaMetadata.artist?.toString().orEmpty())
                    .put("thumbnail_url", item.mediaMetadata.artworkUri?.toString().orEmpty())
            }))
        }
        call.resolve(snapshot)
    }

    /**
     * Paint the window behind the system bars in the page's theme colour. Where
     * the WebView is inset rather than edge to edge, that window is what shows
     * under the clock, and a dark one under a light theme hid the icons.
     */
    @PluginMethod
    fun setSystemBarColor(call: PluginCall) {
        val raw = call.getString("color").orEmpty()
        if (!Regex("^#[0-9A-Fa-f]{6}$").matches(raw)) return call.reject("Not a colour.", StreamResolver.INVALID)
        val color = Color.parseColor(raw)
        activity.runOnUiThread {
            activity.window.decorView.setBackgroundColor(color)
            @Suppress("DEPRECATION")
            activity.window.statusBarColor = color
            @Suppress("DEPRECATION")
            activity.window.navigationBarColor = color
            call.resolve()
        }
    }

    /* ── Internals ──────────────────────────────────────────────────────── */

    private inner class Events : Player.Listener {
        /* A skip, from the page or the notification, before the requested song
         * started: that play is over. Left waiting, it would settle only with
         * the next song's failure or the timeout, and the page would show it
         * loading until then. The play's own new queue is not a skip. */
        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            val waiting = pending ?: return
            if (reason == Player.MEDIA_ITEM_TRANSITION_REASON_PLAYLIST_CHANGED || mediaItem?.mediaId == waiting.sourceId) return
            pending = null
            waiting.call.reject("Skipped before it started.", CANCELED)
        }

        override fun onEvents(player: Player, events: Player.Events) {
            val c = player as MediaController
            val waiting = pending
            if (waiting != null && c.playbackState == Player.STATE_READY && c.currentMediaItem?.mediaId == waiting.sourceId) {
                pending = null
                waiting.call.resolve(state(c))
            }
            emitState(c)
            scheduleTick(c)
        }
    }

    private val tick = Runnable { withController { emitState(it); scheduleTick(it) } }

    private fun scheduleTick(c: MediaController) {
        main.removeCallbacks(tick)
        if (c.isPlaying) main.postDelayed(tick, TICK_MS)
    }

    private fun onServiceError(code: String, final: Boolean) {
        if (!final) return
        val waiting = pending
        if (waiting != null) {
            pending = null
            waiting.call.reject(message(code), code)
            return
        }
        withController { c ->
            send("error", JSObject().put("generation", generation).put("code", code)
                .put("message", message(code)).put("index", c.currentMediaItemIndex)
                .put("source_id", c.currentMediaItem?.mediaId.orEmpty()))
        }
    }

    private fun emitState(c: MediaController) = send("state", state(c))

    private fun state(c: MediaController): JSObject {
        val duration = c.duration.takeIf { it > 0 } ?: 0L
        return JSObject()
            .put("generation", generation)
            .put("playing", c.playWhenReady && (c.playbackState == Player.STATE_READY || c.playbackState == Player.STATE_BUFFERING))
            .put("loading", c.playbackState == Player.STATE_BUFFERING)
            .put("ended", c.playbackState == Player.STATE_ENDED)
            .put("index", c.currentMediaItemIndex)
            .put("source_id", c.currentMediaItem?.mediaId.orEmpty())
            .put("position_s", c.currentPosition.coerceAtLeast(0L) / 1000.0)
            .put("duration_s", duration / 1000.0)
            .put("count", c.mediaItemCount)
    }

    /** Untrusted input from the page becomes MediaItems here, or the call is refused. */
    private fun parseQueue(call: PluginCall): List<MediaItem>? {
        val raw = call.getArray("queue")
        if (raw == null || raw.length() == 0 || raw.length() > MAX_QUEUE) {
            call.reject("That queue cannot be played.", StreamResolver.INVALID)
            return null
        }
        val items = ArrayList<MediaItem>(raw.length())
        for (i in 0 until raw.length()) {
            val track = raw.optJSONObject(i)
            val id = track?.optString("source_id").orEmpty()
            if (!StreamResolver.isValidId(id)) {
                call.reject("Only streamed songs can play here.", StreamResolver.INVALID)
                return null
            }
            val art = track?.optString("thumbnail_url").orEmpty().takeIf { it.startsWith("https://") }
            val metadata = MediaMetadata.Builder()
                .setTitle(track?.optString("title").orEmpty().take(300).ifBlank { null })
                .setArtist(track?.optString("artist").orEmpty().take(300).ifBlank { null })
                .setArtworkUri(art?.let(Uri::parse))
                .build()
            items += MediaItem.Builder().setMediaId(id).setMediaMetadata(metadata).build()
        }
        return items
    }

    private fun withController(block: (MediaController) -> Unit) {
        controllerFuture.addListener({
            val c = try {
                controllerFuture.get()
            } catch (e: Exception) {
                null
            }
            if (c == null) send("error", JSObject().put("code", SERVICE_FAILED).put("message", message(SERVICE_FAILED)))
            else block(c)
        }, ContextCompat.getMainExecutor(context))
    }

    private fun reject(call: PluginCall, e: StreamResolver.ResolveException) =
        call.reject(e.message?.substringAfter(": ") ?: message(e.code), e.code)

    private fun message(code: String) = when (code) {
        StreamResolver.OFFLINE -> "No internet connection."
        PlaybackService.EXPIRED -> "The stream expired and could not be renewed."
        StreamResolver.UNAVAILABLE -> "This song is no longer available."
        StreamResolver.REGION -> "This song isn't available in your country."
        StreamResolver.RATE_LIMITED -> "The source is asking this phone to slow down. Try again shortly."
        StreamResolver.UNSUPPORTED -> "This phone can't play this song's format."
        else -> "Playback stopped unexpectedly."
    }

    companion object {
        /** Bumped when the page and the plugin must change together. */
        const val PROTOCOL = 1
        const val CANCELED = "canceled"
        const val MISMATCH = "mismatch"
        const val SERVICE_FAILED = "service_failed"
        private const val MAX_QUEUE = 1000
        private const val TICK_MS = 1000L
        private const val PLAY_TIMEOUT_MS = 60_000L
    }
}
