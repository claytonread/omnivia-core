import Foundation

/// The exact `NSOpenPanel` configuration a native picker must use. Held as
/// plain values so the policy is proven without opening a panel.
public struct CapturePickerPolicy: Equatable, Sendable {
    public let canChooseFiles: Bool
    public let canChooseDirectories: Bool
    public let allowsMultipleSelection: Bool
    public let treatsFilePackagesAsDirectories: Bool
    public let resolvesAliases: Bool

    /// Ordinary files: many files, never a directory. A package stays one file
    /// and an alias is taken as the alias, not as what it points at.
    public static let files = CapturePickerPolicy(
        canChooseFiles: true,
        canChooseDirectories: false,
        allowsMultipleSelection: true,
        treatsFilePackagesAsDirectories: false,
        resolvesAliases: false
    )

    /// One folder, which is then reviewed for scope on its own.
    public static let folder = CapturePickerPolicy(
        canChooseFiles: false,
        canChooseDirectories: true,
        allowsMultipleSelection: false,
        treatsFilePackagesAsDirectories: false,
        resolvesAliases: false
    )
}

/// Why a native surface refused one selection before it reached review.
public enum CaptureIntakeRefusal: Equatable, Sendable {
    case notReady
    case emptySelection
    case tooManySelected

    /// The fixed sentence the refusing surface shows. These never echo a path,
    /// a filename or any other word the sender supplied.
    public var message: String {
        switch self {
        case .notReady:
            "OmniVia Core cannot review files yet."
        case .emptySelection, .tooManySelected:
            "Choose up to \(CaptureIntakePolicy.maximumSelectionItems) files or folders."
        }
    }
}

/// What every native intake surface — Finder Service, status-item drop, native
/// picker — admits before anything is read.
public enum CaptureIntakePolicy {
    /// One selection never exceeds what one review draft can hold, so no
    /// surface admits a selection that review would then have to refuse whole.
    public static let maximumSelectionItems = CaptureDraft.maximumOrdinaryItems

    /// Admits one incoming selection on its declared count and the companion's
    /// own readiness. Nothing here opens a file or reads a pasteboard, and no
    /// surface registers before readiness is true.
    public static func admit(itemCount: Int, isReady: Bool) -> CaptureIntakeRefusal? {
        guard isReady else { return .notReady }
        guard itemCount > 0 else { return .emptySelection }
        guard itemCount <= maximumSelectionItems else { return .tooManySelected }
        return nil
    }
}
