import AppKit
import SwiftUI

/// The five-band EQ.
@available(macOS 14, *)
struct EqualizerPanel: View {
    static let bands: [(name: String, frequency: String)] = [
        ("Bass", "60"), ("Low", "250"), ("Mid", "1k"), ("High", "4k"), ("Treble", "12k"),
    ]
    static let presetNames = ["Flat", "Bass Boost", "Vocal", "Treble"]

    @Environment(AppModel.self) private var app
    @Environment(PlayerModel.self) private var player
    /// The value under the pointer while a band is dragged.
    @State private var editing: [Int: Double] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 10) {
                Text("Equalizer").font(.system(size: 15, weight: .bold))
                Spacer()
                Menu {
                    ForEach(Self.presetNames, id: \.self) { name in
                        Button(name) { player.applyEQPreset(name); app.sounds.play(.tap) }
                    }
                } label: {
                    Text(currentPreset ?? "Custom")
                }
                .menuStyle(.button)
                .fixedSize()
                Toggle("Equalizer", isOn: Binding(get: { player.eqOn }, set: { player.setEQ(on: $0) }))
                    .toggleStyle(.switch)
                    .labelsHidden()
            }

            ZStack {
                ResponseCurve(values: (0..<5).map(value), range: PlayerModel.eqRange)
                    .stroke(Color.accentColor.opacity(player.eqOn ? 0.35 : 0.15),
                            style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                    .padding(.vertical, EQBandSlider.knobSize / 2)
                HStack(spacing: 0) {
                    ForEach(0..<5, id: \.self) { band in
                        EQBandSlider(value: value(band), enabled: player.eqOn) { gain in
                            editing[band] = gain
                            player.setEQBand(band, gain: gain)
                        } onEnd: {
                            editing[band] = nil
                        }
                        .frame(maxWidth: .infinity)
                        .accessibilityLabel("\(Self.bands[band].name), \(Self.bands[band].frequency) hertz")
                    }
                }
            }
            .frame(height: 168)

            HStack(spacing: 0) {
                ForEach(0..<5, id: \.self) { band in
                    VStack(spacing: 2) {
                        Text(Self.format(value(band)))
                            .font(.system(size: 11, weight: .semibold).monospacedDigit())
                            .foregroundStyle(value(band) == 0 ? Color.secondary : Color.primary)
                        Text(Self.bands[band].name).font(.system(size: 11, weight: .medium))
                        Text(Self.bands[band].frequency).font(.system(size: 10)).foregroundStyle(.tertiary)
                    }
                    .frame(maxWidth: .infinity)
                }
            }

            HStack {
                Text("Drag a band, or double-click it to reset.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button("Reset") { player.applyEQPreset("Flat"); app.sounds.play(.tap) }
                    .controlSize(.small)
                    .disabled(player.eqGains.allSatisfy { $0 == 0 })
            }
        }
    }

    private func value(_ band: Int) -> Double { editing[band] ?? player.eqGains[band] }

    private var currentPreset: String? {
        PlayerModel.eqPresets.first { $0.value == player.eqGains }?.key
    }

    static func format(_ gain: Double) -> String {
        gain == 0 ? "0 dB" : String(format: gain.rounded() == gain ? "%+.0f dB" : "%+.1f dB", gain)
    }
}

/// One vertical band.
@available(macOS 14, *)
struct EQBandSlider: View {
    static let knobSize: CGFloat = 18
    static let step = 0.5

    let value: Double
    let enabled: Bool
    let onChange: (Double) -> Void
    let onEnd: () -> Void

    @State private var hovering = false
    @State private var dragging = false

    var body: some View {
        GeometryReader { proxy in
            let range = PlayerModel.eqRange
            let travel = proxy.size.height - Self.knobSize
            let y: (Double) -> CGFloat = { gain in
                Self.knobSize / 2 + CGFloat(1 - (gain - range.lowerBound) / (range.upperBound - range.lowerBound)) * travel
            }
            let midX = proxy.size.width / 2
            ZStack(alignment: .topLeading) {
                Capsule()
                    .fill(Color.primary.opacity(0.12))
                    .frame(width: 4, height: travel)
                    .position(x: midX, y: proxy.size.height / 2)
                Capsule()
                    .fill(Color.primary.opacity(0.35))
                    .frame(width: 12, height: 1.5)
                    .position(x: midX, y: y(0))
                Capsule()
                    .fill(Color.accentColor.opacity(enabled ? 1 : 0.4))
                    .frame(width: 4, height: max(0, abs(y(value) - y(0))))
                    .position(x: midX, y: (y(value) + y(0)) / 2)
                Circle()
                    .fill(.white)
                    .overlay(Circle().strokeBorder(Color.black.opacity(0.08), lineWidth: 0.5))
                    .shadow(color: .black.opacity(dragging ? 0.35 : 0.22), radius: dragging ? 5 : 3, y: 1.5)
                    .frame(width: Self.knobSize, height: Self.knobSize)
                    .scaleEffect(dragging ? 1.15 : (hovering ? 1.06 : 1))
                    .position(x: midX, y: y(value))
                    .animation(.snappy(duration: 0.15), value: dragging)
            }
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { drag in
                        dragging = true
                        let fraction = 1 - Double((drag.location.y - Self.knobSize / 2) / max(travel, 1))
                        let raw = range.lowerBound + fraction * (range.upperBound - range.lowerBound)
                        let gain = min(max((raw / Self.step).rounded() * Self.step, range.lowerBound), range.upperBound)
                        guard gain != value else { return }
                        // A detent at 0 dB, felt on the trackpad.
                        if gain == 0 || (gain > 0) != (value > 0) {
                            NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now)
                        }
                        onChange(gain)
                    }
                    .onEnded { _ in
                        dragging = false
                        onEnd()
                    }
            )
            .simultaneousGesture(TapGesture(count: 2).onEnded { onChange(0); onEnd() })
            .onHover { hovering = $0 }
        }
        .accessibilityElement()
        .accessibilityValue(EqualizerPanel.format(value))
        .accessibilityAdjustableAction { direction in
            let next = value + (direction == .increment ? 1 : -1)
            onChange(min(max(next, PlayerModel.eqRange.lowerBound), PlayerModel.eqRange.upperBound))
            onEnd()
        }
    }
}

/// A smooth line through the five knobs: the shape of the sound at a glance.
@available(macOS 14, *)
struct ResponseCurve: Shape {
    let values: [Double]
    let range: ClosedRange<Double>

    func path(in rect: CGRect) -> Path {
        guard values.count > 1 else { return Path() }
        let points = values.enumerated().map { index, gain in
            CGPoint(x: rect.minX + rect.width * (CGFloat(index) + 0.5) / CGFloat(values.count),
                    y: rect.minY + rect.height * CGFloat(1 - (gain - range.lowerBound) / (range.upperBound - range.lowerBound)))
        }
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: points[0].y))
        path.addLine(to: points[0])
        for index in 1..<points.count {
            let a = points[index - 1], b = points[index]
            let mid = (a.x + b.x) / 2
            path.addCurve(to: b, control1: CGPoint(x: mid, y: a.y), control2: CGPoint(x: mid, y: b.y))
        }
        path.addLine(to: CGPoint(x: rect.maxX, y: points[points.count - 1].y))
        return path
    }
}
