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
    /// Recent additions only: an addition that is not finished. An item Core
    /// has accepted for processing belongs here, because acceptance is not a
    /// searchable publication or a terminal receipt and this Mac will never see
    /// one arrive. A source subscription never reports this filter, because
    /// "pending" there means waiting for Core rather than an unfinished
    /// addition.
    case pending

    public var title: String {
        switch self {
        case .all: "All"
        case .activeOrPending: "Active or pending"
        case .paused: "Paused"
        case .needsAttention: "Needs attention"
        case .stoppedOrComplete: "Stopped or complete"
        case .pending: "Pending"
        }
    }

    public static func applicable(to segment: FilesAndFoldersSegment) -> [FilesAndFoldersStatusFilter] {
        switch segment {
        // A file or folder subscription can be pending, paused, in review or
        // stopped, so those four buckets plus All are offered on both tabs and a
        // chosen filter stays valid when the tab changes between them.
        case .files, .folders: [.all, .activeOrPending, .paused, .needsAttention, .stoppedOrComplete]
        // Recent additions is an activity and recovery utility, not a source
        // list: it has nothing to pause and no complete whole-addition result to
        // filter on, so it offers exactly All, Pending and Needs attention.
        case .recentAdditions: [.all, .pending, .needsAttention]
        }
    }
}

/// Whether the local activity this surface is showing is the current durable
/// record.
///
/// The activity store is the only writer of its own file, and recording is how
/// this surface learns what the queue and run ledgers now prove. When that
/// write or read fails there is no current answer: whatever the process is
/// holding may already be out of date, and nothing observed since reached local
/// storage. The surface says so and offers no action over those facts rather
/// than presenting them as current.
public enum CaptureActivityAvailability: Equatable, Sendable {
    /// The durable owners were read and what they prove was persisted.
    case current
    /// The current facts could not be persisted or re-read. What is listed is
    /// the last record this process read, which may be stale.
    case unavailable

    public var isCurrent: Bool { self == .current }
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

/// One Recent additions row: either one frozen folder run or one ordinary
/// submission, with every member's own outcome preserved. The group states
/// per-item facts and never a single whole-addition success.
public struct CaptureActivityGroup: Equatable, Sendable {
    /// The run id, or the submission id for an ordinary addition.
    public let identifier: String
    public let displayName: String
    /// The frozen run this group projects, when it is a folder run. Its state is
    /// what the run authority acts on; a group whose manifest is gone has none,
    /// and therefore offers no run action.
    public let run: FolderBulkRun?
    /// Members in the order they were first observed.
    public let items: [CaptureActivityRecord]

    public init(
        identifier: String,
        displayName: String,
        run: FolderBulkRun? = nil,
        items: [CaptureActivityRecord]
    ) {
        self.identifier = identifier
        self.displayName = displayName
        self.run = run
        self.items = items
    }

    public func count(_ fact: CaptureActivityFact) -> Int {
        items.filter { $0.fact == fact }.count
    }

    /// Items that are not receipts: nothing about them can be cleared or
    /// pruned, including an item Core has only accepted for processing, an
    /// unknown outcome and a folder member its run can still retry.
    public var pendingCount: Int { items.filter { !$0.isReceipt }.count }
    public var attentionCount: Int { items.filter { $0.fact.needsAttention }.count }
    public var receiptCount: Int { items.filter(\.isReceipt).count }
    /// The newest member, which is what orders the surface.
    public var latestSequence: UInt64 { items.map(\.sequence).max() ?? 0 }
}

public struct DetailFact: Equatable, Sendable {
    public let label: String
    public let value: String

    public init(label: String, value: String) {
        self.label = label
        self.value = value
    }
}

/// What this surface currently knows about one document's canonical Core history.
///
/// Four states and no fifth: a read that has not happened, one in flight, an answer, and
/// a last answer that a later read could not refresh. `stale` exists so a detail pane can
/// keep showing what Core last said while saying plainly that it may have moved on --
/// which is a different claim from `current`, and neither is `unavailable`.
public enum CoreHistoryAvailability: Equatable, Sendable {
    case notRequested
    case loading
    case current(CoreDocumentHistory)
    case stale(CoreDocumentHistory)
    case unavailable(CoreHistoryError)

    /// The history itself, when there is one to show at all.
    public var history: CoreDocumentHistory? {
        switch self {
        case let .current(value), let .stale(value): value
        case .notRequested, .loading, .unavailable: nil
        }
    }

    /// Whether what is shown is the answer to the most recent read.
    public var isCurrent: Bool {
        if case .current = self { return true }
        return false
    }
}

/// One document's history and the document it is the history of.
///
/// The pairing is load-bearing: a detail pane holding the previous selection's history
/// must not render it against this one, so the document is compared before anything is
/// shown rather than assumed to match.
public struct CoreHistoryState: Equatable, Sendable {
    public let documentID: String?
    public let availability: CoreHistoryAvailability

    public static let notRequested = CoreHistoryState(
        documentID: nil, availability: .notRequested
    )

    public init(documentID: String?, availability: CoreHistoryAvailability) {
        self.documentID = documentID
        self.availability = availability
    }

    /// The history of exactly `documentID`, or nothing.
    public func availability(for documentID: String) -> CoreHistoryAvailability {
        self.documentID == documentID ? availability : .notRequested
    }
}

/// One confirmed reprocess, built only from values Core reported.
///
/// Every field here came out of a `document.history` answer: the capture Core reports as
/// retained and reprocessable, a profile selection from that capture's own approved list,
/// and the extraction head exactly as the read observed it -- including its absence. The
/// operation key is derived from those four, so a repeat of the same confirmed intent
/// replays rather than admitting a second extraction.
public struct CoreReprocessIntent: Equatable, Sendable {
    public let documentID: String
    public let capture: CoreSourceCapture
    public let profile: CoreReprocessProfile
    public let expectedBaseExtractionID: String?
    public let operationKey: String

    public init(
        documentID: String,
        capture: CoreSourceCapture,
        profile: CoreReprocessProfile,
        expectedBaseExtractionID: String?
    ) {
        self.documentID = documentID
        self.capture = capture
        self.profile = profile
        self.expectedBaseExtractionID = expectedBaseExtractionID
        operationKey = CoreDocumentHistoryReader.reprocessOperationKey(
            captureID: capture.captureID,
            profileRef: profile.profileRef,
            revision: profile.revision,
            expectedProfileDigest: profile.profileDigest,
            expectedBaseExtractionID: expectedBaseExtractionID
        )
    }

    /// The confirmation sentence. It names the exact capture and the exact profile, says
    /// that the original is not read, and says what the result can and cannot replace.
    public var confirmationMessage: String {
        "Capture \(capture.captureSequence) of this file, taken \(capture.capturedAt), will be "
            + "processed again with \(profile.title) (profile \(profile.profileRef) "
            + "revision \(profile.revision)).\n\nThe original file is not read and no new "
            + "capture is made. The current searchable version stays available until the new "
            + "result is verified, and if a newer version becomes current first this result is "
            + "kept as history only."
    }
}

/// The enablement and titles of the row actions, for exactly one selection.
public struct FilesAndFoldersActions: Equatable, Sendable {
    public let pauseTitle: String
    public let pauseEnabled: Bool
    public let stopTitle: String
    public let stopEnabled: Bool
    public let checkEnabled: Bool
    /// Reveals the frozen recovery identity of an item whose outcome is
    /// unknown. It reads durable state and allocates nothing.
    public let revealEnabled: Bool
    /// Reprocesses one retained capture Core reports as reprocessable. Enabled only when
    /// Core has reported that action available for a capture of the selected document --
    /// never from a stale read, and never as a guess about what Core would accept.
    public let reprocessEnabled: Bool

    public init(
        pauseTitle: String,
        pauseEnabled: Bool,
        stopTitle: String,
        stopEnabled: Bool,
        checkEnabled: Bool,
        revealEnabled: Bool = false,
        reprocessEnabled: Bool = false
    ) {
        self.pauseTitle = pauseTitle
        self.pauseEnabled = pauseEnabled
        self.stopTitle = stopTitle
        self.stopEnabled = stopEnabled
        self.checkEnabled = checkEnabled
        self.revealEnabled = revealEnabled
        self.reprocessEnabled = reprocessEnabled
    }
}

public enum FilesAndFoldersItem: Equatable, Sendable {
    case file(FileSubscription)
    case folder(FolderSubscription)
    case retainedRoot(RetainedRootGroup)
    case activity(CaptureActivityGroup)

    public var segment: FilesAndFoldersSegment {
        switch self {
        case .file: .files
        case .folder, .retainedRoot: .folders
        case .activity: .recentAdditions
        }
    }

    public var identifier: String {
        switch self {
        case let .file(value): value.subscriptionID
        case let .folder(value): value.subscriptionID
        case let .retainedRoot(value): value.rootRef
        case let .activity(value): value.identifier
        }
    }

    public var displayName: String {
        switch self {
        case let .file(value): value.displayName
        case let .folder(value): value.rootDisplayName
        case let .retainedRoot(value): value.displayName
        case let .activity(value): value.displayName
        }
    }

    /// Every stable identifier a search may name, including the root a folder
    /// subscription or a run is bound to and every frozen item identity an
    /// addition is recovered by.
    public var searchableIdentifiers: [String] {
        switch self {
        case let .file(value): [value.subscriptionID, value.requestedSourceIdentity]
        case let .folder(value): [value.subscriptionID, value.rootRef]
        case let .retainedRoot(value): [value.rootRef]
        case let .activity(value):
            [value.identifier] + (value.run.map { [$0.rootRef] } ?? []) + value.items.map(\.itemID)
        }
    }

    public var statusLabel: String {
        switch self {
        case let .file(value): Self.label(value.state)
        case let .folder(value): Self.label(value.state)
        case .retainedRoot: "Added once"
        case let .activity(value): Self.statusLabel(value)
        }
    }

    public var statusFilter: FilesAndFoldersStatusFilter {
        switch self {
        case let .file(value): Self.bucket(value.state)
        case let .folder(value): Self.bucket(value.state)
        case .retainedRoot: .stoppedOrComplete
        case let .activity(value): Self.bucket(value)
        }
    }

    /// The count column: enrolled members, or how many of an addition's items
    /// Core has accepted for processing. Accepted is not added.
    public var countText: String {
        switch self {
        case .file: "1"
        case let .folder(value): String(value.members.count)
        case let .retainedRoot(value): String(value.memberCount)
        case let .activity(value): "\(value.count(.processing))/\(value.items.count)"
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
        case let .activity(value): Self.progressText(value)
        }
    }

    /// The group's own status: the fact that most needs the user, then the
    /// earliest unfinished stage, then what Core owns, then stopped. It is never
    /// a claim that the addition as a whole succeeded.
    static func statusLabel(_ group: CaptureActivityGroup) -> String {
        let order: [CaptureActivityFact] =
            [.failed, .checking, .sending, .queued, .planned, .processing, .stopped]
        guard let fact = order.first(where: { group.count($0) > 0 }) else {
            return "No activity recorded"
        }
        return label(fact)
    }

    /// Separate per-file facts, in a fixed order, with nothing invented for a
    /// fact no item reports.
    static func progressText(_ group: CaptureActivityGroup) -> String {
        let order: [CaptureActivityFact] =
            [.processing, .sending, .queued, .planned, .checking, .failed, .stopped]
        let parts = order.compactMap { fact -> String? in
            let count = group.count(fact)
            return count > 0 ? phrase(fact, count: count) : nil
        }
        return parts.isEmpty ? "No activity recorded yet" : parts.joined(separator: " · ")
    }

    private static func phrase(_ fact: CaptureActivityFact, count: Int) -> String {
        switch fact {
        case .planned: "\(count) waiting to be prepared"
        case .queued: "\(count) queued on this Mac"
        case .sending: "\(count) sending to Core"
        case .checking: "\(count) checking status"
        case .processing: "\(count) accepted by Core for processing"
        case .failed: "\(count) need attention"
        case .stopped: "\(count) stopped"
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

    private static func bucket(_ group: CaptureActivityGroup) -> FilesAndFoldersStatusFilter {
        if group.attentionCount > 0 { return .needsAttention }
        if group.pendingCount > 0 { return .pending }
        return .stoppedOrComplete
    }

    /// What is proven about one item, in the shared product wording.
    static func label(_ fact: CaptureActivityFact) -> String {
        switch fact {
        case .planned: "Folder addition queued"
        case .queued: "Queued on this Mac"
        case .sending: "Sending to Core…"
        case .checking: "Checking status…"
        case .processing: "Processing in Core"
        case .failed: "Needs attention"
        case .stopped: "Stopped"
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
    /// The durable local activity record, exactly as the activity store holds it.
    public let activity: [CaptureActivityRecord]
    public let activityGroups: [CaptureActivityGroup]
    /// Whether that activity is the current durable record or the last thing
    /// this process managed to read.
    public let activityAvailability: CaptureActivityAvailability
    /// The canonical Core history of at most one document: the selected one. It is kept
    /// in its own field, never folded into `activity`, because the two answer different
    /// questions and one of them is not this Mac's to answer.
    public let coreHistory: CoreHistoryState

    public init(
        workspaceName: String,
        files: [FileSubscription] = [],
        folders: [FolderSubscription] = [],
        retainedRoots: [RetainedRootGroup] = [],
        runs: [FolderBulkRun] = [],
        activity: [CaptureActivityRecord] = [],
        activityAvailability: CaptureActivityAvailability = .current,
        coreHistory: CoreHistoryState = .notRequested
    ) {
        self.workspaceName = workspaceName
        self.files = files
        self.folders = folders
        self.retainedRoots = retainedRoots
        self.runs = runs
        self.activity = activity
        self.activityAvailability = activityAvailability
        self.coreHistory = coreHistory
        activityGroups = Self.activityGroups(records: activity, runs: runs)
    }

    /// The same projection with one new canonical history, so the window can advance a
    /// read without re-reading every durable store behind it.
    public func replacingCoreHistory(_ history: CoreHistoryState) -> FilesAndFoldersModel {
        FilesAndFoldersModel(
            workspaceName: workspaceName,
            files: files,
            folders: folders,
            retainedRoots: retainedRoots,
            runs: runs,
            activity: activity,
            activityAvailability: activityAvailability,
            coreHistory: history
        )
    }

    /// The canonical history to show for one selection, or `.notRequested` when the
    /// selection is not a linked file or the held history belongs to another document.
    public func coreHistoryAvailability(
        for item: FilesAndFoldersItem?
    ) -> CoreHistoryAvailability {
        guard case let .file(value)? = item else { return .notRequested }
        return coreHistory.availability(for: value.linkedDocumentID)
    }

    /// The retained captures the reprocess control may offer for one selection, newest
    /// first, and the extraction head to bind against.
    ///
    /// Empty from a stale or failed read, on purpose: reprocessing binds an exact
    /// extraction head, and a head this surface is no longer sure of is not one to bind.
    public func reprocessChoices(
        for item: FilesAndFoldersItem?
    ) -> (captures: [CoreSourceCapture], expectedBaseExtractionID: String?)? {
        guard case let .file(value)? = item,
              case let .current(history) = coreHistory.availability(for: value.linkedDocumentID),
              !history.reprocessableCaptures.isEmpty
        else { return nil }
        return (history.reprocessableCaptures, history.extraction.currentExtractionID)
    }

    /// Every (retained capture, approved processing) pair the reprocess control may offer
    /// for one selection, newest capture first.
    ///
    /// One flat list rather than two dependent choices: two can disagree, and a profile
    /// chosen against a capture the user changed afterwards would send a selection other
    /// than the one its label described. Every pair here is a capture Core reports as
    /// reprocessable paired with one of that capture's own approved selections.
    public func reprocessOptions(
        for item: FilesAndFoldersItem?
    ) -> [(capture: CoreSourceCapture, profile: CoreReprocessProfile)] {
        guard let choices = reprocessChoices(for: item) else { return [] }
        return choices.captures.flatMap { capture in
            capture.reprocessProfiles.map { (capture: capture, profile: $0) }
        }
    }

    /// How one pair reads in the chooser: the exact capture, whether it is still the
    /// current source, and the exact approved processing.
    public static func reprocessOptionTitle(
        capture: CoreSourceCapture,
        profile: CoreReprocessProfile
    ) -> String {
        "Capture \(capture.captureSequence) · \(capture.capturedAt) · "
            + (capture.isCurrentSource ? "current source" : "historical only")
            + " · \(profile.title) · profile \(profile.profileRef) "
            + "revision \(profile.revision)"
    }

    /// One confirmed reprocess for a selection, or `nil` when Core reports none available.
    public func reprocessIntent(
        for item: FilesAndFoldersItem?,
        captureID: String,
        profileDigest: String
    ) -> CoreReprocessIntent? {
        guard case let .file(value)? = item,
              let choices = reprocessChoices(for: item),
              let capture = choices.captures.first(where: { $0.captureID == captureID }),
              let profile = capture.reprocessProfiles.first(where: {
                  $0.profileDigest == profileDigest
              })
        else { return nil }
        return CoreReprocessIntent(
            documentID: value.linkedDocumentID,
            capture: capture,
            profile: profile,
            expectedBaseExtractionID: choices.expectedBaseExtractionID
        )
    }

    /// Activity that clearing would remove. Unresolved work is never counted:
    /// not an item Core has only accepted for processing, not an unknown
    /// outcome, and not a folder member its run can still retry.
    public var clearableActivityCount: Int { activity.filter(\.isReceipt).count }

    /// Whether an action over local activity may be offered at all. A surface
    /// that could not persist or refresh these facts offers none: clearing,
    /// retrying, stopping, advancing and revealing a recovery identity would all
    /// act on what this process happens to be holding.
    public var canMutateActivity: Bool { activityAvailability.isCurrent }

    /// Groups the durable activity record by the frozen run that owns an item,
    /// or by its submission when no run does. A run whose every item has been
    /// pruned or cleared is not listed: clearing a receipt actually clears it,
    /// and the run's own manifest and links are untouched either way.
    public static func activityGroups(
        records: [CaptureActivityRecord],
        runs: [FolderBulkRun]
    ) -> [CaptureActivityGroup] {
        let runsByID = Dictionary(runs.map { ($0.runID, $0) }, uniquingKeysWith: { first, _ in first })
        var order: [String] = []
        var grouped: [String: [CaptureActivityRecord]] = [:]
        for record in records.sorted(by: { $0.sequence < $1.sequence }) {
            let key = record.runID ?? record.submissionID
            if grouped[key] == nil { order.append(key) }
            grouped[key, default: []].append(record)
        }
        return order.map { key in
            let items = grouped[key] ?? []
            let run = runsByID[key]
            return CaptureActivityGroup(
                identifier: key,
                displayName: run?.rootDisplayName ?? Self.submissionName(items),
                run: run,
                items: items
            )
        }
    }

    private static func submissionName(_ items: [CaptureActivityRecord]) -> String {
        guard items.count > 1 else { return items.first?.displayName ?? "Addition" }
        return "\(items.count) items added together"
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
            // Most recent first, by the store's own monotonic order rather than
            // by a timestamp two additions can share.
            unfiltered = activityGroups.sorted {
                $0.latestSequence == $1.latestSequence
                    ? $0.identifier > $1.identifier
                    : $0.latestSequence > $1.latestSequence
            }.map(FilesAndFoldersItem.activity)
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
            case .recentAdditions:
                return activityAvailability.isCurrent
                    ? "No recent additions are recorded."
                    : "Local activity is unavailable. Nothing recorded here could be read."
            }
        }
        if segment == .recentAdditions {
            let pending = items.filter { $0.statusFilter == .pending }.count
            let attention = items.filter { $0.statusFilter == .needsAttention }.count
            // Counts this surface could not refresh are labelled before they
            // are read, so no count is mistaken for the current position.
            let prefix = activityAvailability.isCurrent
                ? ""
                : "Local activity unavailable — last read, may be out of date · "
            return prefix
                + "\(pending) pending · \(attention) needs attention · \(items.count) listed "
                + "· workspace \(workspaceName)"
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
        case let .activity(value): return activityDetail(value)
        }
    }

    /// The frozen recovery identity of every item whose outcome is unknown, so
    /// it can be reconciled under exactly the same key. This reveals durable
    /// state and nothing else: no replacement key, no destination change and no
    /// removal is offered while an outcome may already have committed.
    public func recoveryDetail(for item: FilesAndFoldersItem?) -> [DetailFact] {
        // Recovery is a current-facts decision. If these facts could not be
        // persisted or refreshed, the detail says that instead, and the action
        // that would have offered them is disabled.
        guard canMutateActivity, case let .activity(group)? = item else { return [] }
        let unknown = group.items.filter { $0.fact == .checking }
        guard !unknown.isEmpty else { return [] }
        return unknown.map { record in
            DetailFact(
                label: "Recovery identity — \(record.displayName)",
                value: "item \(record.itemID) · operation \(record.operationKey)"
                    + " · attempt \(record.attemptGeneration)"
                    + (record.attemptOperationKey.map { " · attempt key \($0)" } ?? "")
                    + (record.attemptRequestDigest.map { " · request \($0)" } ?? "")
                    + (record.attemptDescriptorDigest.map { " · descriptor \($0)" } ?? "")
            )
        } + [
            DetailFact(
                label: "Reconciliation",
                value: "This addition is reconciled under its existing key. No replacement key, "
                    + "destination change or removal is offered while its outcome is unknown."
            ),
        ]
    }

    /// `activity` is the availability of the local activity record, so that a
    /// surface which could not persist or refresh it offers no action over it.
    /// The file and folder rows come from their own stores and are unaffected.
    public static func actions(
        for item: FilesAndFoldersItem?,
        activity: CaptureActivityAvailability = .current,
        coreHistory: CoreHistoryState = .notRequested
    ) -> FilesAndFoldersActions {
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
            // Reprocessing is offered only when a *current* read of this exact document
            // reported at least one capture it will accept. Not from a stale read, not
            // from a failed one, and not inferred from the file's local state.
            let reprocessable: Bool
            if case let .current(history) = coreHistory.availability(for: value.linkedDocumentID) {
                reprocessable = !history.reprocessableCaptures.isEmpty
            } else {
                reprocessable = false
            }
            return subscriptionActions(
                value.state,
                resumable: [.paused, .needsAccess],
                reprocessEnabled: reprocessable
            )
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
        case let .activity(value):
            // Facts that could not be persisted or refreshed are not acted on.
            // A retry, a stop, an advance or a revealed recovery identity chosen
            // from a stale record could act on work that has since moved on.
            guard activity.isCurrent else {
                return FilesAndFoldersActions(
                    pauseTitle: "Retry unavailable files",
                    pauseEnabled: false,
                    stopTitle: "Stop adding remaining files",
                    stopEnabled: false,
                    checkEnabled: false
                )
            }
            guard let run = value.run else {
                // An ordinary submission has no authority to offer here. The
                // queue exposes no cancellation and no return from a known
                // failure, so nothing pretends to retry or stop one; the only
                // safe action is revealing an unknown outcome's identity.
                return FilesAndFoldersActions(
                    pauseTitle: "Retry unavailable files",
                    pauseEnabled: false,
                    stopTitle: "Stop adding remaining files",
                    stopEnabled: false,
                    checkEnabled: false,
                    revealEnabled: value.count(.checking) > 0
                )
            }
            // Resuming is not retrying: a member that already failed keeps its
            // own outcome until Retry unavailable files is chosen for it. Every
            // action acts on this same frozen manifest and discovers nothing.
            return FilesAndFoldersActions(
                pauseTitle: run.state == .paused
                    ? "Resume"
                    : run.attentionCount > 0 ? "Retry unavailable files" : "Pause",
                pauseEnabled: [.adding, .paused, .needsAttention].contains(run.state),
                stopTitle: "Stop adding remaining files",
                // Only members that never started. A published or admitted
                // child keeps its canonical outcome.
                stopEnabled: run.plannedCount > 0,
                checkEnabled: run.state == .adding,
                revealEnabled: value.count(.checking) > 0
            )
        }
    }

    private static func subscriptionActions(
        _ state: FolderSubscriptionState,
        resumable: Set<FolderSubscriptionState>,
        reprocessEnabled: Bool = false
    ) -> FilesAndFoldersActions {
        FilesAndFoldersActions(
            pauseTitle: state == .reviewRequired
                ? "Continue changes"
                : resumable.contains(state) ? "Resume" : "Pause",
            pauseEnabled: [.active, .paused, .needsAccess, .reviewRequired].contains(state),
            stopTitle: "Stop updating",
            stopEnabled: state != .stopped,
            checkEnabled: state == .active,
            // Reprocessing works on bytes Core already holds, so a stopped watch or a
            // missing original does not bar it. What bars it is Core saying so.
            reprocessEnabled: reprocessEnabled
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
        facts += coreHistoryFacts(documentID: value.linkedDocumentID)
        return facts
    }

    /// The canonical Core history of one document, as rows, or the exact reason there is
    /// none to show.
    ///
    /// Two separately labelled histories, because they are two histories: the immutable
    /// source captures Core admitted, and the extraction revisions over them. Each row
    /// says whether it is current or historical-only from Core's own head statement, and
    /// no row is promoted to "current" by being last. Local delivery activity is named as
    /// a separate record and is never listed among these rows.
    private func coreHistoryFacts(documentID: String) -> [DetailFact] {
        switch coreHistory.availability(for: documentID) {
        case .notRequested:
            return [Self.coreHistoryNotReadFact]
        case .loading:
            return [
                DetailFact(
                    label: "Capture history",
                    value: "Reading the capture and extraction history from Core."
                ),
            ]
        case let .unavailable(error):
            return [
                DetailFact(label: "Capture history", value: Self.historyUnavailable(error)),
            ]
        case let .current(history):
            return Self.historyFacts(history, stale: false)
        case let .stale(history):
            return Self.historyFacts(history, stale: true)
        }
    }

    private static func historyFacts(
        _ history: CoreDocumentHistory,
        stale: Bool
    ) -> [DetailFact] {
        var facts: [DetailFact] = []
        if stale {
            facts.append(
                DetailFact(
                    label: "Capture history",
                    value: "These are the last facts Core reported and may be out of date. "
                        + "Reprocessing is unavailable until they can be read again."
                )
            )
        }
        facts += [
            DetailFact(label: "Document in Core", value: history.documentID),
            DetailFact(label: "First added to Core", value: history.createdAt),
            DetailFact(label: "Source state", value: sourceStateText(history)),
            DetailFact(label: "Original access", value: bindingText(history)),
            DetailFact(label: "Current searchable version", value: retrievalText(history)),
            DetailFact(
                label: "Source capture history",
                value: countText(
                    shown: history.captures.count,
                    total: history.captureCount,
                    complete: history.complete,
                    noun: "source capture"
                )
            ),
        ]
        facts += history.captures
            .sorted { $0.captureSequence > $1.captureSequence }
            .map { capture in
                DetailFact(
                    label: "Source capture \(capture.captureSequence)",
                    value: captureText(capture)
                )
            }
        facts.append(
            DetailFact(
                label: "Extraction and profile history",
                value: countText(
                    shown: history.extractions.count,
                    total: history.extractionCount,
                    complete: history.complete,
                    noun: "extraction"
                )
            )
        )
        facts += history.extractions
            .sorted { $0.documentOrdinal > $1.documentOrdinal }
            .map { revision in
                DetailFact(
                    label: "Extraction \(revision.documentOrdinal)",
                    value: extractionText(revision)
                )
            }
        facts.append(Self.localActivityBoundaryFact)
        return facts
    }

    private static func sourceStateText(_ history: CoreDocumentHistory) -> String {
        switch history.source.sourceState {
        case .removed:
            return "Removed in Core. No new capture of this source is admitted and no "
                + "captured copy can be processed again."
        case .active:
            guard let current = history.source.currentCaptureID else {
                return "Active, with no successful capture yet."
            }
            return "Active. Capture \(history.source.captureHeadSequence) is the current "
                + "source (\(current))."
        }
    }

    private static func bindingText(_ history: CoreDocumentHistory) -> String {
        let reason = history.refreshUnavailableReason
        switch history.source.originalBindingState {
        case .connected:
            return reason == nil
                ? "Connected on this device. The original can be read again."
                : "Connected, but Core reports that reading the original again is "
                    + "unavailable: \(unavailableText(reason))."
        case .disconnected:
            return "Disconnected. The original is not read again; captured copies in Core "
                + "are kept and can still be inspected. Use Locate to choose the file again."
        case .noRoute:
            return "None recorded. This document has no original link on this device, so "
                + "only its captured copies are available. Use Locate to link an original."
        }
    }

    private static func retrievalText(_ history: CoreDocumentHistory) -> String {
        switch history.extraction.retrievalState {
        case .notRetrievable:
            return "None published. No extraction of this document is currently searchable."
        case .retrievable:
            guard let current = history.extraction.currentExtractionID,
                  let published = history.extraction.publishedAt
            else { return "Published." }
            let revision = history.extractions.first { $0.extractionRevisionID == current }
            let profile = revision.map {
                " · \(profileText($0.profile))"
            } ?? ""
            return "Extraction \(history.extraction.extractionHeadOrdinal), published "
                + "\(published)\(profile)"
        }
    }

    private static func countText(
        shown: Int,
        total: UInt64,
        complete: Bool,
        noun: String
    ) -> String {
        let plural = total == 1 ? noun : "\(noun)s"
        guard !complete || UInt64(shown) != total else { return "\(total) \(plural)" }
        return "Showing \(shown) of \(total) \(plural)"
    }

    /// One capture's proven facts. Current versus historical-only is Core's head
    /// statement; retention is this capture's own and moves independently of it.
    private static func captureText(_ capture: CoreSourceCapture) -> String {
        var parts = [capture.isCurrentSource ? "Current source" : "Historical only"]
        if !capture.isCurrentSource {
            parts.append("reason \(dispositionText(capture.dispositionReason))")
        }
        parts.append(contentsOf: [
            "taken \(capture.capturedAt)",
            intentText(capture.intentKind),
            retentionText(capture),
            processingText(capture.processingState),
            "\(capture.mediaType) · \(capture.sourceLengthBytes) bytes",
            "requested \(capture.requestedProfile.profileRef) "
                + "revision \(capture.requestedProfile.revision)",
            "capture \(capture.captureID)",
        ])
        if let reason = capture.reprocessUnavailableReason {
            parts.append("reprocessing unavailable: \(unavailableText(reason))")
        } else if capture.reprocessAvailable {
            parts.append(
                "can be processed again as "
                    + capture.reprocessProfiles.map(\.title).joined(separator: " or ")
            )
        }
        return parts.joined(separator: " · ")
    }

    /// One extraction revision's proven facts. A revision with no terminal result is
    /// pending, which is not a failure; a revision that is not the head is historical
    /// only, whatever order it completed in.
    private static func extractionText(_ revision: CoreExtractionRevision) -> String {
        var parts: [String] = []
        if revision.isPending {
            parts.append("Processing")
        } else {
            parts.append(resultText(revision))
        }
        parts.append(contentsOf: [
            "capture \(revision.captureID)",
            profileText(revision.profile),
            "admitted \(revision.admittedAt)",
        ])
        if let completed = revision.completedAt { parts.append("completed \(completed)") }
        if let build = revision.workerBuild { parts.append("worker \(build)") }
        parts.append("extraction \(revision.extractionRevisionID)")
        return parts.joined(separator: " · ")
    }

    private static func resultText(_ revision: CoreExtractionRevision) -> String {
        let outcome: String
        switch revision.resultState {
        case .succeeded: outcome = "Searchable text extracted"
        case .partial: outcome = "Added with limitations"
        case .unsupported: outcome = "Not supported by this processing"
        case .failed: outcome = "Processing failed"
        case nil: outcome = "Processing"
        }
        if revision.isCurrentExtraction { return "\(outcome) · current searchable version" }
        switch revision.publicationDisposition {
        case .promoted: return outcome
        case .superseded: return "\(outcome) · historical only, a newer extraction is current"
        case .historicalOnly:
            return "\(outcome) · historical only, it was based on a version that is no "
                + "longer current"
        case .fencedRemoval:
            return "\(outcome) · historical only, the source was removed before publication"
        case .fencedRetention:
            return "\(outcome) · historical only, the captured copy stopped being retained "
                + "before publication"
        case .notPublished: return "\(outcome) · nothing was replaced"
        case nil: return outcome
        }
    }

    private static func profileText(_ profile: CoreProfileSummary) -> String {
        var parts = [
            profile.processingMode == .textOnly ? "Text only" : "Text and local OCR",
        ]
        if !profile.ocrLanguages.isEmpty {
            parts.append("languages \(profile.ocrLanguages.joined(separator: ", "))")
        }
        if profile.embeddedImageOcr { parts.append("embedded images examined") }
        parts.append("profile \(profile.decoderBuild)")
        parts.append("digest \(profile.digest)")
        return parts.joined(separator: " · ")
    }

    private static func dispositionText(_ reason: CoreDispositionReason) -> String {
        switch reason {
        case .eligible: "eligible"
        case .removedSource: "the source was removed"
        case .supersededGeneration: "its original link had been superseded"
        case .disconnectedGeneration: "its original link had been disconnected"
        case .creationConflict: "the document already existed"
        case .staleBase: "a newer capture was already current"
        }
    }

    private static func intentText(_ intent: CoreIntentKind) -> String {
        switch intent {
        case .newCapture: "first addition"
        case .refresh: "manual refresh"
        case .automaticCapture: "automatic update"
        case .folderCapture: "folder addition"
        }
    }

    private static func retentionText(_ capture: CoreSourceCapture) -> String {
        let changed = capture.retentionChangedAt.map { " since \($0)" } ?? ""
        switch capture.retentionState {
        case .retained: return "copy retained"
        case .expired: return "copy no longer available\(changed)"
        case .deleted: return "copy deleted\(changed)"
        }
    }

    private static func processingText(_ state: CoreProcessingState) -> String {
        switch state {
        case .pending: "processing pending in Core"
        case .settled: "processing settled"
        case .notQueued: "no processing recorded"
        }
    }

    private static func unavailableText(_ reason: CoreHistoryUnavailableReason?) -> String {
        switch reason {
        case .sourceRemoved: "this source was removed in Core"
        case .sourceNotRetained: "the captured copy is no longer retained"
        case .formatNotQualified: "this file type has no supported processing"
        case .noOriginalBinding: "no original is linked on this device"
        case .originalDisconnected: "the original link was disconnected"
        case nil: "Core reported no reason"
        }
    }

    private static func historyUnavailable(_ error: CoreHistoryError) -> String {
        switch error {
        case .unavailable:
            return "Unavailable. Core could not be reached from this Mac, so no capture or "
                + "extraction history is shown. Nothing already in Core is affected."
        case .timedOut:
            return "Unavailable. Core did not answer in time. Nothing already in Core is "
                + "affected."
        case .unreadable:
            return "Unavailable. Core answered with a document this version cannot read. "
                + "Update required before capture history can be shown."
        case let .refused(code):
            switch code {
            case "not_found":
                return "No history. This workspace holds no document for this file yet."
            case "workspace_not_granted", "authorization_denied", "authentication_required",
                 "capability_not_granted":
                return "Access to this workspace is required before its capture history can "
                    + "be shown."
            case "incompatible_version", "upgrade_required":
                return "Update required before capture history can be shown."
            default:
                return "Unavailable. Core refused the request (\(code))."
            }
        }
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

    /// One addition's own facts, then every item's own outcome. The activity
    /// store records nothing about capture or extraction, so neither does this.
    private func activityDetail(_ value: CaptureActivityGroup) -> [DetailFact] {
        var facts = [DetailFact(label: "Addition", value: value.displayName)]
        if !activityAvailability.isCurrent { facts.append(Self.activityUnavailableFact) }
        if let run = value.run {
            facts += [
                DetailFact(label: "Folder run", value: run.runID),
                DetailFact(label: "Manifest revision", value: run.manifestRevision),
                DetailFact(label: "Root", value: run.rootRef),
                DetailFact(label: "Destination workspace", value: run.workspaceRef),
                DetailFact(label: "Started", value: run.createdAt),
                DetailFact(label: "Run status", value: FilesAndFoldersItem.label(run.state)),
                DetailFact(
                    label: "Updates",
                    value: run.subscriptionID == nil
                        ? "Added once. This run establishes no watch."
                        : "This folder is also kept updated as a folder subscription."
                ),
            ]
        } else {
            facts += [
                DetailFact(label: "Submission", value: value.identifier),
                DetailFact(label: "Destination workspace", value: workspaceName),
                DetailFact(
                    label: "Updates",
                    value: "Added once. This submission establishes no watch."
                ),
            ]
        }
        facts += [
            DetailFact(label: "Files in this addition", value: String(value.items.count)),
            DetailFact(label: "Progress", value: FilesAndFoldersItem.progressText(value)),
            // The activity record holds a per-file outcome and nothing else: an
            // addition that mostly succeeded is not an addition that succeeded.
            DetailFact(
                label: "Whole-addition result",
                value: "Not recorded. Each file carries its own outcome."
            ),
        ]
        facts += value.items.map {
            DetailFact(label: $0.displayName, value: Self.itemDetail($0))
        }
        if let run = value.run, FilesAndFoldersItem.isTerminal(run.state) {
            facts.append(Self.retainedLinksFact)
        }
        facts.append(Self.retentionFact)
        facts.append(Self.coreHistoryFact)
        return facts
    }

    /// One item's proven fact and the identity it is recovered by.
    private static func itemDetail(_ record: CaptureActivityRecord) -> String {
        var parts = [
            FilesAndFoldersItem.label(record.fact),
            "item \(record.itemID)",
        ]
        if let code = record.failureCode { parts.append("reason \(code)") }
        if let captureID = record.admittedCaptureID { parts.append("capture \(captureID)") }
        parts.append(
            record.resolvedAt.map { "resolved \($0)" } ?? "first seen \(record.firstObservedAt)"
        )
        return parts.joined(separator: " · ")
    }

    /// What the bound is and, just as importantly, what clearing does not do.
    private static let retentionFact = DetailFact(
        label: "Activity retention",
        value: "Completed activity is kept for up to 30 days or 100 items. Clearing it removes "
            + "local receipts only: queued work and its recovery keys, original links, running "
            + "watches, folder run manifests and anything already in Core are untouched."
    )

    /// Said plainly when the activity store could not record or re-read what the
    /// durable owners now prove: nothing observed since reached local storage,
    /// what is listed is only the last thing this process read, and no action
    /// over it is offered. The durable owners themselves are untouched.
    private static let activityUnavailableFact = DetailFact(
        label: "Local activity",
        value: "Unavailable. These are the last records this Mac read and may be out of date; "
            + "nothing observed since was saved locally. Clearing, retrying, stopping, checking "
            + "and recovery details are unavailable until it can be read again. Queued work and "
            + "its recovery keys, original links, running watches, folder run manifests and "
            + "everything already in Core are unchanged."
    )

    private static let retainedLinksFact = DetailFact(
        label: "Already added files",
        value: "Still in Core and still linked, so they can be refreshed manually."
    )

    /// What a folder, a retained root or an addition can say about capture history:
    /// nothing of its own. Those rows are not one document, so there is no one document's
    /// history to show for them -- the files under them each have their own.
    private static let coreHistoryFact = DetailFact(
        label: "Capture history",
        value: "Capture, extraction and profile history is held per file in Core. Select a "
            + "file to see its own history."
    )

    /// Said before any read has been asked for, and after a selection changes.
    private static let coreHistoryNotReadFact = DetailFact(
        label: "Capture history",
        value: "Not read yet. Core holds the capture, extraction and profile history for "
            + "this file."
    )

    /// The boundary, said once at the end of the canonical rows. Without it a reader has
    /// no way to tell which of two records they are looking at, and the two are not
    /// interchangeable: one is what Core did, the other is what this Mac did.
    private static let localActivityBoundaryFact = DetailFact(
        label: "Local delivery activity",
        value: "Listed separately under Recent additions. Those are this Mac's own delivery "
            + "receipts, not Core's record, and a local receipt is never a capture or an "
            + "extraction."
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
