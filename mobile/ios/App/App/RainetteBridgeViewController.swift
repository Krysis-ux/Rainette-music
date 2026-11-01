import Capacitor
import UIKit

/// Capacitor's view controller with the app's own player plugged in. The
/// plugin lives in the app rather than in an npm package, so it is registered
/// here instead of being found by `cap sync`.
class RainetteBridgeViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(RainettePlayerPlugin())
    }
}
