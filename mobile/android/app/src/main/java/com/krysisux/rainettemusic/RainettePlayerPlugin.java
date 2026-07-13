package com.krysisux.rainettemusic;

import android.content.ComponentName;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MediaMetadata;
import androidx.media3.common.Player;
import androidx.media3.session.MediaController;
import androidx.media3.session.SessionToken;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.PluginMethod;
import com.google.common.util.concurrent.ListenableFuture;
import java.util.concurrent.Executor;

/** Small command boundary between shared web UI and Android's Media3 player. */
@CapacitorPlugin(name = "RainettePlayer")
public final class RainettePlayerPlugin extends Plugin {
    private ListenableFuture<MediaController> controllerFuture;
    private final Executor mainExecutor = runnable -> getActivity().runOnUiThread(runnable);

    private void controller(PluginCall call, ControllerAction action) {
        if (controllerFuture == null) {
            SessionToken token = new SessionToken(getContext(), new ComponentName(getContext(), RainettePlaybackService.class));
            controllerFuture = new MediaController.Builder(getContext(), token).buildAsync();
        }
        controllerFuture.addListener(() -> {
            try { action.run(controllerFuture.get()); }
            catch (Exception error) { call.reject("Native player is unavailable", error); }
        }, mainExecutor);
    }

    @PluginMethod
    public void command(PluginCall call) {
        String action = call.getString("action", "");
        JSObject payload = call.getObject("payload", new JSObject());
        controller(call, player -> {
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
            else if ("seek".equals(action)) player.seekTo((long) (payload.getDouble("positionMs", 0d)));
            else if ("next".equals(action)) player.seekToNextMediaItem();
            else if ("previous".equals(action)) player.seekToPreviousMediaItem();
            else if ("repeat".equals(action)) player.setRepeatMode(payload.getBoolean("enabled", false) ? Player.REPEAT_MODE_ALL : Player.REPEAT_MODE_OFF);
            else if ("shuffle".equals(action)) player.setShuffleModeEnabled(payload.getBoolean("enabled", false));
            else { call.reject("Unknown player action"); return; }
            JSObject state = new JSObject();
            state.put("playing", player.isPlaying());
            state.put("positionMs", player.getCurrentPosition());
            notifyListeners("rainettePlaybackState", state);
            call.resolve(state);
        });
    }

    @Override protected void handleOnDestroy() {
        if (controllerFuture != null) MediaController.releaseFuture(controllerFuture);
    }

    private interface ControllerAction { void run(MediaController controller) throws Exception; }
}
