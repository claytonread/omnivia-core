import AppKit
import DocumentIntakeMacOS

@MainActor
final class CapturePanelDropView: NSView {
    private let onDrop: ([URL]) -> Void

    init(onDrop: @escaping ([URL]) -> Void) {
        self.onDrop = onDrop
        super.init(frame: .zero)
        registerForDraggedTypes([.fileURL])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        sender.draggingSourceOperationMask.contains(.copy)
            && sender.draggingPasteboard.canReadObject(
                forClasses: [NSURL.self],
                options: [.urlReadingFileURLsOnly: true]
            )
            ? .copy : []
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        draggingEntered(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let urls = sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL],
              !urls.isEmpty,
              urls.count <= 100
        else { return false }
        onDrop(urls)
        return true
    }
}
