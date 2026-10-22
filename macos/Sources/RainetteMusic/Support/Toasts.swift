import SwiftUI
import Observation

/// Short-lived status capsules: "Added to Road Trip", "Couldn't play …".
@available(macOS 14, *)
@Observable @MainActor
final class ToastCenter {
    enum Style { case info, success, error }

    struct Toast: Identifiable, Equatable {
        let id = UUID()
        let message: String
        let symbol: String
        let style: Style
    }

    private(set) var current: Toast?
    @ObservationIgnored private var dismissal: Task<Void, Never>?

    func show(_ message: String, symbol: String, style: Style = .info, sticky: Bool = false) {
        let toast = Toast(message: message, symbol: symbol, style: style)
        withAnimation(.snappy(duration: 0.28)) { current = toast }
        dismissal?.cancel()
        let seconds: UInt64 = sticky ? 30 : (style == .error ? 6 : 3)
        dismissal = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: seconds * 1_000_000_000)
            guard let self, !Task.isCancelled, self.current?.id == toast.id else { return }
            withAnimation(.smooth(duration: 0.35)) { self.current = nil }
        }
    }

    func dismiss() { withAnimation(.smooth(duration: 0.25)) { current = nil } }
}

@available(macOS 14, *)
struct ToastView: View {
    let toast: ToastCenter.Toast

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: toast.symbol)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(tint)
                .symbolRenderingMode(.hierarchical)
            Text(toast.message)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(2)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .glassSurface(in: Capsule())
        .shadow(color: .black.opacity(0.12), radius: 16, y: 6)
    }

    private var tint: Color {
        switch toast.style {
        case .info: return .accentColor
        case .success: return .green
        case .error: return .orange
        }
    }
}
