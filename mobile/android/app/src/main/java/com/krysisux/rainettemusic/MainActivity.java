package com.krysisux.rainettemusic;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override public void onCreate(Bundle savedInstanceState) {
        registerPlugin(RainettePlayerPlugin.class);
        registerPlugin(RainetteCompanionPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
