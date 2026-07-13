package com.krysisux.rainettemusic;

import androidx.annotation.Nullable;
import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.session.MediaSession;
import androidx.media3.session.MediaSessionService;

/** Android-owned player; Media3 keeps it alive past the Capacitor activity. */
public final class RainettePlaybackService extends MediaSessionService {
    private MediaSession mediaSession;

    @Override public void onCreate() {
        super.onCreate();
        ExoPlayer player = new ExoPlayer.Builder(this).build();
        player.setAudioAttributes(new AudioAttributes.Builder()
            .setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true);
        mediaSession = new MediaSession.Builder(this, player).build();
    }

    @Override public @Nullable MediaSession onGetSession(MediaSession.ControllerInfo controllerInfo) { return mediaSession; }

    @Override public void onDestroy() {
        if (mediaSession != null) { mediaSession.getPlayer().release(); mediaSession.release(); mediaSession = null; }
        super.onDestroy();
    }
}
