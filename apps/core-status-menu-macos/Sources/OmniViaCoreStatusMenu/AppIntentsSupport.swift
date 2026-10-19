import AppIntents
import DocumentIntakeMacOS
import Foundation
import UniformTypeIdentifiers

@MainActor
enum ShortcutIntakeRuntime {
    private static var snapshotStore: OwnedSnapshotStore?
    private static var openHandler: (() -> Void)?
    private static var reviewHandler: (([URL]) -> Void)?

    static func configure(
        snapshotStore: OwnedSnapshotStore,
        openHandler: @escaping () -> Void,
        reviewHandler: @escaping ([URL]) -> Void
    ) {
        self.snapshotStore = snapshotStore
        self.openHandler = openHandler
        self.reviewHandler = reviewHandler
    }

    static func store() -> OwnedSnapshotStore? { snapshotStore }
    static func open() { openHandler?() }
    static func review(_ urls: [URL]) { reviewHandler?(urls) }
}

struct OpenAddToCoreIntent: AppIntent {
    static let title: LocalizedStringResource = "Open Add to Core"
    static let description = IntentDescription(
        "Opens OmniVia Core's capture panel without reading the clipboard or Finder selection."
    )
    // Both surfaces are declared. `openAppWhenRun` is what macOS 13 and 14
    // understand; `supportedModes` states the same intent precisely where the
    // SDK has it. Neither raises this package's macOS 13 minimum.
    static let openAppWhenRun = true

    #if compiler(>=6.2)
    @available(macOS 26.0, *)
    static var supportedModes: IntentModes { .foreground(.immediate) }
    #endif

    func perform() async throws -> some IntentResult & ProvidesDialog {
        await MainActor.run { ShortcutIntakeRuntime.open() }
        return .result(dialog: "Add to Core is open.")
    }
}

struct ReviewFilesInOmniViaCoreIntent: AppIntent {
    static let title: LocalizedStringResource = "Review Files in OmniVia Core"
    static let description = IntentDescription(
        "Copies up to 10 supplied files into protected temporary storage and opens them for review. Nothing is added until you confirm in OmniVia Core."
    )
    static let openAppWhenRun = true

    #if compiler(>=6.2)
    @available(macOS 26.0, *)
    static var supportedModes: IntentModes { .foreground(.immediate) }
    #endif

    @Parameter(title: "Files", description: "Files to copy and review")
    var files: [IntentFile]

    static var parameterSummary: some ParameterSummary {
        Summary("Review \(\.$files) in OmniVia Core")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard !files.isEmpty else {
            throw OwnedSnapshotStoreError.emptySelection
        }
        guard files.count <= OwnedSnapshotStore.maximumItems else {
            throw OwnedSnapshotStoreError.tooManyItems
        }
        guard let store = await MainActor.run(body: { ShortcutIntakeRuntime.store() }) else {
            throw ShortcutIntakeError.companionUnavailable
        }
        let inputs: [OwnedSnapshotInput] = files.map { file in
            if let fileURL = file.fileURL {
                return .file(fileURL, suggestedFilename: file.filename)
            }
            return .data(file.data, suggestedFilename: file.filename)
        }
        let urls = try store.stage(inputs)
        await MainActor.run { ShortcutIntakeRuntime.review(urls) }
        return .result(dialog: "Ready to review in OmniVia Core. Nothing has been added yet.")
    }
}

enum ShortcutIntakeError: Error {
    case companionUnavailable
}

struct OmniViaCoreShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenAddToCoreIntent(),
            phrases: [
                "Open Add to Core in \(.applicationName)",
                "Open \(.applicationName) capture",
            ],
            shortTitle: "Open Add to Core",
            systemImageName: "tray.and.arrow.down"
        )
        AppShortcut(
            intent: ReviewFilesInOmniViaCoreIntent(),
            phrases: [
                "Review files in \(.applicationName)",
                "Add files with \(.applicationName)",
            ],
            shortTitle: "Review Files",
            systemImageName: "doc.on.doc"
        )
    }

    static var shortcutTileColor: ShortcutTileColor { .orange }
}
