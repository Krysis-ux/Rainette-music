package com.krysisux.rainettemusic

import android.app.PendingIntent
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.HttpDataSource
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.upstream.DefaultLoadErrorHandlingPolicy
import androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import java.io.File

/**
 * The one audio owner on Android.
 *
 * The player, its queue and its notification live here rather than in the
 * WebView, so a locked screen or a backgrounded app keeps playing and keeps
 * advancing: each track's stream is resolved by [ResolvingDataSource] when the
 * player opens it, on the player's own loading thread, with no JavaScript in
 * the loop.
 */
@UnstableApi
class PlaybackService : MediaSessionService() {

    private var session: MediaSession? = null
    private var songs: SimpleCache? = null
    private val main = Handler(Looper.getMainLooper())
    private var networkRetries = 0
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    /** Whether the last failure is one a returning network could fix. */
    private var waitingForNetwork = false

    override fun onCreate() {
        super.onCreate()
        // A read on a dead network hangs until this, so it bounds recovery.
        val http = DefaultHttpDataSource.Factory()
            .setConnectTimeoutMs(10_000)
            .setReadTimeoutMs(15_000)
        val streams = ResolvingDataSource.Factory(ChunkedDataSource.Factory(http), ::resolveSpec)
        // Songs already heard, kept on disk under their own id (the private
        // rainette:// address, before it is resolved): a replay starts at once,
        // asks the source for nothing, and plays with no connection.
        val cache = SimpleCache(File(cacheDir, "songs"), LeastRecentlyUsedCacheEvictor(SONG_CACHE_BYTES), StandaloneDatabaseProvider(this))
        songs = cache
        val cached = CacheDataSource.Factory()
            .setCache(cache)
            .setUpstreamDataSourceFactory(streams)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        val player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(cached).setLoadErrorHandlingPolicy(RetryPolicy()))
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(),
                /* handleAudioFocus = */ true,
            )
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        // The next song's address and first seconds are fetched while this one
        // plays, so a skip or the end of a song starts the next one at once.
        player.preloadConfiguration = ExoPlayer.PreloadConfiguration(PRELOAD_US)
        player.addListener(Recovery(player))
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        session = MediaSession.Builder(this, player)
            .setCallback(Callback)
            .setSessionActivity(open)
            .build()
        retryWhenTheNetworkReturns(player)
    }

    /** The backoff is the fallback; a network that comes back is the real cue to try again. */
    private fun retryWhenTheNetworkReturns(player: ExoPlayer) {
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onLost(network: Network) {
                debug { "network lost" }
                StreamResolver.abortInFlight()
            }

            override fun onAvailable(network: Network) {
                debug { "network available" }
                // Anything still in flight began with no usable network and will
                // only time out; fail it now so the loader retries on this one.
                StreamResolver.abortInFlight()
                main.post {
                    if (player.mediaItemCount == 0) return@post
                    if (waitingForNetwork && player.playerError != null) {
                        main.removeCallbacksAndMessages(null)
                        player.prepare()
                    } else if (player.playbackState == Player.STATE_BUFFERING) {
                        // Stalled on a load that began offline: start it again now
                        // rather than at its next backoff step.
                        player.seekTo(player.currentMediaItemIndex, player.currentPosition)
                    }
                }
            }
        }
        getSystemService(ConnectivityManager::class.java)?.registerDefaultNetworkCallback(callback)
        networkCallback = callback
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    /** Swiping the app away closes it, music included, as the person means it to. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        session?.player?.run {
            pause()
            stop()
        }
        stopSelf()
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        networkCallback?.let { getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(it) }
        session?.run {
            player.release()
            release()
        }
        session = null
        songs?.release()
        songs = null
        super.onDestroy()
    }

    /**
     * A dropped network is a pause in the music, not the end of it: re-prepare
     * with backoff, a bounded number of times, then report and stop trying. A
     * permanent failure (gone, region-locked) is reported at once.
     */
    private inner class Recovery(private val player: ExoPlayer) : Player.Listener {
        override fun onPlayerError(error: PlaybackException) {
            val code = errorCode(error)
            debug { "player error $code (${error.errorCodeName})" }
            val recoverable = code == StreamResolver.OFFLINE || code == EXPIRED
            waitingForNetwork = code == StreamResolver.OFFLINE
            if (recoverable && networkRetries < NETWORK_BACKOFF_MS.size) {
                val delay = NETWORK_BACKOFF_MS[networkRetries++]
                val item = player.currentMediaItemIndex
                PlaybackEvents.publishError(code, false)
                main.postDelayed({
                    // Only if nothing else happened meanwhile: a newer track or a stop wins.
                    if (player.playerError != null && player.currentMediaItemIndex == item) {
                        player.prepare()
                    }
                }, delay)
                return
            }
            networkRetries = 0
            PlaybackEvents.publishError(code, true)
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            if (isPlaying) networkRetries = 0
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            networkRetries = 0
        }
    }

    /** Only this app, the system's own surfaces and Bluetooth may drive playback. */
    private object Callback : MediaSession.Callback {
        override fun onConnect(session: MediaSession, controller: MediaSession.ControllerInfo): MediaSession.ConnectionResult =
            if (controller.isTrusted) super.onConnect(session, controller) else MediaSession.ConnectionResult.reject()

        /** Items arrive from a controller without a URI; the URI is derived, never accepted. */
        override fun onAddMediaItems(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo,
            mediaItems: MutableList<MediaItem>,
        ): ListenableFuture<MutableList<MediaItem>> = Futures.immediateFuture(
            mediaItems.filter { StreamResolver.isValidId(it.mediaId) }
                .map { it.buildUpon().setUri(streamUri(it.mediaId)).build() }
                .toMutableList(),
        )
    }

    /** Refuses a URL the source has revoked or let expire: resolve again, twice at most. */
    private class RetryPolicy : DefaultLoadErrorHandlingPolicy() {
        override fun getRetryDelayMsFor(info: LoadErrorHandlingPolicy.LoadErrorInfo): Long {
            debug { "load error #${info.errorCount}: ${info.exception.javaClass.simpleName} ${causeOf<StreamResolver.ResolveException>(info.exception)?.code.orEmpty()}" }
            val status = causeOf<HttpDataSource.InvalidResponseCodeException>(info.exception)?.responseCode
            if (status == 403 || status == 410) {
                if (info.errorCount > MAX_REFRESHES) return C.TIME_UNSET
                idOf(info.loadEventInfo.dataSpec.uri)?.let(StreamResolver::invalidate)
                return 300L * info.errorCount
            }
            val resolve = causeOf<StreamResolver.ResolveException>(info.exception)
            if (resolve != null && resolve.code != StreamResolver.OFFLINE) return C.TIME_UNSET
            return super.getRetryDelayMsFor(info)
        }
    }

    companion object {
        const val SCHEME = "rainette"
        private const val SONG_CACHE_BYTES = 400L shl 20
        private const val PRELOAD_US = 10_000_000L
        const val TAG = "RainettePlayback"
        const val EXPIRED = "expired"
        private const val MAX_REFRESHES = 2
        private val NETWORK_BACKOFF_MS = longArrayOf(2_000, 4_000, 8_000, 16_000, 30_000)

        fun streamUri(id: String): Uri = Uri.parse("$SCHEME://youtube/$id")

        /** Silent unless enabled: `adb shell setprop log.tag.RainettePlayback DEBUG`. Ids and codes only, never URLs. */
        inline fun debug(message: () -> String) {
            if (Log.isLoggable(TAG, Log.DEBUG)) Log.d(TAG, message())
        }

        fun idOf(uri: Uri?): String? =
            uri?.takeIf { it.scheme == SCHEME }?.lastPathSegment?.takeIf(StreamResolver::isValidId)

        /** Turns `rainette://youtube/ID` into the real, signed URL, at the moment the player opens it. */
        fun resolveSpec(spec: DataSpec): DataSpec {
            val id = idOf(spec.uri) ?: return spec
            debug { "resolve $id at ${spec.position}" }
            val stream = StreamResolver.resolve(id)
            return spec.buildUpon()
                .setUri(stream.url)
                .setHttpRequestHeaders(spec.httpRequestHeaders + ("User-Agent" to stream.userAgent))
                .build()
        }

        inline fun <reified T : Throwable> causeOf(error: Throwable?): T? {
            var current = error
            while (current != null) {
                if (current is T) return current
                current = current.cause
            }
            return null
        }

        /** The web layer's vocabulary for what went wrong (see pwa/src/source-provider.js). */
        fun errorCode(error: PlaybackException): String {
            causeOf<StreamResolver.ResolveException>(error)?.let { return it.code }
            val status = causeOf<HttpDataSource.InvalidResponseCodeException>(error)?.responseCode
            if (status == 403 || status == 410) return EXPIRED
            if (status == 429) return StreamResolver.RATE_LIMITED
            return when (error.errorCode) {
                PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED,
                PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_TIMEOUT -> StreamResolver.OFFLINE
                PlaybackException.ERROR_CODE_DECODING_FORMAT_UNSUPPORTED,
                PlaybackException.ERROR_CODE_PARSING_CONTAINER_UNSUPPORTED,
                PlaybackException.ERROR_CODE_DECODER_INIT_FAILED -> StreamResolver.UNSUPPORTED
                else -> "service_failed"
            }
        }
    }
}
