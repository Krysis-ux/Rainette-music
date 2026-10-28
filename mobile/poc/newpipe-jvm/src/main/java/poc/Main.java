package poc;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.schabi.newpipe.extractor.InfoItem;
import org.schabi.newpipe.extractor.NewPipe;
import org.schabi.newpipe.extractor.ServiceList;
import org.schabi.newpipe.extractor.StreamingService;
import org.schabi.newpipe.extractor.downloader.Downloader;
import org.schabi.newpipe.extractor.downloader.Request;
import org.schabi.newpipe.extractor.downloader.Response;
import org.schabi.newpipe.extractor.search.SearchExtractor;
import org.schabi.newpipe.extractor.services.youtube.linkHandler.YoutubeSearchQueryHandlerFactory;
import org.schabi.newpipe.extractor.stream.AudioStream;
import org.schabi.newpipe.extractor.stream.StreamInfo;
import org.schabi.newpipe.extractor.stream.StreamInfoItem;

/**
 * Feasibility probe for Rainette's on-device Android resolver: search YouTube
 * Music, resolve an audio-only stream, and prove the bytes are real audio --
 * with nothing between this process and the upstream. Exit code is the number
 * of failed checks.
 */
public final class Main {
    private static int failures = 0;

    public static void main(String[] args) throws Exception {
        Path out = Path.of(args.length > 0 ? args[0] : "out");
        Files.createDirectories(out);
        NewPipe.init(new JdkDownloader());
        StreamingService yt = ServiceList.YouTube;

        // 1. Search (YouTube Music "songs" filter, which is what Rainette's catalog search means).
        long t0 = System.nanoTime();
        SearchExtractor search = yt.getSearchExtractor(yt.getSearchQHFactory().fromQuery(
                "daft punk one more time", List.of(YoutubeSearchQueryHandlerFactory.MUSIC_SONGS), ""));
        search.fetchPage();
        List<StreamInfoItem> songs = new ArrayList<>();
        for (InfoItem item : search.getInitialPage().getItems()) {
            if (item instanceof StreamInfoItem s) songs.add(s);
        }
        log("search", "%d songs in %d ms", songs.size(), ms(t0));
        check(!songs.isEmpty(), "search returned songs");
        for (int i = 0; i < Math.min(5, songs.size()); i++) {
            StreamInfoItem s = songs.get(i);
            log("  result", "%s | %s | %ds | %s", s.getName(), s.getUploaderName(), s.getDuration(), s.getUrl());
        }

        // 2. Resolve three consecutive tracks, the way a queue advances.
        for (int i = 0; i < Math.min(3, songs.size()); i++) {
            resolveAndFetch(yt, songs.get(i).getUrl(), out.resolve("track" + i + ".m4a"), false);
        }

        // 3. Failure shapes the app has to tell apart.
        expectFailure(yt, "https://www.youtube.com/watch?v=aaaaaaaaaaa", "unavailable id");
        tamperedUrl(yt, songs.get(0).getUrl());

        log("done", "%d failed check(s)", failures);
        System.exit(failures);
    }

    private static void resolveAndFetch(StreamingService yt, String url, Path file, boolean fullDownload)
            throws Exception {
        long t0 = System.nanoTime();
        StreamInfo info = StreamInfo.getInfo(yt, url);
        log("resolve", "%s (%s) in %d ms, duration %ds", info.getName(), info.getUploaderName(), ms(t0),
                info.getDuration());
        List<AudioStream> audio = info.getAudioStreams();
        for (AudioStream a : audio) {
            log("  stream", "itag=%d %s %s %dkbps delivery=%s", a.getItag(), a.getFormat(), a.getCodec(),
                    a.getAverageBitrate(), a.getDeliveryMethod());
        }
        // m4a/AAC is the one format both Media3 and AVFoundation decode natively.
        AudioStream m4a = audio.stream()
                .filter(a -> a.getFormat() != null && "m4a".equals(a.getFormat().getSuffix()) && a.isUrl())
                .max(java.util.Comparator.comparingInt(AudioStream::getAverageBitrate)).orElse(null);
        check(m4a != null, "m4a progressive stream present for " + info.getId());
        if (m4a == null) return;
        String content = m4a.getContent();
        log("  expiry", "expire=%s client=%s", query(content, "expire"), query(content, "c"));

        HttpRequest.Builder req = HttpRequest.newBuilder(URI.create(content)).timeout(Duration.ofSeconds(60));
        if (!fullDownload) req.header("Range", "bytes=0-262143");
        long f0 = System.nanoTime();
        HttpResponse<byte[]> resp = HTTP.send(req.GET().build(), HttpResponse.BodyHandlers.ofByteArray());
        long took = Math.max(1, ms(f0));
        String type = resp.headers().firstValue("content-type").orElse("");
        byte[] body = resp.body();
        log("  fetch", "HTTP %d %s %d bytes in %d ms (%d KB/s, itag %d)", resp.statusCode(), type, body.length,
                took, body.length / took, m4a.getItag());
        check(resp.statusCode() == 200 || resp.statusCode() == 206, "audio fetch status for " + info.getId());
        check(type.startsWith("audio/"), "audio content-type for " + info.getId());
        check(body.length > 8 && new String(body, 4, 4).equals("ftyp"), "MP4 'ftyp' box at start for " + info.getId());
        Files.write(file, body);
        if (!fullDownload) chunkedDownload(content, m4a.getItag(), file.resolveSibling(file.getFileName() + ".chunked.m4a"));
    }

    /** Bounded ranges, the way a player that is not throttled has to read. */
    private static void chunkedDownload(String content, int itag, Path file) throws Exception {
        final int chunk = 1 << 20;
        java.io.ByteArrayOutputStream all = new java.io.ByteArrayOutputStream();
        long f0 = System.nanoTime();
        long total = -1;
        int requests = 0;
        while (total < 0 || all.size() < total) {
            int from = all.size();
            HttpResponse<byte[]> r = HTTP.send(HttpRequest.newBuilder(URI.create(content))
                    .header("Range", "bytes=" + from + "-" + (from + chunk - 1)).GET().build(),
                    HttpResponse.BodyHandlers.ofByteArray());
            requests++;
            check(r.statusCode() == 206, "chunk " + requests + " is 206");
            if (r.statusCode() != 206) return;
            String range = r.headers().firstValue("content-range").orElse("");
            total = Long.parseLong(range.substring(range.lastIndexOf('/') + 1));
            all.write(r.body());
        }
        long took = Math.max(1, ms(f0));
        log("  chunked", "%d bytes in %d requests, %d ms (%d KB/s, itag %d)", all.size(), requests, took,
                all.size() / took, itag);
        Files.write(file, all.toByteArray());
    }

    private static void expectFailure(StreamingService yt, String url, String label) {
        try {
            StreamInfo.getInfo(yt, url);
            check(false, label + " should fail");
        } catch (Exception e) {
            log("expected-failure", "%s -> %s: %s", label, e.getClass().getSimpleName(), e.getMessage());
        }
    }

    /** An expired or altered URL must come back as a clean HTTP refusal the app can re-resolve on. */
    private static void tamperedUrl(StreamingService yt, String url) throws Exception {
        StreamInfo info = StreamInfo.getInfo(yt, url);
        String content = info.getAudioStreams().get(0).getContent();
        String bad = content.replaceFirst("expire=(\\d+)", "expire=1000000000");
        HttpResponse<Void> r = HTTP.send(HttpRequest.newBuilder(URI.create(bad)).header("Range", "bytes=0-1023")
                .GET().build(), HttpResponse.BodyHandlers.discarding());
        log("tampered-expiry", "HTTP %d", r.statusCode());
        check(r.statusCode() == 403, "tampered expiry is refused with 403");
    }

    private static String query(String url, String key) {
        for (String part : URI.create(url).getRawQuery().split("&")) {
            if (part.startsWith(key + "=")) return part.substring(key.length() + 1);
        }
        return "?";
    }

    private static void check(boolean ok, String what) {
        System.out.println((ok ? "PASS " : "FAIL ") + what);
        if (!ok) failures++;
    }

    private static void log(String tag, String fmt, Object... a) {
        System.out.println("[" + tag + "] " + String.format(fmt, a));
    }

    private static long ms(long t0) { return (System.nanoTime() - t0) / 1_000_000; }

    static final HttpClient HTTP = HttpClient.newBuilder()
            .followRedirects(HttpClient.Redirect.NORMAL).connectTimeout(Duration.ofSeconds(15)).build();

    /** The extractor's only network seam. On Android this becomes OkHttp. */
    static final class JdkDownloader extends Downloader {
        @Override
        public Response execute(Request request) throws IOException {
            HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(request.url())).timeout(Duration.ofSeconds(30));
            for (Map.Entry<String, List<String>> h : request.headers().entrySet()) {
                for (String v : h.getValue()) b.header(h.getKey(), v);
            }
            byte[] data = request.dataToSend();
            b.method(request.httpMethod(), data == null ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofByteArray(data));
            try {
                HttpResponse<String> r = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
                return new Response(r.statusCode(), "", r.headers().map(), r.body(), r.uri().toString());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IOException(e);
            }
        }
    }
}
