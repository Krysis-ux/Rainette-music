// swift-tools-version: 5.10
import PackageDescription

// The native macOS front-end. It launches the Python engine (`main.py --engine`)
// and is a client of the same local server every Rainette window talks to.
let package = Package(
    name: "RainetteMusic",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "RainetteMusic", path: "Sources/RainetteMusic"),
    ]
)
