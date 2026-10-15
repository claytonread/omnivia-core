// ============================================================================
// `U.toast` — what happened, said once, over the window.
//
// `#sw-toasts`: a column centred on the window, 20pt off the bottom, stacking
// upward, never taking the mouse. Each `.sw-toast` is a pill that holds for
// 2.8s and fades out over 0.2s.
//
// It is not decoration. A revoke changes a row's state and then says nothing:
// the sheet closes, the pane re-reads, and the only evidence the change
// landed is a chip the person may not be looking at. The toast is the
// confirmation, and it is also the accessible one — each is announced, so the
// outcome reaches a screen reader rather than only the pixels.
//
// Failures do not come here. A write that was refused has somewhere better to
// be said: the sheet that asked for it stays open and says so, which is
// §A8's rule that a refusal is stated where the action was taken, not in
// something that disappears after three seconds.
// ============================================================================

import AppKit

@MainActor
final class OvToastHost {
    enum Kind {
        case ok
        case warning

        var glyph: OvIcon {
            switch self {
            case .ok: return .check
            case .warning: return .alertCircle
            }
        }

        var tint: NSColor {
            switch self {
            case .ok: return Ov.success
            case .warning: return Ov.warning
            }
        }
    }

    private weak var container: NSView?
    private let stack = PassThroughStack()

    /// 2800ms visible, then a 200ms fade, per `U.toast`.
    static let hold: TimeInterval = 2.8
    static let fade: TimeInterval = 0.2

    init(in container: NSView) {
        self.container = container
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 7
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack, positioned: .above, relativeTo: nil)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -20),
            stack.widthAnchor.constraint(lessThanOrEqualTo: container.widthAnchor, multiplier: 0.8),
        ])
    }

    func show(_ message: String, kind: Kind = .ok, appearance: NSAppearance) {
        guard let container else { return }
        var pill: NSView!
        appearance.performAsCurrentDrawingAppearance { pill = self.makePill(message, kind: kind) }
        stack.addArrangedSubview(pill)

        // The outcome, for anyone not watching the window.
        NSAccessibility.post(
            element: container,
            notification: .announcementRequested,
            userInfo: [
                .announcement: message,
                .priority: NSAccessibilityPriorityLevel.medium.rawValue,
            ]
        )

        DispatchQueue.main.asyncAfter(deadline: .now() + Self.hold) { [weak self, weak pill] in
            guard let pill else { return }
            NSAnimationContext.runAnimationGroup { context in
                context.duration = Self.fade
                pill.animator().alphaValue = 0
            } completionHandler: {
                self?.stack.removeView(pill)
            }
        }
    }

    /// `.sw-toast`: a pill, an optional glyph, one line.
    private func makePill(_ message: String, kind: Kind) -> NSView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        row.edgeInsets = NSEdgeInsets(top: 9, left: 14, bottom: 9, right: 14)
        row.wantsLayer = true
        row.layer?.backgroundColor = Ov.materialHud.cgColor
        row.layer?.borderWidth = 1
        row.layer?.borderColor = Ov.borderDefault.cgColor
        row.layer?.shadowColor = NSColor.black.cgColor
        // `--ov-shadow-popover`, which is heavier on dark.
        row.layer?.shadowOpacity = Ov.isDark ? 0.55 : 0.16
        row.layer?.shadowRadius = 12
        row.layer?.shadowOffset = CGSize(width: 0, height: -4)

        let icon = NSImageView()
        icon.image = kind.glyph.image(size: 14)
        icon.contentTintColor = kind.tint
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 14).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 14).isActive = true

        let label = NSTextField(labelWithString: message)
        label.font = OvFont.sans(12.5)
        label.textColor = Ov.textPrimary
        label.lineBreakMode = .byTruncatingTail
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        row.addArrangedSubview(icon)
        row.addArrangedSubview(label)
        // `border-radius: var(--ov-radius-pill)` on a fixed-height row.
        row.translatesAutoresizingMaskIntoConstraints = false
        let height = row.fittingSize.height
        row.layer?.cornerRadius = max(height, 32) / 2
        return row
    }
}

/// `#sw-toasts{pointer-events:none}`: the column is display-only, so a pill
/// resting over a row cannot swallow the click meant for it.
final class PassThroughStack: NSStackView {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
