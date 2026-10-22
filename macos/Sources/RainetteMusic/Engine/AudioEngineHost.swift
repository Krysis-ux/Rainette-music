import AppKit
import WebKit

/// Hosts the desktop player page (web/miniplayer.html) as an invisible engine.
@MainActor
final class AudioEngineHost: NSObject {
    private var window: NSWindow?
    private(set) var webView: WKWebView?
    private var url: URL?
    var onLoad: () -> Void = {}

    func load(_ handshake: EngineHandshake) {
        var components = URLComponents(url: handshake.baseURL.appendingPathComponent("miniplayer.html"), resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "token", value: handshake.token)]
        url = components.url
        if webView == nil { build() }
        if let url { webView?.load(URLRequest(url: url)) }
    }

    func reload() {
        if let url { webView?.load(URLRequest(url: url)) }
    }

    /// Settings the player reads from its own localStorage at decision time (fade, autoplay-similar).
    func setStorage(_ key: String, _ value: String?) {
        let script: String
        if let value {
            script = "try { localStorage.setItem(\(Self.literal(key)), \(Self.literal(value))) } catch {}"
        } else {
            script = "try { localStorage.removeItem(\(Self.literal(key))) } catch {}"
        }
        webView?.evaluateJavaScript(script)
    }

    func storage(_ key: String) async -> String? {
        guard let webView else { return nil }
        return try? await webView.evaluateJavaScript("localStorage.getItem(\(Self.literal(key)))") as? String
    }

    private static func literal(_ value: String) -> String {
        String(decoding: JSON.string(value).encoded(), as: UTF8.self)
    }

    private func build() {
        let configuration = WKWebViewConfiguration()
        // Playback is started remotely, never by a click inside this page.
        configuration.mediaTypesRequiringUserActionForPlayback = []
        let console = WKUserScript(source: """
            (() => {
                const post = (kind, text) => { try { window.webkit.messageHandlers.engineLog.postMessage(kind + ': ' + text); } catch (_) {} };
                window.addEventListener('error', e => post('error', e.message + ' @' + e.filename + ':' + e.lineno));
                window.addEventListener('unhandledrejection', e => post('rejection', String(e.reason)));
                const original = console.error.bind(console);
                console.error = (...args) => { post('console', args.map(String).join(' ')); original(...args); };
            })();
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(console)
        configuration.userContentController.add(LogHandler(), name: "engineLog")

        let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 352, height: 184), configuration: configuration)
        webView.navigationDelegate = self
        Self.disableOcclusionThrottling(webView)

        // Ordered in but fully transparent and click-through.
        let window = NSWindow(contentRect: NSRect(x: 40, y: 40, width: 352, height: 184),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.alphaValue = 0
        window.ignoresMouseEvents = true
        window.hasShadow = false
        window.isExcludedFromWindowsMenu = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        window.title = "Rainette Engine"
        window.orderBack(nil)
        self.window = window
        self.webView = webView
    }

    /// A window behind other windows is "occluded", and WebKit treats an occluded page as hidden too.
    private static func disableOcclusionThrottling(_ webView: WKWebView) {
        let selector = NSSelectorFromString("_setWindowOcclusionDetectionEnabled:")
        guard webView.responds(to: selector) else { return }
        typealias Setter = @convention(c) (AnyObject, Selector, Bool) -> Void
        unsafeBitCast(webView.method(for: selector), to: Setter.self)(webView, selector, false)
    }

    private final class LogHandler: NSObject, WKScriptMessageHandler {
        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            NSLog("Rainette engine page %@", String(describing: message.body))
        }
    }
}

extension AudioEngineHost: WKNavigationDelegate {
    nonisolated func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        Task { @MainActor in self.onLoad() }
    }

    /// The page's process can be killed under memory pressure.
    nonisolated func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        Task { @MainActor in
            NSLog("Rainette engine page terminated; reloading")
            self.reload()
        }
    }
}
