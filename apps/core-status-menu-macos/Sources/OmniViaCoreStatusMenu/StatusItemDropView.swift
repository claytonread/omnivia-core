import AppKit

/// One accepted status-item drag, read at the drop and never before it.
struct StatusItemDropContents {
    var fileURLs: [URL] = []
    var promises: [NSFilePromiseReceiver] = []
    var texts: [String] = []
    /// True when `texts` holds a dropped web link rather than typed text. The
    /// link is kept as the text it is: nothing is fetched over the network.
    var textIsLink = false

    /// How many files the promises in this drag say they will deliver.
    var promisedFileCount: Int { promises.reduce(0) { $0 + $1.fileNames.count } }
}

@MainActor
final class StatusItemDropView: NSView {
    private let onDrop: (StatusItemDropContents) -> Void
    private var acceptsCurrentDrag = false
    private var affordanceTimer: Timer?
    private let affordance = StatusItemDropAffordance()
    private var sessions = DragSessionGate()

    init(onDrop: @escaping (StatusItemDropContents) -> Void) {
        self.onDrop = onDrop
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        var types: [NSPasteboard.PasteboardType] = [.fileURL, .URL, .string]
        for identifier in NSFilePromiseReceiver.readableDraggedTypes {
            types.append(NSPasteboard.PasteboardType(identifier))
        }
        registerForDraggedTypes(types)
        setAccessibilityElement(false)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    override var acceptsFirstResponder: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        superview?.acceptsFirstMouse(for: event) ?? super.acceptsFirstMouse(for: event)
    }

    // The system-created status button keeps its own click, menu tracking and
    // accessibility. This view adds a drop target over it and hands every
    // mouse press straight back.
    override func mouseDown(with event: NSEvent) { superview?.mouseDown(with: event) }
    override func rightMouseDown(with event: NSEvent) { superview?.rightMouseDown(with: event) }
    override func otherMouseDown(with event: NSEvent) { superview?.otherMouseDown(with: event) }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        // Hover decides on the pasteboard's declared types alone: no item is
        // loaded, no promise is received and no file is opened until the drop.
        let pasteboard = sender.draggingPasteboard
        let declaresSomethingReviewable = pasteboard.canReadObject(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        )
            || pasteboard.canReadObject(forClasses: [NSFilePromiseReceiver.self], options: nil)
            || pasteboard.availableType(from: [.URL, .string]) != nil
        acceptsCurrentDrag = sender.draggingSourceOperationMask.contains(.copy)
            && declaresSomethingReviewable
        // The count is the pasteboard's own item count — metadata, not content.
        if acceptsCurrentDrag { scheduleAffordance(declaredItems: sender.numberOfValidItemsForDrop) }
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
              sessions.admit(session: sender.draggingSequenceNumber)
        else { return false }
        let contents = Self.read(sender.draggingPasteboard)
        guard StatusItemDragPolicy.admits(
            fileURLs: contents.fileURLs.count,
            promisedFiles: contents.promisedFileCount,
            texts: contents.texts
        ) else { return false }
        // Accepted input goes to review. This target queues nothing itself.
        onDrop(contents)
        return true
    }

    /// Reads one dropped pasteboard in the order the policy prefers: the
    /// originals a file URL names, else the files the source promises, else the
    /// text or link it offers.
    private static func read(_ pasteboard: NSPasteboard) -> StatusItemDropContents {
        var contents = StatusItemDropContents()
        contents.fileURLs = pasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL] ?? []
        if !contents.fileURLs.isEmpty { return contents }

        contents.promises = pasteboard.readObjects(
            forClasses: [NSFilePromiseReceiver.self],
            options: nil
        ) as? [NSFilePromiseReceiver] ?? []
        if !contents.promises.isEmpty { return contents }

        if let link = (pasteboard.readObjects(forClasses: [NSURL.self], options: nil) as? [URL])?
            .first(where: { !$0.isFileURL })
        {
            contents.texts = [link.absoluteString]
            contents.textIsLink = true
        } else if let text = pasteboard.string(forType: .string),
                  !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        {
            contents.texts = [text]
        }
        return contents
    }

    /// Shows the accept affordance only once the drag has stayed put: the icon
    /// tints and the compact invitation appears beside it. Nothing here
    /// activates the application or takes key.
    private func scheduleAffordance(declaredItems: Int) {
        affordanceTimer?.invalidate()
        let timer = Timer(
            timeInterval: StatusItemDragPolicy.hoverAffordanceDelay,
            repeats: false
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.acceptsCurrentDrag else { return }
                self.layer?.backgroundColor = NSColor.controlAccentColor
                    .withAlphaComponent(0.25).cgColor
                self.presentAffordance(declaredItems: declaredItems)
            }
        }
        // Common modes include event tracking, which is the loop a live drag
        // runs in, so the affordance appears while the person is still holding.
        RunLoop.main.add(timer, forMode: .common)
        affordanceTimer = timer
    }

    private func presentAffordance(declaredItems: Int) {
        guard let window else { return }
        let anchor = window.convertToScreen(convert(bounds, to: nil))
        guard let screen = window.screen
            ?? NSScreen.screens.first(where: { $0.frame.intersects(anchor) })
            ?? NSScreen.main
        else { return }
        affordance.show(
            StatusItemDragPolicy.affordanceLabel(declaredItems: declaredItems),
            anchor: anchor,
            visibleFrame: screen.visibleFrame
        )
    }

    private func clearDragState() {
        affordanceTimer?.invalidate()
        affordanceTimer = nil
        acceptsCurrentDrag = false
        layer?.backgroundColor = nil
        affordance.hide()
    }
}
