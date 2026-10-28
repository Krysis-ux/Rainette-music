package com.krysisux.rainettemusic

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import androidx.media3.datasource.TransferListener

/**
 * Reads a stream as a run of bounded `Range` requests.
 *
 * YouTube throttles an open-ended request for a whole file to roughly real-time
 * speed (measured: 36 KB/s for a 128 kbps track) while answering 1 MB ranges at
 * several MB/s. Media3 opens progressive media open-ended, so without this the
 * next track buffers barely faster than it plays and a seek ahead stalls.
 *
 * To the player this is still one continuous source: chunk boundaries are
 * crossed inside [read].
 */
@UnstableApi
class ChunkedDataSource(
    private val upstream: HttpDataSource,
    private val chunkBytes: Long = DEFAULT_CHUNK_BYTES,
) : DataSource {

    private var spec: DataSpec? = null
    private var position = 0L
    /** Bytes still owed to the caller, or [C.LENGTH_UNSET] when it asked for "to the end". */
    private var remaining = C.LENGTH_UNSET.toLong()
    private var chunkLeft = 0L
    private var total = C.LENGTH_UNSET.toLong()
    private var unchunked = false

    override fun addTransferListener(transferListener: TransferListener) =
        upstream.addTransferListener(transferListener)

    override fun open(dataSpec: DataSpec): Long {
        spec = dataSpec
        position = dataSpec.position
        remaining = dataSpec.length
        total = C.LENGTH_UNSET.toLong()
        unchunked = false
        openChunk()
        if (unchunked) return dataSpec.length
        return if (remaining != C.LENGTH_UNSET.toLong()) remaining
        else if (total != C.LENGTH_UNSET.toLong()) total - position
        else C.LENGTH_UNSET.toLong()
    }

    private fun openChunk() {
        val base = requireNotNull(spec)
        var want = if (remaining == C.LENGTH_UNSET.toLong()) chunkBytes else minOf(chunkBytes, remaining)
        // Never ask past the end once the end is known.
        if (total != C.LENGTH_UNSET.toLong()) want = minOf(want, total - position)
        upstream.open(base.subrange(position - base.position, want))
        // A server that ignores Range sends the whole body with a 200. That is
        // still correct data, just unchunked, so it is passed through as-is.
        if (upstream.responseCode == 200) {
            unchunked = true
            return
        }
        chunkLeft = want
        total = parseTotal(upstream.responseHeaders["Content-Range"]?.firstOrNull()
            ?: upstream.responseHeaders["content-range"]?.firstOrNull())
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        if (unchunked) return upstream.read(buffer, offset, length)
        while (true) {
            if (remaining == 0L || (total != C.LENGTH_UNSET.toLong() && position >= total)) return C.RESULT_END_OF_INPUT
            if (chunkLeft == 0L) {
                upstream.close()
                openChunk()
                if (unchunked) return upstream.read(buffer, offset, length)
            }
            val read = upstream.read(buffer, offset, minOf(length.toLong(), chunkLeft).toInt())
            if (read == C.RESULT_END_OF_INPUT) {
                // The chunk came up short: that is the true end of the file.
                return C.RESULT_END_OF_INPUT
            }
            position += read
            chunkLeft -= read
            if (remaining != C.LENGTH_UNSET.toLong()) remaining -= read
            return read
        }
    }

    override fun getUri(): Uri? = upstream.uri

    override fun getResponseHeaders(): Map<String, List<String>> = upstream.responseHeaders

    override fun close() {
        spec = null
        upstream.close()
    }

    class Factory(private val upstream: HttpDataSource.Factory) : DataSource.Factory {
        override fun createDataSource(): DataSource = ChunkedDataSource(upstream.createDataSource())
    }

    companion object {
        const val DEFAULT_CHUNK_BYTES = 1L shl 20

        /** `bytes 0-1048575/5185422` → 5185422; anything else → unknown. */
        fun parseTotal(header: String?): Long =
            header?.substringAfterLast('/', "")?.trim()?.toLongOrNull() ?: C.LENGTH_UNSET.toLong()
    }
}
