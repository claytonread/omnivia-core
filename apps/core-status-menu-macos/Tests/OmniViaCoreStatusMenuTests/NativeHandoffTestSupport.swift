import CryptoKit
import Foundation
import XCTest
@testable import CoreCaptureMacOS
@testable import DocumentIntakeMacOS
@testable import OmniViaCoreStatusMenu

struct InjectedFailure: Error, Equatable {}

/// What the review owner was handed, batch by batch.
final class AcceptedBox {
    var batches: [[ManualCaptureCandidate]] = []
}

/// How many times the Share extension's chooser continuation reached review.
final class ChooserBox {
    var opens = 0
}

/// A bookmark codec whose first `failures` grants cannot be created.
final class StubBookmarkCodec: SecurityScopedBookmarkCoding, @unchecked Sendable {
    private let lock = NSLock()
    private var remainingFailures: Int

    init(failures: Int = 0) { remainingFailures = failures }

    func makeReadOnlyBookmark(for url: URL) throws -> Data {
        try lock.withLock {
            if remainingFailures > 0 {
                remainingFailures -= 1
                throw InjectedFailure()
            }
            return Data(url.path.utf8)
        }
    }

    func resolveReadOnlyBookmark(_ data: Data) throws -> (url: URL, stale: Bool) {
        (URL(fileURLWithPath: String(decoding: data, as: UTF8.self)), false)
    }

    func startAccessing(_ url: URL) -> Bool { true }
    func stopAccessing(_ url: URL) {}
}

/// One simulated companion process over durable state, wired the way
/// `main.swift` wires it. Launching again over the same `root` is a restart:
/// every durable store survives, process memory (review, the receiver's
/// presented set) does not.
@MainActor
struct Companion {
    let receiver: NativeHandoffReceiver
    let panel: CapturePanelController?
    /// The folder submitter the panel was given, when the launch asked for one.
    let folders: FolderCaptureSubmission?
    let environment: DocumentIntakeEnvironment
    let submitter: ManualCaptureSubmission
    let mailbox: NativeHandoffMailbox
    let candidates: NativeIntakeCandidateStore
    let snapshots: OwnedSnapshotStore
    let accepted: AcceptedBox
    /// The durable, snapshot-only Share spool in the App Group, and the
    /// companion side that adopts from it.
    let shareInbox: ShareInboxStore
    let shareAdopter: ShareInboxAdopter
    let chooser: ChooserBox

    /// Access references whose grant is currently connected.
    var connectedAccessRefs: [String] {
        environment.accessStore.descriptors().filter { $0.state == .connected }.map(\.accessRef)
    }

    /// The folder root grants that still hold their route — a draft's prepared
    /// grant included — each naming the owners holding it.
    var connectedRoots: [NativeRootAccessDescriptor] {
        environment.accessStore.rootDescriptors().filter { $0.state.retainsRoute }
    }
}

@MainActor
class NativeHandoffTestCase: XCTestCase {
    func launch(
        root: URL,
        accepted: AcceptedBox = AcceptedBox(),
        bookmarkFailures: Int = 0,
        withPanel: Bool = false,
        withFolders: Bool = false,
        chooser: ChooserBox = ChooserBox()
    ) throws -> Companion {
        let intake = root.appendingPathComponent("native-intake", isDirectory: true)
        let environment = try DocumentIntakeEnvironment(
            stateRoot: intake,
            bookmarkCodec: StubBookmarkCodec(failures: bookmarkFailures)
        )
        let snapshots = try OwnedSnapshotStore(root: ownedRoot(root))
        let mailbox = try NativeHandoffMailbox(
            root: intake.appendingPathComponent("authenticated-native-handoff", isDirectory: true)
        )
        let candidates = try NativeIntakeCandidateStore(root: candidatesRoot(root))
        let submitter = ManualCaptureSubmission(
            environment: environment,
            workspaceRef: "workspace-1",
            snapshotStore: snapshots,
            candidates: candidates
        )
        let folders = withFolders
            ? FolderCaptureSubmission(environment: environment, workspaceRef: "workspace-1")
            : nil
        let panel = withPanel
            ? CapturePanelController(
                workspaceName: "workspace-1",
                submitter: submitter,
                folderSubmitter: folders,
                onQueued: nil,
                opensWindow: false
            )
            : nil
        let receiver = NativeHandoffReceiver(
            mailbox: mailbox,
            resources: try appGroup(root),
            candidates: candidates,
            submitter: submitter,
            snapshotStore: snapshots,
            accept: { batch in
                accepted.batches.append(batch)
                panel?.addIncomingAdopted(batch)
            }
        )
        let inbox = try shareInbox(root)
        let adopter = ShareInboxAdopter(
            inbox: inbox,
            snapshotStore: snapshots,
            openChooser: { chooser.opens += 1 },
            accept: { batch in
                accepted.batches.append(batch)
                return panel?.addIncomingAdopted(batch) ?? true
            }
        )
        return Companion(
            receiver: receiver,
            panel: panel,
            folders: folders,
            environment: environment,
            submitter: submitter,
            mailbox: mailbox,
            candidates: candidates,
            snapshots: snapshots,
            accepted: accepted,
            shareInbox: inbox,
            shareAdopter: adopter,
            chooser: chooser
        )
    }

    func shareInbox(_ root: URL) throws -> ShareInboxStore {
        try ShareInboxStore(
            root: root.appendingPathComponent("app-group", isDirectory: true)
                .appendingPathComponent(ShareInboxStore.directoryName, isDirectory: true)
        )
    }

    /// Exactly what the Share extension does: stage every item's bytes into a
    /// request directory, then publish the complete envelope in one step.
    @discardableResult
    func publishShare(
        _ inbox: ShareInboxStore,
        _ contents: [(name: String, text: String)],
        kind: ShareSnapshotKind = .file
    ) throws -> String {
        let request = try stageShare(inbox, contents)
        try inbox.publish(ShareInboxEnvelopeV1(
            requestID: request.id,
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            createdAt: Date(),
            items: request.items.map {
                ShareInboxItemV1(
                    slotID: $0.slotID,
                    displayName: $0.displayName,
                    relativePath: $0.relativePath,
                    kind: kind,
                    byteLength: $0.byteLength,
                    digest: $0.digest
                )
            }
        ))
        return request.id
    }

    /// The staged-but-unpublished half of the above: what a share that crashed
    /// before the atomic publish leaves behind.
    func stageShare(
        _ inbox: ShareInboxStore,
        _ contents: [(name: String, text: String)]
    ) throws -> (id: String, directory: URL, items: [ShareInboxItemV1]) {
        let request = try inbox.beginRequest()
        var items: [ShareInboxItemV1] = []
        for (index, entry) in contents.enumerated() {
            let relativePath = ShareInboxStore.payloadName(index: index)
            let bytes = Data(entry.text.utf8)
            try bytes.write(to: try inbox.payloadDestination(request, relativePath: relativePath))
            items.append(ShareInboxItemV1(
                slotID: newID(),
                displayName: entry.name,
                relativePath: relativePath,
                kind: .file,
                byteLength: UInt64(bytes.count),
                digest: ShareInboxStore.digest(of: bytes)
            ))
        }
        return (request.requestID, request.directory, items)
    }

    func inboxRoot(_ root: URL) -> URL {
        root.appendingPathComponent("app-group", isDirectory: true)
            .appendingPathComponent(ShareInboxStore.directoryName, isDirectory: true)
    }

    func pendingDirectory(_ root: URL, requestID: String) -> URL {
        inboxRoot(root)
            .appendingPathComponent("pending", isDirectory: true)
            .appendingPathComponent(requestID, isDirectory: true)
    }

    func appGroup(_ root: URL) throws -> NativeHandoffMailbox {
        try NativeHandoffMailbox(root: root.appendingPathComponent("app-group", isDirectory: true))
    }

    func ownedRoot(_ root: URL) -> URL {
        root.appendingPathComponent("native-intake", isDirectory: true)
            .appendingPathComponent("owned-snapshots", isDirectory: true)
    }

    func candidatesRoot(_ root: URL) -> URL {
        root.appendingPathComponent("native-intake", isDirectory: true)
            .appendingPathComponent("native-intake-candidates", isDirectory: true)
    }

    func acknowledgementsRoot(_ root: URL) -> URL {
        root.appendingPathComponent("native-intake", isDirectory: true)
            .appendingPathComponent("authenticated-native-handoff", isDirectory: true)
            .appendingPathComponent("acknowledgements", isDirectory: true)
    }

    func ownedRequests(_ root: URL) throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: ownedRoot(root).path).sorted()
    }

    func ownedFiles(_ root: URL, nonce: String) throws -> [String] {
        try FileManager.default.contentsOfDirectory(
            atPath: ownedRoot(root).appendingPathComponent(nonce, isDirectory: true).path
        ).sorted()
    }

    func ownedURL(_ root: URL, nonce: String, name: String) -> URL {
        ownedRoot(root).appendingPathComponent(nonce, isDirectory: true)
            .appendingPathComponent(name, isDirectory: false)
    }

    func newID() -> String { UUID().uuidString.lowercased() }

    func sha256(_ text: String) -> String {
        "sha256:" + SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// Fails the test unless `body` throws, and hands back what it threw.
    @discardableResult
    func failure<T>(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () async throws -> T
    ) async -> Error? {
        do {
            _ = try await body()
            XCTFail("expected a failure", file: file, line: line)
            return nil
        } catch {
            return error
        }
    }

    /// A Finder offer: one slot per selected item, in order, classified the way
    /// the Quick Action classifies it — a directory as `folder_bookmark` and a
    /// regular file as `original_bookmark`.
    func finderOffer(
        nonce: String,
        slotIDs: [String],
        urls: [URL],
        now: Date = Date()
    ) throws -> NativeHandoffOfferV1 {
        NativeHandoffOfferV1(
            requestNonce: nonce,
            expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
            senderBundleID: NativeHandoffMailbox.finderBundleID,
            surface: .finderQuickAction,
            createdAt: now,
            expiresAt: now.addingTimeInterval(NativeHandoffMailbox.maximumLifetime),
            slots: try zip(slotIDs, urls).map { slotID, url in
                let isDirectory = (try? url.resourceValues(forKeys: [.isDirectoryKey]))?
                    .isDirectory == true
                return NativeHandoffSlotV1(
                    slotID: slotID,
                    displayName: url.lastPathComponent,
                    sourceKind: isDirectory ? .folderBookmark : .originalBookmark,
                    bookmark: try url.bookmarkData(
                        options: [],
                        includingResourceValuesForKeys: nil,
                        relativeTo: nil
                    )
                )
            }
        )
    }

    func makeFolder(_ root: URL, name: String, files: [String] = []) throws -> URL {
        let url = root.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        for file in files {
            try Data("member".utf8).write(to: url.appendingPathComponent(file))
        }
        return url
    }

    /// A Share offer whose slot `i` points at `blob-(i+1)` of the nonce's
    /// transfer directory and is named `names[i]`.
    func shareOffer(
        nonce: String,
        slotIDs: [String],
        names: [String] = ["alpha.txt", "beta.txt", "gamma.txt"],
        now: Date = Date()
    ) -> NativeHandoffOfferV1 {
        NativeHandoffOfferV1(
            requestNonce: nonce,
            expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
            senderBundleID: NativeHandoffMailbox.shareBundleID,
            surface: .share,
            createdAt: now,
            expiresAt: now.addingTimeInterval(NativeHandoffMailbox.maximumLifetime),
            slots: slotIDs.enumerated().map { index, slotID in
                NativeHandoffSlotV1(
                    slotID: slotID,
                    displayName: names[index],
                    sourceKind: .capturedSnapshot,
                    snapshotRelativePath: "blob-\(index + 1)"
                )
            }
        )
    }

    /// Writes the extension's transfer blobs into the App Group directory.
    @discardableResult
    func writeTransferBlobs(
        _ root: URL,
        nonce: String,
        contents: [String] = ["alpha", "beta"]
    ) throws -> URL {
        let transfer = try appGroup(root).prepareSnapshotDirectory(nonce: nonce)
        for (index, text) in contents.enumerated() {
            try Data(text.utf8).write(to: transfer.appendingPathComponent("blob-\(index + 1)"))
        }
        return transfer
    }

    func writeOriginal(_ root: URL, name: String, text: String = "original") throws -> URL {
        let url = root.appendingPathComponent(name)
        try Data(text.utf8).write(to: url)
        return url
    }

    func temporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("native-handoff-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock {
            // A test that made a directory unwritable must not strand it.
            if let enumerator = FileManager.default.enumerator(atPath: directory.path) {
                for case let path as String in enumerator {
                    let url = directory.appendingPathComponent(path)
                    try? FileManager.default.setAttributes(
                        [.posixPermissions: 0o700],
                        ofItemAtPath: url.path
                    )
                }
            }
            try? FileManager.default.removeItem(at: directory)
        }
        return directory
    }

    /// Runs `body` with a directory made unreadable or unwritable. Skipped as
    /// root, which ignores permission bits.
    @discardableResult
    func withPermissions<T>(
        _ permissions: Int,
        on url: URL,
        _ body: () async throws -> T
    ) async throws -> T {
        try XCTSkipIf(getuid() == 0, "permission bits do not bind root")
        try FileManager.default.setAttributes([.posixPermissions: permissions], ofItemAtPath: url.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path) }
        return try await body()
    }
}
