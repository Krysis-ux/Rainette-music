import SwiftUI

/// Liquid Glass on macOS 26, the closest material everywhere else.
@available(macOS 14, *)
extension View {
    @ViewBuilder
    func glassSurface<S: InsettableShape>(in shape: S, interactive: Bool = false, tint: Color? = nil) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26, *) {
            self.glassEffect(Self.glass(interactive: interactive, tint: tint), in: shape)
        } else {
            self.materialSurface(in: shape, tint: tint)
        }
        #else
        self.materialSurface(in: shape, tint: tint)
        #endif
    }

    #if compiler(>=6.2)
    @available(macOS 26, *)
    private static func glass(interactive: Bool, tint: Color?) -> Glass {
        var glass = Glass.regular
        if let tint { glass = glass.tint(tint) }
        if interactive { glass = glass.interactive() }
        return glass
    }
    #endif

    private func materialSurface<S: InsettableShape>(in shape: S, tint: Color?) -> some View {
        self.background {
            shape.fill(.regularMaterial)
                .overlay { if let tint { shape.fill(tint.opacity(0.18)) } }
                .overlay { shape.strokeBorder(.white.opacity(0.14), lineWidth: 0.5) }
        }
    }

    /// Groups sibling glass so it renders together and can morph.
    @ViewBuilder
    func glassGroup(spacing: CGFloat = 12) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26, *) {
            GlassEffectContainer(spacing: spacing) { self }
        } else {
            self
        }
        #else
        self
        #endif
    }
}

/// A round glass button for transport and card overlays.
@available(macOS 14, *)
struct GlassIconButtonStyle: ButtonStyle {
    var size: CGFloat = 32
    var prominent = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: size * 0.42, weight: .semibold))
            .foregroundStyle(prominent ? Color.white : Color.primary)
            .frame(width: size, height: size)
            .background { if prominent { Circle().fill(Color.accentColor) } }
            .glassSurface(in: Circle(), interactive: true, tint: prominent ? .accentColor : nil)
            .contentShape(Circle())
            .scaleEffect(configuration.isPressed ? 0.9 : 1)
            .animation(.snappy(duration: 0.18), value: configuration.isPressed)
    }
}

/// Capsule action buttons ("Play", "Shuffle") on detail headers.
@available(macOS 14, *)
struct PillButtonStyle: ButtonStyle {
    var prominent = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(prominent ? Color.white : Color.primary)
            .padding(.horizontal, 18)
            .frame(height: 32)
            .background { if prominent { Capsule().fill(Color.accentColor.gradient) } }
            .glassSurface(in: Capsule(), interactive: true, tint: prominent ? .accentColor : nil)
            .contentShape(Capsule())
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(.snappy(duration: 0.18), value: configuration.isPressed)
    }
}
