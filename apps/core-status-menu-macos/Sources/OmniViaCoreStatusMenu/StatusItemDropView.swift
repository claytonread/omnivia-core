import AppKit

@MainActor
final class StatusItemDropView: NSView {
    private let onDrop: ([URL]) -> Void
    private var acceptsCurrentDrag = false

    init(onDrop: @escaping ([URL]) -> Void) {
        self.onDrop = onDrop
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        registerForDraggedTypes([.fileURL])
        setAccessibilityElement(false)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    override var acceptsFirstResponder: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        superview?.acceptsFirstMouse(for: event) ?? super.acceptsFirstMouse(for: event)
    }

    override func mouseDown(with event: NSEvent) {
        // Preserve the system-created status button's native menu tracking.
        superview?.mouseDown(with: event)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        let supportsCopy = sender.draggingSourceOperationMask.contains(.copy)
        acceptsCurrentDrag = supportsCopy && sender.draggingPasteboard.canReadObject(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        )
        layer?.backgroundColor = acceptsCurrentDrag
            ? NSColor.controlAccentColor.withAlphaComponent(0.25).cgColor
            : nil
        return acceptsCurrentDrag ? .copy : []
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard acceptsCurrentDrag, sender.draggingSourceOperationMask.contains(.copy) else { return [] }
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) { clearDragState() }
    override func draggingEnded(_ sender: NSDraggingInfo) { clearDragState() }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        acceptsCurrentDrag && bounds.contains(convert(sender.draggingLocation, from: nil))
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        defer { clearDragState() }
        guard acceptsCurrentDrag,
              let values = sender.draggingPasteboard.readObjects(
                  forClasses: [NSURL.self],
                  options: [.urlReadingFileURLsOnly: true]
              ) as? [URL],
              !values.isEmpty,
              values.count <= 100
        else { return false }
        onDrop(values)
        return true
    }

    private func clearDragState() {
        acceptsCurrentDrag = false
        layer?.backgroundColor = nil
    }
}
