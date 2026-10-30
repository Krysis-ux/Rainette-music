package com.krysisux.rainettemusic

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import androidx.media3.datasource.TransferListener
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.ByteArrayOutputStream

@UnstableApi
@RunWith(RobolectricTestRunner::class)
class ChunkedDataSourceTest {

    /** A server holding [body] that honours Range unless told not to. */
    private class FakeServer(val body: ByteArray, val honoursRange: Boolean = true) : HttpDataSource {
        val requested = mutableListOf<Pair<Long, Long>>()
        private var from = 0
        private var until = 0
        private var code = 0
        private var headers = emptyMap<String, List<String>>()

        override fun open(dataSpec: DataSpec): Long {
            val start = dataSpec.position
            val end = if (dataSpec.length == C.LENGTH_UNSET.toLong()) body.size.toLong() else minOf(body.size.toLong(), start + dataSpec.length)
            requested += start to dataSpec.length
            if (honoursRange) {
                from = start.toInt(); until = end.toInt(); code = 206
                headers = mapOf("Content-Range" to listOf("bytes $start-${end - 1}/${body.size}"))
            } else {
                from = 0; until = body.size; code = 200; headers = emptyMap()
            }
            return (until - from).toLong()
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
            if (from >= until) return C.RESULT_END_OF_INPUT
            val n = minOf(length, until - from, 7_000) // short reads, like a real socket
            System.arraycopy(body, from, buffer, offset, n)
            from += n
            return n
        }

        override fun close() {}
        override fun getUri(): Uri = Uri.parse("https://example.invalid/a.m4a")
        override fun getResponseHeaders() = headers
        override fun getResponseCode() = code
        override fun addTransferListener(transferListener: TransferListener) {}
        override fun setRequestProperty(name: String, value: String) {}
        override fun clearRequestProperty(name: String) {}
        override fun clearAllRequestProperties() {}
    }

    private fun readAll(source: ChunkedDataSource, spec: DataSpec): ByteArray {
        source.open(spec)
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(16_384)
        while (true) {
            val n = source.read(buffer, 0, buffer.size)
            if (n == C.RESULT_END_OF_INPUT) break
            out.write(buffer, 0, n)
        }
        source.close()
        return out.toByteArray()
    }

    private val body = ByteArray(250_000) { (it * 31).toByte() }
    private val spec = DataSpec(Uri.parse("https://example.invalid/a.m4a"))

    @Test
    fun `an open-ended read becomes bounded ranges and returns the whole file`() {
        val server = FakeServer(body)
        val bytes = readAll(ChunkedDataSource(server, chunkBytes = 100_000), spec)
        assertArrayEquals(body, bytes)
        // Once Content-Range has told it the size, the last request asks for exactly what is left.
        assertEquals(listOf(0L to 100_000L, 100_000L to 100_000L, 200_000L to 50_000L), server.requested)
    }

    @Test
    fun `a seek reads from the requested position to the end`() {
        val server = FakeServer(body)
        val bytes = readAll(ChunkedDataSource(server, chunkBytes = 100_000), spec.subrange(120_000))
        assertArrayEquals(body.copyOfRange(120_000, body.size), bytes)
        assertEquals(120_000L, server.requested.first().first)
    }

    @Test
    fun `a bounded request never reads past what was asked for`() {
        val server = FakeServer(body)
        val bytes = readAll(ChunkedDataSource(server, chunkBytes = 100_000), spec.subrange(10, 150_000))
        assertArrayEquals(body.copyOfRange(10, 150_010), bytes)
        assertEquals(listOf(10L to 100_000L, 100_010L to 50_000L), server.requested)
    }

    @Test
    fun `a server that ignores Range is passed through unchunked`() {
        val server = FakeServer(body, honoursRange = false)
        val bytes = readAll(ChunkedDataSource(server, chunkBytes = 100_000), spec)
        assertArrayEquals(body, bytes)
        assertEquals(1, server.requested.size)
    }

    @Test
    fun `the total length is read from Content-Range`() {
        assertEquals(5185422L, ChunkedDataSource.parseTotal("bytes 0-1048575/5185422"))
        assertEquals(C.LENGTH_UNSET.toLong(), ChunkedDataSource.parseTotal("bytes 0-1/*"))
        assertEquals(C.LENGTH_UNSET.toLong(), ChunkedDataSource.parseTotal(null))
    }
}
