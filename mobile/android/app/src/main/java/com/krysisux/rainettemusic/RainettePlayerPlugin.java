package com.krysisux.rainettemusic;

import android.content.ComponentName;
import android.os.Handler;
import android.os.Looper;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MediaMetadata;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.session.MediaController;
import androidx.media3.session.SessionToken;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.google.common.util.concurrent.ListenableFuture;
import java.util.concurrent.Executor;

/** Small command boundary between shared web UI and Android's Media3 player. */
@CapacitorPlugin(name = "RainettePlayer")
public final class RainettePlayerPlugin extends Plugin {
    private static final long TRANSFER_TIMEOUT_MS = 25_000L;
    private ListenableFuture<MediaController> controllerFuture;
    private final Executor mainExecutor = runnable -> getActivity().runOnUiThread(runnable);
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private MediaController observedController;
    private Player.Listener playbackListener;
    private MediaController transferController;
    private Player.Listener transferListener;
    private PluginCall transferCall;
    private Runnable transferTimeout;
    private boolean transferShouldPlay;

    private void controller(PluginCall call, ControllerAction action) {
        if (controllerFuture == null) {
            SessionToken token = new SessionToken(getContext(), new ComponentName(getContext(), RainettePlaybackService.class));
            controllerFuture = new MediaController.Builder(getContext(), token).buildAsync();
        }
        controllerFuture.addListener(() -> {
            try {
                MediaController player = controllerFuture.get();
                observe(player);
                action.run(player);
            }
            catch (Exception error) { call.reject("Native player is unavailable", error); }
        }, mainExecutor);
    }

    private JSObject playerState(Player player) {
        JSObject state = new JSObject();
        state.put("ok", true);
        state.put("playing", player.isPlaying());
        state.put("positionMs", Math.max(0L, player.getCurrentPosition()));
        state.put("ready", player.getPlaybackState() == Player.STATE_READY);
        state.put("playbackState", player.getPlaybackState());
        state.put("repeat", player.getRepeatMode() != Player.REPEAT_MODE_OFF);
        return state;
    }

    private void publishPlayerState(Player player) {
        notifyListeners("rainettePlaybackState", playerState(player));
    }

    private void observe(MediaController player) {
        if (observedController == player) return;
        if (observedController != null && playbackListener != null) {
            observedController.removeListener(playbackListener);
        }
        observedController = player;
        playbackListener = new Player.Listener() {
            @Override public void onIsPlayingChanged(boolean isPlaying) { publishPlayerState(player); }
            @Override public void onPlaybackStateChanged(int playbackState) { publishPlayerState(player); }
            @Override public void onRepeatModeChanged(int repeatMode) { publishPlayerState(player); }
        };
        player.addListener(playbackListener);
    }

    private void clearPendingTransfer() {
        if (transferController != null && transferListener != null) {
            transferController.removeListener(transferListener);
        }
        if (transferTimeout != null) mainHandler.removeCallbacks(transferTimeout);
        transferController = null;
        transferListener = null;
        transferCall = null;
        transferTimeout = null;
    }

    private void failPendingTransfer(String message, boolean clearTarget) {
        MediaController player = transferController;
        PluginCall call = transferCall;
        clearPendingTransfer();
        if (clearTarget && player != null) {
            player.stop();
            player.clearMediaItems();
        }
        if (call != null) call.reject(message);
    }

    private void maybeResolveTransfer(MediaController player) {
        if (transferCall == null || transferController != player) return;
        boolean ready = player.getPlaybackState() == Player.STATE_READY;
        if (!ready || (transferShouldPlay && !player.isPlaying())) return;
        PluginCall call = transferCall;
        JSObject state = playerState(player);
        state.put("transferReady", true);
        clearPendingTransfer();
        publishPlayerState(player);
        call.resolve(state);
    }

    private void prepareTransfer(PluginCall call, MediaController player, JSObject payload) {
        String url = payload.getString("url", "");
        if (url.isEmpty()) { call.reject("A playback URL is required"); return; }
        if (transferCall != null) failPendingTransfer("Transfer was replaced by a newer request", true);

        MediaMetadata metadata = new MediaMetadata.Builder()
            .setTitle(payload.getString("title", "Rainette Music"))
            .setArtist(payload.getString("artist", ""))
            .build();
        transferController = player;
        transferCall = call;
        transferShouldPlay = payload.getBoolean("playing", false);
        transferListener = new Player.Listener() {
            @Override public void onPlaybackStateChanged(int playbackState) { maybeResolveTransfer(player); }
            @Override public void onIsPlayingChanged(boolean isPlaying) { maybeResolveTransfer(player); }
            @Override public void onPlayerError(PlaybackException error) {
                failPendingTransfer("Phone could not load the transfer", true);
            }
        };
        transferTimeout = () -> failPendingTransfer("Phone could not load the transfer in time", true);
        player.addListener(transferListener);
        mainHandler.postDelayed(transferTimeout, TRANSFER_TIMEOUT_MS);

        player.setMediaItem(new MediaItem.Builder().setUri(url).setMediaMetadata(metadata).build());
        player.setRepeatMode(payload.getBoolean("repeat", false) ? Player.REPEAT_MODE_ALL : Player.REPEAT_MODE_OFF);
        player.seekTo(Math.max(0L, (long) payload.optDouble("positionMs", 0d)));
        player.setPlayWhenReady(transferShouldPlay);
        player.prepare();
        if (transferShouldPlay) player.play();
        else player.pause();
        maybeResolveTransfer(player);
    }

    @PluginMethod
    public void command(PluginCall call) {
        String action = call.getString("action", "");
        JSObject payload = call.getObject("payload", new JSObject());
        controller(call, player -> {
            if ("prepareTransfer".equals(action)) {
                prepareTransfer(call, player, payload);
                return;
            }
            if (!"status".equals(action) && transferCall != null) {
                failPendingTransfer("Transfer was interrupted by another playback command", true);
            }
            if ("load".equals(action)) {
                String url = payload.getString("url", "");
                if (url.isEmpty()) { call.reject("A playback URL is required"); return; }
                MediaMetadata metadata = new MediaMetadata.Builder()
                    .setTitle(payload.getString("title", "Rainette Music"))
                    .setArtist(payload.getString("artist", ""))
                    .build();
                player.setMediaItem(new MediaItem.Builder().setUri(url).setMediaMetadata(metadata).build());
                player.prepare();
            } else if ("play".equals(action)) player.play();
            else if ("pause".equals(action)) player.pause();
            else if ("seek".equals(action)) player.seekTo((long) payload.optDouble("positionMs", 0d));
            else if ("next".equals(action)) player.seekToNextMediaItem();
            else if ("previous".equals(action)) player.seekToPreviousMediaItem();
            else if ("repeat".equals(action)) player.setRepeatMode(payload.getBoolean("enabled", false) ? Player.REPEAT_MODE_ALL : Player.REPEAT_MODE_OFF);
            else if ("shuffle".equals(action)) player.setShuffleModeEnabled(payload.getBoolean("enabled", false));
            else if ("status".equals(action)) { /* return the current canonical native state below */ }
            else { call.reject("Unknown player action"); return; }
            JSObject state = playerState(player);
            if (!"status".equals(action)) publishPlayerState(player);
            call.resolve(state);
        });
    }

    @Override protected void handleOnDestroy() {
        if (transferCall != null) failPendingTransfer("Player closed before the transfer completed", true);
        if (observedController != null && playbackListener != null) observedController.removeListener(playbackListener);
        observedController = null;
        playbackListener = null;
        if (controllerFuture != null) MediaController.releaseFuture(controllerFuture);
    }

    private interface ControllerAction { void run(MediaController controller) throws Exception; }
}
