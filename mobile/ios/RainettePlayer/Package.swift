// swift-tools-version: 5.9
import PackageDescription

// The iPhone app's player: search, stream resolution and playback, all on the
// phone. It builds for macOS too, so its tests run on a Mac without a phone.
let package = Package(
    name: "RainettePlayer",
    platforms: [.iOS(.v15), .macOS(.v13)],
    products: [
        .library(name: "RainettePlayerCore", targets: ["RainettePlayerCore"]),
    ],
    dependencies: [
        // MIT. Resolves a song to its audio on the device. Only its local
        // extractor is ever used; see Resolver.swift.
        .package(url: "https://github.com/alexeichhorn/YouTubeKit.git", exact: "0.4.10"),
    ],
    targets: [
        .target(
            name: "RainettePlayerCore",
            dependencies: [.product(name: "YouTubeKit", package: "YouTubeKit")]
        ),
        .testTarget(
            name: "RainettePlayerCoreTests",
            dependencies: ["RainettePlayerCore"],
            resources: [.copy("Fixtures")]
        ),
    ]
)
