import AppKit
import SwiftUI

enum ArtworkURL {
    static func sized(_ raw: String, pixels: Int) -> URL? {
        guard !raw.isEmpty else { return nil }
        var value = raw
        if let range = value.range(of: #"=w\d+-h\d+"#, options: .regularExpression) {
            value.replaceSubrange(range, with: "=w\(pixels)-h\(pixels)")
        }
        return URL(string: value)
    }
}

/// Loads, caches and de-duplicates artwork.
@available(macOS 14, *)
@MainActor
final class ImagePipeline {
    static let shared = ImagePipeline()

    private let memory = NSCache<NSURL, NSImage>()
    private var inFlight: [URL: Task<NSImage?, Never>] = [:]
    private let session: URLSession

    private init() {
        memory.countLimit = 600
        let configuration = URLSessionConfiguration.default
        configuration.urlCache = URLCache(memoryCapacity: 32 << 20, diskCapacity: 300 << 20)
        configuration.requestCachePolicy = .returnCacheDataElseLoad
        session = URLSession(configuration: configuration)
    }

    func cached(_ url: URL) -> NSImage? { memory.object(forKey: url as NSURL) }

    func image(for url: URL) async -> NSImage? {
        if let hit = cached(url) { return hit }
        if let running = inFlight[url] { return await running.value }
        let session = self.session
        let task = Task<NSImage?, Never> {
            guard let (data, response) = try? await session.data(from: url),
                  (response as? HTTPURLResponse)?.statusCode ?? 200 < 400,
                  let image = NSImage(data: data) else { return nil }
            return Self.trimmingLetterbox(image, from: url)
        }
        inFlight[url] = task
        let image = await task.value
        inFlight[url] = nil
        if let image { memory.setObject(image, forKey: url as NSURL) }
        return image
    }

    /// YouTube's `hqdefault` is a 16:9 frame inside a 4:3 image with black bars baked in.
    private nonisolated static func trimmingLetterbox(_ image: NSImage, from url: URL) -> NSImage {
        guard url.host?.contains("ytimg.com") == true,
              let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return image }
        let width = CGFloat(cg.width), height = CGFloat(cg.height)
        guard abs(width / height - 4.0 / 3.0) < 0.05 else { return image }
        let band = width * 9 / 16
        guard let cropped = cg.cropping(to: CGRect(x: 0, y: (height - band) / 2, width: width, height: band)) else { return image }
        return NSImage(cgImage: cropped, size: NSSize(width: width, height: band))
    }
}

/// Square (or circular) artwork with a warm placeholder while it loads.
@available(macOS 14, *)
struct ArtworkView: View {
    let url: String
    var pixels: Int = 400
    var cornerRadius: CGFloat = 8
    var circle = false
    var placeholderSymbol = "music.note"
    var seed = ""

    @State private var image: NSImage?

    var body: some View {
        let resolved = ArtworkURL.sized(url, pixels: pixels)
        ZStack {
            Placeholder(symbol: placeholderSymbol, seed: seed.isEmpty ? url : seed)
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fill)
                    .transition(.opacity)
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .clipShape(shape)
        .overlay { shape.stroke(Color.primary.opacity(0.08), lineWidth: 0.5) }
        .task(id: resolved) {
            guard let resolved else { image = nil; return }
            if let hit = ImagePipeline.shared.cached(resolved) { image = hit; return }
            let loaded = await ImagePipeline.shared.image(for: resolved)
            withAnimation(.easeOut(duration: 0.25)) { image = loaded }
        }
    }

    private var shape: AnyShape {
        circle ? AnyShape(Circle()) : AnyShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
    }

    private struct Placeholder: View {
        let symbol: String
        let seed: String

        var body: some View {
            // Stable across launches, unlike hashValue, so a card keeps its colour.
            let hash = seed.unicodeScalars.reduce(5381) { ($0 &* 33 &+ Int($1.value)) & 0xFFFF_FFFF }
            let hue = Double(hash % 360) / 360
            LinearGradient(colors: [Color(hue: hue, saturation: 0.32, brightness: 0.62),
                                    Color(hue: (hue + 0.08).truncatingRemainder(dividingBy: 1), saturation: 0.42, brightness: 0.38)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
                .overlay {
                    GeometryReader { proxy in
                        Image(systemName: symbol)
                            .font(.system(size: max(12, proxy.size.width * 0.28), weight: .medium))
                            .foregroundStyle(.white.opacity(0.75))
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
        }
    }
}

/// Artwork blown up and blurred: the backdrop of headers and Now Playing.
@available(macOS 14, *)
struct ArtworkBackdrop: View {
    let url: String
    var blur: CGFloat = 70
    var dim: Double = 0.25

    @State private var image: NSImage?

    var body: some View {
        GeometryReader { proxy in
            ZStack {
                Color(nsColor: .windowBackgroundColor)
                if let image {
                    Image(nsImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fill)
                        .frame(width: proxy.size.width, height: proxy.size.height)
                        .scaleEffect(1.4)
                        .blur(radius: blur, opaque: true)
                        .saturation(1.35)
                        .overlay(Color.black.opacity(dim))
                        .transition(.opacity)
                }
            }
            .clipped()
        }
        .task(id: url) {
            guard let resolved = ArtworkURL.sized(url, pixels: 300) else { image = nil; return }
            let loaded = await ImagePipeline.shared.image(for: resolved)
            withAnimation(.easeInOut(duration: 0.6)) { image = loaded }
        }
    }
}
