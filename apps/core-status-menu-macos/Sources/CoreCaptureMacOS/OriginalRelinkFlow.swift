import AppKit
import DocumentIntakeMacOS

extension CapturePickerPolicy {
    /// The one place a policy becomes an `NSOpenPanel`, so no native picker can drift from it.
    @MainActor
    func openPanel() -> NSOpenPanel {
        let panel = NSOpenPanel()
        panel.canChooseFiles = canChooseFiles
        panel.canChooseDirectories = canChooseDirectories
        panel.allowsMultipleSelection = allowsMultipleSelection
        panel.treatsFilePackagesAsDirectories = treatsFilePackagesAsDirectories
        panel.resolvesAliases = resolvesAliases
        return panel
    }
}

/// What the person decides about a staged Locate candidate.
public enum OriginalRelinkDecision: Equatable, Sendable {
    /// Cancel is the safe default and the first button.
    case cancel
    case useThisFile
    /// Add the chosen file as its own document instead of moving the named one.
    case addSeparately
}

/// What the person is shown before anything is reserved (design §9.5): the existing
/// document's label, the selected file and the comparison result.
public struct OriginalRelinkPrompt: Equatable, Sendable {
    public let documentLabel: String
    public let selectedFileName: String
    public let comparison: OriginalRelinkComparison

    public var title: String { "Use “\(selectedFileName)” as the original of “\(documentLabel)”?" }

    /// The comparison sentence is the design's own. A changed hash is stated as a difference
    /// and never as proof that the file is unrelated.
    public var comparisonText: String {
        switch comparison {
        case .identical:
            "This file is identical to the last captured version."
        case .differs:
            "This file differs from the last captured version. A different hash does not "
                + "prove it is unrelated."
        case .noCapturedVersion:
            "Core has not reported a last captured version, so this file could not be compared."
        }
    }

    public var informativeText: String {
        comparisonText + "\n\nMoving the original changes where future reads come from. Captured "
            + "versions are not changed, the selected file is not captured now, and updates "
            + "stay paused until you resume them."
    }

    /// Offered where the file may really be a different document.
    public var offersAddSeparately: Bool { comparison != .identical }
}

/// The result of one Locate, for the caller and for tests: what happened and nothing else.
public enum OriginalRelinkResult: Equatable, Sendable {
    case noFileChosen
    case refused(OriginalRelinkError)
    case cancelled
    case addedSeparately
    /// The generation is reserved, the route is paused and Core is owed the relink.
    case reserved(accessRef: String, generation: UInt64)
    case failed
}

/// Design §9.5 as one user action on the Files tab: the native picker stages one candidate,
/// the exact comparison and an explicit confirmation follow, and only then is a new binding
/// generation reserved and a recoverable relink intent recorded for Core.
///
/// The picker and the prompt are the only things a test replaces; staging, the comparison,
/// the reservation and the pause are the production environment's. Nothing here reads the
/// old file, activates the candidate or contacts Core: the reservation is what makes the old
/// generation unable to publish, and `OriginalRelinkReconciler` is what activates the new
/// grant after Core's confirmation.
@MainActor
public final class OriginalRelinkFlow {
    private let environment: DocumentIntakeEnvironment
    private let fallback: CaptureDeliveryBinding
    private let chooseFile: () -> URL?
    private let decide: (OriginalRelinkPrompt) -> OriginalRelinkDecision
    private let refuse: (String) -> Void
    private let addSeparately: (URL) -> Void
    private let onReserved: () -> Void

    /// `chooseFile` and `decide` default to the native picker and alert. `fallback` is the
    /// companion's own binding, used only for a route with no queued work or watch to name
    /// the workspace it was frozen under. `onReserved` wakes delivery so Core is told now
    /// rather than at the next pass.
    public init(
        environment: DocumentIntakeEnvironment,
        fallback: CaptureDeliveryBinding,
        chooseFile: (() -> URL?)? = nil,
        decide: ((OriginalRelinkPrompt) -> OriginalRelinkDecision)? = nil,
        refuse: ((String) -> Void)? = nil,
        addSeparately: @escaping (URL) -> Void,
        onReserved: @escaping () -> Void
    ) {
        self.environment = environment
        self.fallback = fallback
        self.chooseFile = chooseFile ?? {
            let panel = CapturePickerPolicy.relinkFile.openPanel()
            panel.message = "Choose the file that is now the original."
            return panel.runModal() == .OK ? panel.url : nil
        }
        self.decide = decide ?? { prompt in
            let alert = NSAlert()
            alert.messageText = prompt.title
            alert.informativeText = prompt.informativeText
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Use this file")
            if prompt.offersAddSeparately { alert.addButton(withTitle: "Add separately") }
            switch alert.runModal() {
            case .alertSecondButtonReturn: return .useThisFile
            case .alertThirdButtonReturn: return .addSeparately
            default: return .cancel
            }
        }
        self.refuse = refuse ?? { message in
            let alert = NSAlert()
            alert.messageText = "Could not use that file"
            alert.informativeText = message
            alert.runModal()
        }
        self.addSeparately = addSeparately
        self.onReserved = onReserved
    }

    /// The last version Core reported capturing: the one it calls current, else the newest.
    nonisolated static func baseline(from history: CoreDocumentHistory?) -> OriginalRelinkBaseline? {
        guard let history,
              let capture = history.captures.first(where: \.isCurrentSource)
                  ?? history.captures.max(by: { $0.captureSequence < $1.captureSequence })
        else { return nil }
        return OriginalRelinkBaseline(
            sourceDigest: capture.sourceDigest,
            sourceLengthBytes: capture.sourceLengthBytes
        )
    }

    /// The picker and the alert run on the main actor; reading the chosen file to compare it
    /// does not (design §9.3: I/O stays off the UI thread).
    @discardableResult
    public func locate(
        _ file: FileSubscription,
        history: CoreDocumentHistory?
    ) async -> OriginalRelinkResult {
        guard let url = chooseFile() else { return .noFileChosen }
        let environment = self.environment
        let baseline = Self.baseline(from: history)
        let staged = await Task.detached(priority: .userInitiated) {
            Result { try environment.stageRelink(for: file, selectedURL: url, baseline: baseline) }
        }.value
        let candidate: OriginalRelinkCandidate
        do {
            candidate = try staged.get()
        } catch let error as OriginalRelinkError {
            refuse(Self.message(for: error))
            return .refused(error)
        } catch {
            refuse(Self.message(for: .unreadable))
            return .refused(.unreadable)
        }
        let prompt = OriginalRelinkPrompt(
            documentLabel: file.displayName,
            selectedFileName: candidate.fileName,
            comparison: candidate.comparison
        )
        switch decide(prompt) {
        case .cancel:
            return .cancelled
        case .addSeparately:
            guard prompt.offersAddSeparately else { return .cancelled }
            addSeparately(url)
            return .addedSeparately
        case .useThisFile:
            do {
                let intent = try environment.relinkSource(candidate: candidate, fallback: fallback)
                onReserved()
                return .reserved(accessRef: intent.accessRef, generation: intent.generation)
            } catch {
                refuse("The file could not be linked. Nothing changed.")
                return .failed
            }
        }
    }

    nonisolated static func message(for error: OriginalRelinkError) -> String {
        switch error {
        case .notRelinkable:
            "This file cannot be relocated right now. Nothing changed."
        case .unsupportedType:
            "That is not the kind of file this one was added as. Choose a file of the same kind."
        case .unreadable:
            "That file could not be read to compare it. Nothing changed."
        }
    }
}
