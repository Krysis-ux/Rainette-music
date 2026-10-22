import AppKit

// The SwiftUI interface needs macOS 14.
if #available(macOS 14, *), !ClassicInterface.requested {
    RainetteApp.main()
} else {
    ClassicInterface.launch()
}
