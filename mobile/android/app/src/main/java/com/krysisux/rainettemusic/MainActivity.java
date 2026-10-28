package com.krysisux.rainettemusic;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Local plugins must be registered before the bridge starts.
        registerPlugin(RainettePlayerPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
