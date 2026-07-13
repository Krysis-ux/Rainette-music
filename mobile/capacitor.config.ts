import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.krysisux.rainettemusic',
  appName: 'Rainette Music',
  webDir: '../web',
  bundledWebRuntime: false,
  android: {
    allowMixedContent: false,
    backgroundColor: '#15211a'
  }
};

export default config;
