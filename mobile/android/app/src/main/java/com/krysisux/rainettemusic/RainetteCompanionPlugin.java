package com.krysisux.rainettemusic;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import androidx.appcompat.app.AlertDialog;
import androidx.security.crypto.EncryptedSharedPreferences;
import androidx.security.crypto.MasterKey;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.Inet6Address;
import java.net.InetAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.Certificate;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.security.spec.MGF1ParameterSpec;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.crypto.Cipher;
import javax.crypto.spec.OAEPParameterSpec;
import javax.crypto.spec.PSource;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/** Secure native LAN companion client. Pairing secrets never enter the WebView. */
@CapacitorPlugin(name = "RainetteCompanion")
public final class RainetteCompanionPlugin extends Plugin {
    private static final String KEY_ALIAS = "rainette-companion-pairing-rsa";
    private static final String PREFS = "rainette-companion-credentials";
    private static final String PENDING_ACK_REQUEST_ID = "pending_ack_request_id";
    private static final int ACK_MAX_ATTEMPTS = 3;
    private static final long[] ACK_BACKOFF_MS = {250L, 1000L};
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    // Transfer requests deliberately wait for the target's acknowledgement.
    // A separate bounded pool lets that same phone post the acknowledgement
    // while its initiating request is still waiting, avoiding a self-deadlock.
    private final ExecutorService commandWorker = Executors.newFixedThreadPool(3);
    private final ExecutorService syncWorker = Executors.newSingleThreadExecutor();
    private volatile boolean syncRequested = false;

    @Override
    public void load() {
        worker.execute(this::reconcilePendingAcknowledgement);
        startSyncInternal();
        Intent intent = getActivity().getIntent();
        if (intent != null && isPairingUri(intent.getData())) {
            confirmAndStartPairing(intent.getData(), null);
            intent.setData(null);
        }
    }

    @Override
    protected void handleOnNewIntent(Intent intent) {
        if (intent != null && isPairingUri(intent.getData())) {
            confirmAndStartPairing(intent.getData(), null);
            intent.setData(null);
        }
    }

    @PluginMethod
    public void pair(PluginCall call) {
        Uri uri = Uri.parse(call.getString("uri", ""));
        if (!isPairingUri(uri)) {
            call.reject("A valid rainette://pair link is required");
            return;
        }
        confirmAndStartPairing(uri, call);
    }

    @PluginMethod
    public void request(PluginCall call) {
        commandWorker.execute(() -> {
            try {
                SharedPreferences prefs = securePreferences();
                String endpoint = prefs.getString("endpoint", "");
                String pin = prefs.getString("certificate_sha256", "");
                String token = prefs.getString("device_token", "");
                if (endpoint.isEmpty() || pin.isEmpty() || token.isEmpty()) {
                    throw new IllegalStateException("Pair this phone with a Rainette desktop first");
                }
                validateCompanionEndpoint(endpoint);
                JSObject payload = call.getObject("payload", new JSObject());
                HttpsURLConnection connection = openPinned(new URL(endpoint + "/command"), pin);
                connection.setReadTimeout(30_000);
                connection.setRequestProperty("Authorization", "Bearer " + token);
                writeJson(connection, new JSONObject(payload.toString()));
                int status = connection.getResponseCode();
                JSONObject response = readJson(connection, status);
                JSObject result = JSObject.fromJSONObject(response);
                result.put("statusCode", status);
                call.resolve(result);
            } catch (Exception error) {
                call.reject(error.getMessage(), error);
            }
        });
    }

    @PluginMethod
    public void connectionStatus(PluginCall call) {
        worker.execute(() -> {
            JSObject state = new JSObject();
            state.put("type", "rainette_companion_pairing_status");
            boolean hasCredential = false;
            try {
                SharedPreferences prefs = securePreferences();
                String endpoint = prefs.getString("endpoint", "");
                String pin = prefs.getString("certificate_sha256", "");
                String deviceId = prefs.getString("device_id", "");
                String token = prefs.getString("device_token", "");
                hasCredential = !endpoint.isEmpty() && !pin.isEmpty() && !deviceId.isEmpty() && !token.isEmpty();
                if (!hasCredential) {
                    state.put("ok", true);
                    state.put("paired", false);
                    state.put("status", "unpaired");
                    call.resolve(state);
                    return;
                }
                URL endpointUrl = validateCompanionEndpoint(endpoint);
                HttpsURLConnection connection = openPinned(new URL(endpoint + "/status"), pin);
                connection.setRequestMethod("GET");
                connection.setReadTimeout(10_000);
                connection.setRequestProperty("Authorization", "Bearer " + token);
                int statusCode = connection.getResponseCode();
                JSONObject response = readJson(connection, statusCode);
                String authenticatedDeviceId = response.optString("device_id", "");
                if (!deviceId.equals(authenticatedDeviceId)) {
                    throw new SecurityException("Rainette desktop returned a different device identity");
                }
                state.put("ok", true);
                state.put("paired", true);
                state.put("status", "connected");
                state.put("device_id", deviceId);
                state.put("endpoint_host", endpointUrl.getHost());
                if (response.has("capabilities")) {
                    state.put("capabilities", response.getJSONArray("capabilities"));
                }
                call.resolve(state);
            } catch (Exception error) {
                // An unreachable desktop does not erase a durable credential.
                // Report a reconnecting state so the UI can distinguish it
                // from a phone that has never been paired.
                state.put("ok", false);
                state.put("paired", hasCredential);
                state.put("status", hasCredential ? "reconnecting" : "unpaired");
                state.put("msg", error.getMessage());
                call.resolve(state);
            }
        });
    }

    @PluginMethod
    public void startSync(PluginCall call) {
        startSyncInternal();
        JSObject result = new JSObject();
        result.put("ok", true);
        call.resolve(result);
    }

    @PluginMethod
    public void stopSync(PluginCall call) {
        syncRequested = false;
        JSObject result = new JSObject();
        result.put("ok", true);
        call.resolve(result);
    }

    private synchronized void startSyncInternal() {
        if (syncRequested) return;
        syncRequested = true;
        syncWorker.execute(this::runSyncLoop);
    }

    private void runSyncLoop() {
        long revision = 0L;
        String activeEndpoint = "";
        String activeDeviceId = "";
        String activeToken = "";
        while (syncRequested && !Thread.currentThread().isInterrupted()) {
            try {
                SharedPreferences prefs = securePreferences();
                String endpoint = prefs.getString("endpoint", "");
                String pin = prefs.getString("certificate_sha256", "");
                String deviceId = prefs.getString("device_id", "");
                String token = prefs.getString("device_token", "");
                if (endpoint.isEmpty() || pin.isEmpty() || deviceId.isEmpty() || token.isEmpty()) {
                    revision = 0L;
                    activeEndpoint = "";
                    activeDeviceId = "";
                    activeToken = "";
                    Thread.sleep(1000L);
                    continue;
                }
                if (!endpoint.equals(activeEndpoint) || !deviceId.equals(activeDeviceId) || !token.equals(activeToken)) {
                    // Pairing can replace credentials while the long-poll loop
                    // is already running.  Revisions are scoped to one desktop
                    // broker and must never carry across that identity change.
                    revision = 0L;
                    activeEndpoint = endpoint;
                    activeDeviceId = deviceId;
                    activeToken = token;
                }
                validateCompanionEndpoint(endpoint);
                HttpsURLConnection connection = openPinned(new URL(endpoint + "/events?after=" + revision + "&wait=25"), pin);
                connection.setRequestMethod("GET");
                connection.setReadTimeout(30_000);
                connection.setRequestProperty("Authorization", "Bearer " + token);
                JSONObject response = readJson(connection, connection.getResponseCode());
                SharedPreferences latest = securePreferences();
                if (!endpoint.equals(latest.getString("endpoint", ""))
                    || !deviceId.equals(latest.getString("device_id", ""))
                    || !token.equals(latest.getString("device_token", ""))) {
                    // Pairing changed while the old desktop long-poll was in
                    // flight.  Drop that stale response rather than applying
                    // another desktop's playback or library event.
                    revision = 0L;
                    activeEndpoint = "";
                    activeDeviceId = "";
                    activeToken = "";
                    continue;
                }
                long responseRevision = response.optLong("revision", revision);
                revision = response.optBoolean("reset_required", false)
                    ? responseRevision
                    : Math.max(revision, responseRevision);
                JSObject update = JSObject.fromJSONObject(response);
                notifyListeners("rainetteCompanionSync", update, true);
            } catch (Exception error) {
                JSObject failed = new JSObject();
                failed.put("ok", false);
                failed.put("status", "reconnecting");
                failed.put("msg", error.getMessage());
                notifyListeners("rainetteCompanionSync", failed, true);
                try { Thread.sleep(1500L); }
                catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); return; }
            }
        }
    }

    private void confirmAndStartPairing(Uri uri, PluginCall call) {
        try {
            URL endpointUrl = validatePairingEndpoint(uri);
            // Validate every secret before presenting a trusted-looking native dialog.
            normalizePin(required(uri, "certificate_sha256"));
            required(uri, "invitation");
            String host = endpointUrl.getHost();
            getActivity().runOnUiThread(() -> new AlertDialog.Builder(getActivity())
                .setTitle("Pair with Rainette desktop")
                .setMessage("Connect to " + host + "? This will replace the desktop currently paired with this phone.")
                .setPositiveButton("Pair", (dialog, which) -> startPairing(uri, call))
                .setNegativeButton("Cancel", (dialog, which) ->
                    finishPairing(call, false, "cancelled", "Pairing was cancelled"))
                .setOnCancelListener(dialog ->
                    finishPairing(call, false, "cancelled", "Pairing was cancelled"))
                .show());
        } catch (Exception error) {
            finishPairing(call, false, "failed", error.getMessage());
        }
    }

    private void startPairing(Uri uri, PluginCall call) {
        worker.execute(() -> {
            try {
                emitPairingProgress("connecting", "Contacting Rainette desktop");
                String endpoint = required(uri, "endpoint");
                String pin = normalizePin(required(uri, "certificate_sha256"));
                String invitation = required(uri, "invitation");
                validatePairingEndpoint(uri);
                KeyPair keyPair = getOrCreatePairingKey();
                JSONObject request = new JSONObject()
                    .put("invitation", invitation)
                    .put("device_name", android.os.Build.MODEL)
                    .put("public_key", Base64.encodeToString(keyPair.getPublic().getEncoded(), Base64.NO_WRAP));
                HttpsURLConnection pairing = openPinned(new URL(endpoint + "/pair/request"), pin);
                writeJson(pairing, request);
                JSONObject pending = readJson(pairing, pairing.getResponseCode());
                String requestId = pending.getString("request_id");
                emitPairingProgress("pending_approval", "Waiting for desktop approval");

                long deadline = System.currentTimeMillis() + 300_000L;
                while (System.currentTimeMillis() < deadline) {
                    JSONObject proof = new JSONObject()
                        .put("request_id", requestId)
                        .put("invitation", invitation);
                    HttpsURLConnection poll = openPinned(new URL(endpoint + "/pair/result"), pin);
                    writeJson(poll, proof);
                    int statusCode = poll.getResponseCode();
                    JSONObject result = readJson(poll, statusCode);
                    String status = result.optString("status", "");
                    if ("approved".equals(status)) {
                        emitPairingProgress("securing", "Securing this phone");
                        String token = decryptToken(keyPair, result.getString("encrypted_device_token"));
                        boolean stored = securePreferences().edit()
                            .putString("endpoint", endpoint)
                            .putString("certificate_sha256", pin)
                            .putString("device_id", result.getString("device_id"))
                            .putString("device_token", token)
                            .putString("pending_ack_request_id", requestId)
                            .commit();
                        if (!stored) throw new IllegalStateException("Could not securely store the desktop credential");
                        if (acknowledgePairingWithRetry(endpoint, pin, token, requestId)) {
                            clearPendingAcknowledgement(securePreferences(), requestId);
                        }
                        startSyncInternal();
                        finishPairing(call, true, "approved", null);
                        return;
                    }
                    if ("rejected".equals(status) || "expired".equals(status)) {
                        finishPairing(call, false, status, "Pairing was " + status);
                        return;
                    }
                    Thread.sleep(1200L);
                }
                finishPairing(call, false, "expired", "Pairing timed out");
            } catch (Exception error) {
                finishPairing(call, false, "failed", error.getMessage());
            }
        });
    }

    private void reconcilePendingAcknowledgement() {
        try {
            SharedPreferences prefs = securePreferences();
            String requestId = prefs.getString("pending_ack_request_id", "");
            if (requestId.isEmpty()) return;
            String endpoint = prefs.getString("endpoint", "");
            String pin = prefs.getString("certificate_sha256", "");
            String token = prefs.getString("device_token", "");
            if (endpoint.isEmpty() || pin.isEmpty() || token.isEmpty()) return;
            emitPairingProgress("securing", "Finishing secure pairing");
            validateCompanionEndpoint(endpoint);
            if (acknowledgePairingWithRetry(endpoint, pin, token, requestId)) {
                clearPendingAcknowledgement(prefs, requestId);
                emitPairingProgress("approved", null);
            }
        } catch (Exception ignored) {
            // Keep the encrypted pending request id for the next app startup.
        }
    }

    private boolean acknowledgePairingWithRetry(
        String endpoint, String pin, String token, String requestId
    ) {
        for (int attempt = 0; attempt < ACK_MAX_ATTEMPTS; attempt++) {
            try {
                HttpsURLConnection acknowledgement = openPinned(new URL(endpoint + "/pair/ack"), pin);
                acknowledgement.setRequestProperty("Authorization", "Bearer " + token);
                writeJson(acknowledgement, new JSONObject().put("request_id", requestId));
                int status = acknowledgement.getResponseCode();
                if (status >= 200 && status < 300) {
                    readJson(acknowledgement, status);
                    return true;
                }
                if (!isTransientAckStatus(status)) return true;
                readJson(acknowledgement, status);
            } catch (Exception ignored) {
                // Connection, timeout, and TLS failures may clear on the next bounded attempt/startup.
            }
            if (attempt + 1 < ACK_MAX_ATTEMPTS) {
                try {
                    Thread.sleep(ACK_BACKOFF_MS[Math.min(attempt, ACK_BACKOFF_MS.length - 1)]);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    return false;
                }
            }
        }
        return false;
    }

    private static boolean isTransientAckStatus(int status) {
        return status == 408 || status == 425 || status == 429 || status >= 500;
    }

    private static void clearPendingAcknowledgement(SharedPreferences prefs, String requestId) {
        if (requestId.equals(prefs.getString(PENDING_ACK_REQUEST_ID, ""))) {
            prefs.edit().remove("pending_ack_request_id").commit();
        }
    }

    private void finishPairing(PluginCall call, boolean ok, String status, String message) {
        JSObject result = new JSObject();
        result.put("type", "rainette_companion_pairing");
        result.put("ok", ok);
        result.put("status", status);
        if (message != null) result.put("msg", message);
        if (call != null) {
            if (ok) call.resolve(result); else call.reject(message == null ? status : message);
        }
        notifyListeners("rainetteCompanionMessage", result, true);
    }

    private void emitPairingProgress(String status, String message) {
        JSObject progress = new JSObject();
        progress.put("type", "rainette_companion_pairing");
        progress.put("ok", true);
        progress.put("status", status);
        if (message != null) progress.put("msg", message);
        notifyListeners("rainetteCompanionMessage", progress, true);
    }

    private KeyPair getOrCreatePairingKey() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (store.containsAlias(KEY_ALIAS)) {
            Certificate certificate = store.getCertificate(KEY_ALIAS);
            return new KeyPair(certificate.getPublicKey(), (java.security.PrivateKey) store.getKey(KEY_ALIAS, null));
        }
        KeyPairGenerator generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_RSA, "AndroidKeyStore");
        generator.initialize(new KeyGenParameterSpec.Builder(
            KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
            .setKeySize(2048)
            .setDigests(KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_SHA512)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_RSA_OAEP)
            .build());
        return generator.generateKeyPair();
    }

    private String decryptToken(KeyPair keyPair, String encrypted) throws Exception {
        Cipher cipher = Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding");
        OAEPParameterSpec spec = new OAEPParameterSpec(
            "SHA-256", "MGF1", MGF1ParameterSpec.SHA1, PSource.PSpecified.DEFAULT);
        cipher.init(Cipher.DECRYPT_MODE, keyPair.getPrivate(), spec);
        byte[] clear = cipher.doFinal(Base64.decode(encrypted, Base64.DEFAULT));
        return new String(clear, StandardCharsets.UTF_8);
    }

    private SharedPreferences securePreferences() throws Exception {
        Context context = getContext();
        MasterKey master = new MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build();
        return EncryptedSharedPreferences.create(
            context, PREFS, master,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM);
    }

    private HttpsURLConnection openPinned(URL url, String pin) throws Exception {
        byte[] expected = hex(normalizePin(pin));
        X509TrustManager trust = new X509TrustManager() {
            @Override public void checkClientTrusted(X509Certificate[] chain, String authType) throws CertificateException {
                throw new CertificateException("Client certificates are not accepted");
            }
            @Override public void checkServerTrusted(X509Certificate[] chain, String authType) throws CertificateException {
                if (chain == null || chain.length == 0 || !certificateMatchesPin(chain[0], expected)) {
                    throw new CertificateException("Rainette desktop certificate pin mismatch");
                }
            }
            @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
        };
        SSLContext ssl = SSLContext.getInstance("TLS");
        ssl.init(null, new TrustManager[]{trust}, new SecureRandom());
        HttpsURLConnection connection = (HttpsURLConnection) url.openConnection();
        connection.setSSLSocketFactory(ssl.getSocketFactory());
        connection.setHostnameVerifier((hostname, session) -> {
            try {
                Certificate[] peer = session.getPeerCertificates();
                return peer.length > 0 && certificateMatchesPin((X509Certificate) peer[0], expected);
            } catch (Exception ignored) {
                return false;
            }
        });
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(8000);
        connection.setRequestProperty("Accept", "application/json");
        return connection;
    }

    private static boolean certificateMatchesPin(X509Certificate certificate, byte[] expected) {
        try {
            byte[] actual = MessageDigest.getInstance("SHA-256").digest(certificate.getEncoded());
            return MessageDigest.isEqual(actual, expected);
        } catch (Exception error) {
            return false;
        }
    }

    private static void writeJson(HttpsURLConnection connection, JSONObject payload) throws Exception {
        connection.setRequestMethod("POST");
        connection.setDoOutput(true);
        connection.setRequestProperty("Content-Type", "application/json");
        try (OutputStream output = connection.getOutputStream()) {
            output.write(payload.toString().getBytes(StandardCharsets.UTF_8));
        }
    }

    private static JSONObject readJson(HttpsURLConnection connection, int status) throws Exception {
        InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
        if (stream == null) return new JSONObject();
        StringBuilder content = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) content.append(line);
        }
        JSONObject result = new JSONObject(content.toString());
        if (status >= 400 && status != 410) {
            throw new IllegalStateException(result.optString("msg", "Companion request failed"));
        }
        return result;
    }

    private static boolean isPairingUri(Uri uri) {
        return uri != null && "rainette".equalsIgnoreCase(uri.getScheme()) && "pair".equalsIgnoreCase(uri.getHost());
    }

    private static URL validatePairingEndpoint(Uri uri) throws Exception {
        return validateCompanionEndpoint(required(uri, "endpoint"));
    }

    private static URL validateCompanionEndpoint(String value) throws Exception {
        URL endpoint = new URL(value);
        if (!"https".equalsIgnoreCase(endpoint.getProtocol())) {
            throw new IllegalArgumentException("Pairing endpoint must use HTTPS");
        }
        if (!isAllowedEndpointHost(endpoint.getHost())) {
            throw new IllegalArgumentException("Pairing endpoint must use a private or local host");
        }
        String path = endpoint.getPath();
        if (endpoint.getUserInfo() != null || endpoint.getQuery() != null || endpoint.getRef() != null
            || (path != null && !path.isEmpty() && !"/".equals(path))) {
            throw new IllegalArgumentException("Pairing endpoint must be a server origin");
        }
        return endpoint;
    }

    private static boolean isAllowedEndpointHost(String host) {
        if (host == null) return false;
        String clean = host.trim().toLowerCase(Locale.US);
        if (clean.endsWith(".")) clean = clean.substring(0, clean.length() - 1);
        if ("localhost".equals(clean) || clean.endsWith(".local")) return true;

        if (clean.contains(":")) {
            try {
                InetAddress address = InetAddress.getByName(clean);
                if (!(address instanceof Inet6Address)) return false;
                byte[] bytes = address.getAddress();
                boolean uniqueLocal = bytes.length == 16 && (bytes[0] & 0xfe) == 0xfc;
                return address.isLoopbackAddress() || address.isLinkLocalAddress()
                    || address.isSiteLocalAddress() || uniqueLocal;
            } catch (Exception ignored) {
                return false;
            }
        }

        String[] pieces = clean.split("\\.", -1);
        if (pieces.length != 4) return false;
        int[] octets = new int[4];
        try {
            for (int index = 0; index < pieces.length; index++) {
                if (pieces[index].isEmpty() || !pieces[index].matches("[0-9]{1,3}")) return false;
                octets[index] = Integer.parseInt(pieces[index]);
                if (octets[index] > 255) return false;
            }
        } catch (NumberFormatException ignored) {
            return false;
        }
        return octets[0] == 10
            || octets[0] == 127
            || (octets[0] == 169 && octets[1] == 254)
            || (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31)
            || (octets[0] == 192 && octets[1] == 168);
    }

    private static String required(Uri uri, String name) {
        String value = uri.getQueryParameter(name);
        if (value == null || value.trim().isEmpty()) throw new IllegalArgumentException("Missing " + name);
        return value.trim();
    }

    private static String normalizePin(String pin) {
        String clean = pin.replace(":", "").trim().toLowerCase(Locale.US);
        if (!clean.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("Invalid certificate_sha256 pin");
        return clean;
    }

    private static byte[] hex(String value) {
        byte[] decoded = new byte[value.length() / 2];
        for (int index = 0; index < decoded.length; index++) {
            decoded[index] = (byte) Integer.parseInt(value.substring(index * 2, index * 2 + 2), 16);
        }
        return decoded;
    }

    @Override
    protected void handleOnDestroy() {
        syncRequested = false;
        worker.shutdownNow();
        commandWorker.shutdownNow();
        syncWorker.shutdownNow();
    }
}
