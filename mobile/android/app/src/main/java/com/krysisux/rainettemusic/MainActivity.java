package com.krysisux.rainettemusic;

import android.os.Bundle;
import android.webkit.WebView;

import androidx.activity.OnBackPressedCallback;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Local plugins must be registered before the bridge starts.
        registerPlugin(RainettePlayerPlugin.class);
        super.onCreate(savedInstanceState);

        // Every sheet the page opens is a history entry, so Back closes the one
        // on top, as it does on any Android app. Capacitor alone has no Back
        // handling, which made Back leave the app from inside any sheet. With
        // nothing open, the app goes to the background rather than finishing:
        // the music and the page stay exactly where they were.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                WebView webView = bridge == null ? null : bridge.getWebView();
                if (webView != null && webView.canGoBack()) {
                    webView.goBack();
                } else {
                    moveTaskToBack(true);
                }
            }
        });
    }
}
