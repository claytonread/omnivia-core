import Foundation

/// What one independently confirmed group of a mixed selection came to.
///
/// An ordinary-file group, an import-once folder and a keep-updated folder are
/// each reviewed, confirmed and published on their own, so each ends in its own
/// receipt. A receipt speaks only for its group: there is no whole-selection
/// result, and one group's cancellation or failure says nothing about another.
public struct CaptureGroupReceipt: Equatable, Identifiable {
    public enum Kind: Equatable {
        case ordinaryFiles
        case folderImportOnce
        case folderKeepUpdated
    }

    public enum Outcome: Equatable {
        /// Handed to the durable queue. `queued` counts the items published so
        /// far; a folder continues in bounded batches after this receipt.
        case queued(queued: Int)
        /// The person declined at this group's own confirmation. Nothing from
        /// this group was added and its review row, if any, is still in review.
        case cancelled
        case failed
    }

    /// Minted when the group's confirmation starts; names this group alone.
    public let id: String
    public let kind: Kind
    public let title: String
    /// The durable submission this group created, when it created one. A
    /// cancelled or failed group names none.
    public let submissionID: String?
    public let outcome: Outcome
    /// One sentence about this group only.
    public let detail: String

    static func newID() -> String { "group-" + UUID().uuidString.lowercased() }

    public var summary: String { "\(title): \(detail)" }
}
