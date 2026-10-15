import DocumentIntakeMacOS
import Foundation

/// Which surface of the management window a row belongs to.
public enum FilesAndFoldersSegment: Int, CaseIterable, Sendable {
    case files
    case folders
    case recentAdditions

    public var title: String {
        switch self {
        case .files: "Files"
        case .folders: "Folders"
        case .recentAdditions: "Recent additions"
        }
    }

    /// The activity column's heading. Each segment reports a different durable
    /// fact there, so none of them is labelled as one shared "last checked".
    public var activityColumnTitle: String {
        switch self {
        case .files: "Last checked"
        case .folders: "Last metadata scan"
        case .recentAdditions: "Per-file progress"
        }
    }
}

public enum FilesAndFoldersStatusFilter: Int, CaseIterable, Sendable {
    case all
    case activeOrPending
    case paused
    case needsAttention
    case stoppedOrComplete

    public var title: String {
        switch self {
        case .all: "All"
        case .activeOrPending: "Active or pending"
        case .paused: "Paused"
        case .needsAttention: "Needs attention"
        case .stoppedOrComplete: "Stopped or complete"
        }
    }

    public static func applicable(to segment: FilesAndFoldersSegment) -> [FilesAndFoldersStatusFilter] {
        switch segment {
        // Every segment's state space reaches all four buckets: a file or folder
        // subscription can be pending, paused, in review or stopped, and a bulk
        // run can be adding, paused, needing attention or finished. So the list
        // does not vary, and a chosen filter stays valid across a segment change.
        case .files, .folders, .recentAdditions: allCases
        }
    }
}

/// A root group that was added once and is retained only so the files it already
/// added stay linked. It is not a watch: nothing discovers new members through it.
public struct RetainedRootGroup: Equatable, Sendable {
    public let rootRef: String
    public let displayName: String
    public let memberCount: Int
    public let membersFrozen: Bool

    public init(rootRef: String, displayName: String, memberCount: Int, membersFrozen: Bool) {
        self.rootRef = rootRef
        self.displayName = displayName
        self.memberCount = memberCount
        self.membersFrozen = membersFrozen
    }
}

public struct DetailFact: Equatable, Sendable {
    public let label: String
    public let value: String

    public init(label: String, value: String) {
        self.label = label
        self.value = value
    }
}

/// The enablement and titles of the three actions, for exactly one selection.
public struct FilesAndFoldersActions: Equatable, Sendable {
    public let pauseTitle: String
    public let pauseEnabled: Bool
    public let stopTitle: String
    public let stopEnabled: Bool
    public let checkEnabled: Bool
}

public enum FilesAndFoldersItem: Equatable, Sendable {
    case file(FileSubscription)
    case folder(FolderSubscription)
    case retainedRoot(RetainedRootGroup)
    case bulkRun(FolderBulkRun)

    public var segment: FilesAndFoldersSegment {
        switch self {
        case .file: .files
        case .folder, .retainedRoot: .folders
        case .bulkRun: .recentAdditions
        }
    }

    public var identifier: String {
        switch self {
        case let .file(value): value.subscriptionID
        case let .folder(value): value.subscriptionID
        case let .retainedRoot(value): value.rootRef
        case let .bulkRun(value): value.runID
        }
    }

    public var displayName: String {
        switch self {
        case let .file(value): value.displayName
        case let .folder(value): value.rootDisplayName
        case let .retainedRoot(value): value.displayName
        case let .bulkRun(value): value.rootDisplayName
        }
    }

    /// Every stable identifier a search may name, including the root a folder
    /// subscription or a run is bound to.
    public var searchableIdentifiers: [String] {
        switch self {
        case let .file(value): [value.subscriptionID, value.requestedSourceIdentity]
        case let .folder(value): [value.subscriptionID, value.rootRef]
        case let .retainedRoot(value): [value.rootRef]
        case let .bulkRun(value): [value.runID, value.rootRef]
        }
    }

    public var statusLabel: String {
        switch self {
        case let .file(value): Self.label(value.state)
        case let .folder(value): Self.label(value.state)
        case .retainedRoot: "Added once"
        case let .bulkRun(value): Self.label(value.state)
        }
    }

    public var statusFilter: FilesAndFoldersStatusFilter {
        switch self {
        case let .file(value): Self.bucket(value.state)
        case let .folder(value): Self.bucket(value.state)
        case .retainedRoot: .stoppedOrComplete
        case let .bulkRun(value): Self.bucket(value.state)
        }
    }

    /// The count column: enrolled members, or a run's added-of-planned.
    public var countText: String {
        switch self {
        case .file: "1"
        case let .folder(value): String(value.members.count)
        case let .retainedRoot(value): String(value.memberCount)
        case let .bulkRun(value): "\(value.admittedCount)/\(value.members.count)"
        }
    }

    public var activityText: String {
        switch self {
        case let .file(value): value.lastCheckedAt ?? "Not checked yet"
        case let .folder(value):
            value.reviewChangeCount.map { "\($0) changes require review" }
                ?? value.lastCompletedMetadataScanAt
                ?? "Not scanned yet"
        case .retainedRoot: "Not kept updated"
        case let .bulkRun(value):
            // Separate per-file facts, never one whole-folder success.
            "\(value.admittedCount) added · \(value.publishedCount) in progress · "
                + "\(value.plannedCount) waiting · \(value.stoppedCount) stopped · "
                + "\(value.attentionCount) needs attention"
                + (Self.isTerminal(value.state) ? " · links retained for manual refresh" : "")
        }
    }

    private static func bucket(_ state: FolderSubscriptionState) -> FilesAndFoldersStatusFilter {
        switch state {
        case .waitingForCore, .active: .activeOrPending
        case .paused: .paused
        case .reviewRequired, .needsAccess: .needsAttention
        case .stopped: .stoppedOrComplete
        }
    }

    private static func bucket(_ state: FolderBulkRunState) -> FilesAndFoldersStatusFilter {
        switch state {
        case .adding: .activeOrPending
        case .paused: .paused
        case .needsAttention: .needsAttention
        case .complete, .stopped: .stoppedOrComplete
        }
    }

    static func label(_ state: FolderSubscriptionState) -> String {
        switch state {
        case .waitingForCore: "Waiting for Core"
        case .active: "Watching"
        case .paused: "Paused"
        case .reviewRequired: "Review required"
        case .needsAccess: "Needs access"
        case .stopped: "Stopped"
        }
    }

    static func label(_ state: FolderBulkRunState) -> String {
        switch state {
        case .adding: "Adding"
        case .paused: "Paused"
        case .complete: "Added"
        case .needsAttention: "Needs attention"
        case .stopped: "Stopped"
        }
    }

    static func isTerminal(_ state: FolderBulkRunState) -> Bool {
        state == .complete || state == .stopped
    }
}

/// Projects the durable native stores onto the management window's three
/// surfaces. It states only what those stores record: nothing here invents a
/// capture, extraction or profile history that this side does not hold.
public struct FilesAndFoldersModel: Sendable {
    /// What Core is told about where captures land, as the companion was
    /// configured — not a guess made from a row.
    public let workspaceName: String
    public let files: [FileSubscription]
    public let folders: [FolderSubscription]
    public let retainedRoots: [RetainedRootGroup]
    public let runs: [FolderBulkRun]

    public init(
        workspaceName: String,
        files: [FileSubscription] = [],
        folders: [FolderSubscription] = [],
        retainedRoots: [RetainedRootGroup] = [],
        runs: [FolderBulkRun] = []
    ) {
        self.workspaceName = workspaceName
        self.files = files
        self.folders = folders
        self.retainedRoots = retainedRoots
        self.runs = runs
    }

    /// Retained one-off root groups that no folder subscription represents. A
    /// group is listed only when a durable run names it: the root grant itself
    /// records no display name, and inventing one would be a guess.
    public static func retainedRoots(
        groups: [NativeRootAccessDescriptor],
        runs: [FolderBulkRun],
        folders: [FolderSubscription]
    ) -> [RetainedRootGroup] {
        let watched = Set(folders.map(\.rootRef))
        var names: [String: String] = [:]
        for run in runs.sorted(by: { $0.createdAt < $1.createdAt }) {
            names[run.rootRef] = run.rootDisplayName
        }
        return groups.compactMap { group in
            guard group.authority != .watchAndMembers,
                  group.state.retainsRoute,
                  !watched.contains(group.rootRef),
                  let name = names[group.rootRef]
            else { return nil }
            return RetainedRootGroup(
                rootRef: group.rootRef,
                displayName: name,
                memberCount: group.memberRefs.count,
                membersFrozen: group.membersFrozen
            )
        }
    }

    public func items(
        segment: FilesAndFoldersSegment,
        search: String = "",
        statusFilter: FilesAndFoldersStatusFilter = .all
    ) -> [FilesAndFoldersItem] {
        let unfiltered: [FilesAndFoldersItem]
        switch segment {
        case .files:
            unfiltered = Self.byName(files.map(FilesAndFoldersItem.file))
        case .folders:
            unfiltered = Self.byName(
                folders.map(FilesAndFoldersItem.folder)
                    + retainedRoots.map(FilesAndFoldersItem.retainedRoot)
            )
        case .recentAdditions:
            // Most recent first: the surface is about what was just added.
            unfiltered = runs.sorted {
                $0.createdAt == $1.createdAt ? $0.runID > $1.runID : $0.createdAt > $1.createdAt
            }.map(FilesAndFoldersItem.bulkRun)
        }
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return unfiltered.filter { item in
            (statusFilter == .all || item.statusFilter == statusFilter)
                && (query.isEmpty || Self.matches(item, query: query))
        }
    }

    public func summary(items: [FilesAndFoldersItem], segment: FilesAndFoldersSegment) -> String {
        guard !items.isEmpty else {
            switch segment {
            case .files: return "No files are listed."
            case .folders: return "No folders are listed."
            case .recentAdditions: return "No folder additions are recorded."
            }
        }
        let active = items.filter { $0.statusFilter == .activeOrPending }.count
        return "\(active) active or pending · \(items.count) listed · workspace \(workspaceName)"
    }

    /// Only facts the native stores durably hold. Where this side has no record —
    /// capture, extraction and profile history — the detail says where that lives
    /// instead of printing a timestamp or status it cannot know.
    public func detail(for item: FilesAndFoldersItem?) -> [DetailFact] {
        guard let item else { return [] }
        switch item {
        case let .file(value): return fileDetail(value)
        case let .folder(value): return folderDetail(value)
        case let .retainedRoot(value): return retainedRootDetail(value)
        case let .bulkRun(value): return bulkRunDetail(value)
        }
    }

    public static func actions(for item: FilesAndFoldersItem?) -> FilesAndFoldersActions {
        guard let item else {
            return FilesAndFoldersActions(
                pauseTitle: "Pause",
                pauseEnabled: false,
                stopTitle: "Stop updating",
                stopEnabled: false,
                checkEnabled: false
            )
        }
        switch item {
        case let .file(value):
            return subscriptionActions(value.state, resumable: [.paused, .needsAccess])
        case let .folder(value):
            return subscriptionActions(
                value.state,
                resumable: [.paused, .needsAccess, .reviewRequired]
            )
        case .retainedRoot:
            // Nothing watches this root, so there is no update to pause, stop or
            // check. The detail area says so rather than leaving dead buttons
            // unexplained.
            return FilesAndFoldersActions(
                pauseTitle: "Pause",
                pauseEnabled: false,
                stopTitle: "Stop updating",
                stopEnabled: false,
                checkEnabled: false
            )
        case let .bulkRun(value):
            // Resuming is not retrying: a member that already failed keeps its
            // own outcome until Retry unavailable files is chosen for it.
            return FilesAndFoldersActions(
                pauseTitle: value.state == .paused
                    ? "Resume"
                    : value.attentionCount > 0 ? "Retry unavailable files" : "Pause",
                pauseEnabled: [.adding, .paused, .needsAttention].contains(value.state),
                stopTitle: "Stop adding remaining files",
                stopEnabled: value.plannedCount > 0,
                checkEnabled: value.state == .adding
            )
        }
    }

    private static func subscriptionActions(
        _ state: FolderSubscriptionState,
        resumable: Set<FolderSubscriptionState>
    ) -> FilesAndFoldersActions {
        FilesAndFoldersActions(
            pauseTitle: state == .reviewRequired
                ? "Continue changes"
                : resumable.contains(state) ? "Resume" : "Pause",
            pauseEnabled: [.active, .paused, .needsAccess, .reviewRequired].contains(state),
            stopTitle: "Stop updating",
            stopEnabled: state != .stopped,
            checkEnabled: state == .active
        )
    }

    private func fileDetail(_ value: FileSubscription) -> [DetailFact] {
        var facts = [
            DetailFact(label: "File", value: value.displayName),
            DetailFact(label: "Subscription", value: value.subscriptionID),
            DetailFact(label: "Source identity", value: value.requestedSourceIdentity),
            DetailFact(label: "Destination workspace", value: value.workspaceRef),
            DetailFact(
                label: "Updates",
                value: value.state == .stopped
                    ? "Stopped. This file is no longer kept updated."
                    : "Kept updated when this file changes."
            ),
            DetailFact(label: "Status", value: FilesAndFoldersItem.label(value.state)),
            DetailFact(label: "Last checked", value: value.lastCheckedAt ?? "Not checked yet"),
            DetailFact(
                label: "Last change seen",
                value: value.lastObservedAt ?? "No change recorded yet"
            ),
        ]
        if value.state == .stopped {
            facts.append(Self.retainedLinksFact)
        }
        facts.append(Self.coreHistoryFact)
        return facts
    }

    private func folderDetail(_ value: FolderSubscription) -> [DetailFact] {
        let present = value.members.filter { $0.state == .present }.count
        var facts = [
            DetailFact(label: "Folder", value: value.rootDisplayName),
            DetailFact(label: "Subscription", value: value.subscriptionID),
            DetailFact(label: "Root", value: value.rootRef),
            DetailFact(label: "Destination workspace", value: value.workspaceRef),
            DetailFact(
                label: "Scope",
                value: (value.recursive ? "This folder and its subfolders" : "This folder only")
                    + (value.includeHidden ? ", including hidden items" : "")
            ),
            DetailFact(
                label: "Updates",
                value: value.state == .stopped
                    ? "Stopped. This folder is no longer kept updated."
                    : "Kept updated as its files change."
            ),
            DetailFact(label: "Status", value: FilesAndFoldersItem.label(value.state)),
            DetailFact(
                label: "Files enrolled",
                value: "\(value.members.count) (\(present) present)"
            ),
            DetailFact(
                label: "Last metadata scan",
                value: value.lastCompletedMetadataScanAt ?? "Not scanned yet"
            ),
            DetailFact(
                label: "Unusual changes to review",
                value: value.reviewChangeCount.map(String.init) ?? "None recorded"
            ),
        ]
        if value.state == .stopped {
            facts.append(Self.retainedLinksFact)
        }
        facts.append(Self.coreHistoryFact)
        return facts
    }

    private func retainedRootDetail(_ value: RetainedRootGroup) -> [DetailFact] {
        [
            DetailFact(label: "Folder", value: value.displayName),
            DetailFact(label: "Root", value: value.rootRef),
            DetailFact(label: "Updates", value: "Added once. This folder is not kept updated."),
            DetailFact(
                label: "Files in this group",
                value: "\(value.memberCount)"
                    + (value.membersFrozen ? " (membership frozen)" : "")
            ),
            Self.retainedLinksFact,
            DetailFact(
                label: "Actions",
                value: "Updating actions do not apply: nothing watches this folder."
            ),
            Self.coreHistoryFact,
        ]
    }

    private func bulkRunDetail(_ value: FolderBulkRun) -> [DetailFact] {
        var facts = [
            DetailFact(label: "Folder", value: value.rootDisplayName),
            DetailFact(label: "Run", value: value.runID),
            DetailFact(label: "Root", value: value.rootRef),
            DetailFact(label: "Destination workspace", value: value.workspaceRef),
            DetailFact(label: "Started", value: value.createdAt),
            DetailFact(label: "Status", value: FilesAndFoldersItem.label(value.state)),
            DetailFact(label: "Files in this run", value: String(value.members.count)),
            DetailFact(
                label: "Progress",
                value: "\(value.admittedCount) added, \(value.publishedCount) in progress, "
                    + "\(value.plannedCount) waiting, \(value.stoppedCount) stopped, "
                    + "\(value.attentionCount) need attention"
            ),
            // The ledger records a per-file outcome and nothing else: a run that
            // mostly succeeded is not a folder that succeeded.
            DetailFact(
                label: "Whole-folder result",
                value: "Not recorded. Each file carries its own outcome."
            ),
            DetailFact(
                label: "Updates",
                value: value.subscriptionID == nil
                    ? "Added once. This run establishes no watch."
                    : "This folder is also kept updated as a folder subscription."
            ),
        ]
        if FilesAndFoldersItem.isTerminal(value.state) {
            facts.append(Self.retainedLinksFact)
        }
        facts.append(Self.coreHistoryFact)
        return facts
    }

    private static let retainedLinksFact = DetailFact(
        label: "Already added files",
        value: "Still in Core and still linked, so they can be refreshed manually."
    )

    /// This side holds no capture, extraction or profile history, so the detail
    /// names where that history lives rather than inventing one.
    private static let coreHistoryFact = DetailFact(
        label: "Capture history",
        value: "Capture, extraction and profile history is available from Core after admission."
    )

    private static func matches(_ item: FilesAndFoldersItem, query: String) -> Bool {
        ([item.displayName] + item.searchableIdentifiers).contains {
            $0.localizedCaseInsensitiveContains(query)
        }
    }

    private static func byName(_ items: [FilesAndFoldersItem]) -> [FilesAndFoldersItem] {
        items.sorted {
            let order = $0.displayName.localizedCaseInsensitiveCompare($1.displayName)
            return order == .orderedSame ? $0.identifier < $1.identifier : order == .orderedAscending
        }
    }
}
