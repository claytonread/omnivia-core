import AppKit

/// The compact invitation shown under the status item while an acceptable drag
/// hovers. It is a nonactivating floating panel that ignores mouse events, so
/// the drag the person is still holding keeps going to the status button: this
/// never activates the application, never takes key, and sits in no click, menu
/// or accessibility path. It is ordered out again on exit, end and drop.
@MainActor
final class StatusItemDropAffordance {
    private var panel: NSPanel?
    private let label: NSTextField = {
        let field = NSTextField(labelWithString: "")
        field.font = .systemFont(ofSize: NSFont.smallSystemFontSize, weight: .medium)
        field.textColor = .labelColor
        field.alignment = .center
        field.translatesAutoresizingMaskIntoConstraints = false
        return field
    }()

    /// Shows the affordance for a drag hovering over `anchor`, clamped into
    /// `visibleFrame`. Ordering a nonactivating panel front regardless of
    /// activation is deliberate: `makeKeyAndOrderFront` would pull focus out of
    /// the drag.
    func show(_ text: String, anchor: CGRect, visibleFrame: CGRect) {
        let panel = panel ?? makePanel()
        self.panel = panel
        label.stringValue = text
        let fitting = label.fittingSize
        let size = CGSize(width: fitting.width + 20, height: fitting.height + 12)
        panel.setFrame(
            StatusItemDragPolicy.affordanceFrame(
                size: size,
                anchor: anchor,
                visible: visibleFrame
            ),
            display: false
        )
        panel.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: CGRect(origin: .zero, size: CGSize(width: 1, height: 1)),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: true
        )
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .transient, .ignoresCycle]
        let background = NSVisualEffectView()
        background.material = .popover
        background.state = .active
        background.blendingMode = .behindWindow
        background.wantsLayer = true
        background.layer?.cornerRadius = 8
        background.layer?.masksToBounds = true
        // A transient decoration announces nothing of its own: the status
        // button keeps the accessibility the system gave it.
        background.setAccessibilityElement(false)
        background.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: background.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: background.centerYAnchor),
        ])
        panel.contentView = background
        return panel
    }
}
