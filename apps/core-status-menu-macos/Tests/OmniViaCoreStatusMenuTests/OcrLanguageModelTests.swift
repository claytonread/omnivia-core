// The Document processing pane's language states.
//
// These are the pane's decisions without the pane: which state it is in, what
// it may act on, and what happens around a mutation. The administering client
// is a stub, so nothing here needs a service — the real one is covered by
// `OcrResourceAdministrationTests`.

import Foundation
import XCTest

@testable import CoreSettingsMacOS

private func identity(
    _ language: String, size: Int = 4_113_088, digest: String? = nil
) -> OcrResourceIdentity {
    OcrResourceIdentity(
        language: language,
        engineVersion: "5.5.3",
        modelVersion: "tessdata_fast-4.1.0",
        byteSize: size,
        sha256: digest ?? String(repeating: language.first.map(String.init) ?? "a", count: 64)
    )
}

private func inventory(
    _ resources: [OcrInstalledResource], installed: Int = 0
) -> OcrResourceInventory {
    OcrResourceInventory(
        capacityBytes: 536_870_912,
        installedBytes: installed,
        availableBytes: 536_870_912 - installed,
        resources: resources
    )
}

private let english = OcrInstalledResource(
    identity: identity("eng"), pinned: false, pinnedBy: [], bytesPresent: true
)
private let pinnedGerman = OcrInstalledResource(
    identity: identity("deu"), pinned: true, pinnedBy: ["op-7"], bytesPresent: true
)

/// A scripted administering client. Each call pops the next queued answer and
/// records what was asked, so a mutation's follow-up read is visible.
private final class StubAdministrator: OcrResourceAdministering, @unchecked Sendable {
    enum Call: Equatable { case status, remove(OcrResourceIdentity), reconcile, install }

    private(set) var calls: [Call] = []
    private var statuses: [Result<OcrResourceInventory, OcrResourceError>]
    private var mutations: [Result<OcrResourceInventory, OcrResourceError>]
    private var repairs: [Result<OcrReconcileOutcome, OcrResourceError>]

    init(
        statuses: [Result<OcrResourceInventory, OcrResourceError>],
        mutations: [Result<OcrResourceInventory, OcrResourceError>] = [],
        repairs: [Result<OcrReconcileOutcome, OcrResourceError>] = []
    ) {
        self.statuses = statuses
        self.mutations = mutations
        self.repairs = repairs
    }

    func status() async -> Result<OcrResourceInventory, OcrResourceError> {
        calls.append(.status)
        return statuses.isEmpty ? .failure(.unreadableOutput) : statuses.removeFirst()
    }

    func remove(
        _ identity: OcrResourceIdentity
    ) async -> Result<OcrResourceInventory, OcrResourceError> {
        calls.append(.remove(identity))
        return mutations.isEmpty ? .failure(.unreadableOutput) : mutations.removeFirst()
    }

    func reconcile() async -> Result<OcrReconcileOutcome, OcrResourceError> {
        calls.append(.reconcile)
        return repairs.isEmpty ? .failure(.unreadableOutput) : repairs.removeFirst()
    }

    func install(
        payload: URL, identity: OcrResourceIdentity
    ) async -> Result<OcrResourceInventory, OcrResourceError> {
        calls.append(.install)
        return mutations.isEmpty ? .failure(.unreadableOutput) : mutations.removeFirst()
    }
}

@MainActor
final class OcrLanguageModelTests: XCTestCase {
    // MARK: States

    /// No packaged service: the pane says so, and nothing is ever run.
    func testWithoutAnAdministratorThereIsNothingToRead() async {
        let model = OcrLanguageModel(administering: nil)
        XCTAssertEqual(model.state, .noAdministrator)
        XCTAssertFalse(model.canAct)
        await model.load()
        XCTAssertEqual(model.state, .noAdministrator)
    }

    func testTheModelStartsLoadingAndBecomesReady() async {
        let stub = StubAdministrator(statuses: [.success(inventory([english], installed: 4_113_088))])
        let model = OcrLanguageModel(administering: stub)
        XCTAssertEqual(model.state, .loading)

        await model.load()
        XCTAssertEqual(model.state, .ready(inventory([english], installed: 4_113_088)))
        XCTAssertNil(model.staleError)
        XCTAssertTrue(model.canAct)
    }

    /// A first read that fails leaves nothing to show — and must not be
    /// rendered as an empty cache.
    func testAFirstFailedReadIsUnavailableNotEmpty() async {
        let stub = StubAdministrator(statuses: [.failure(.unavailable)])
        let model = OcrLanguageModel(administering: stub)
        await model.load()
        XCTAssertEqual(model.state, .unavailable(.unavailable))
        XCTAssertNil(model.inventory)
    }

    /// A failed *re*-read keeps the previous figures and marks them previous.
    /// A failed read is not proof that the cache emptied.
    func testAFailedRereadLeavesThePreviousReadingMarkedStale() async {
        let first = inventory([english], installed: 4_113_088)
        let stub = StubAdministrator(statuses: [.success(first), .failure(.timedOut)])
        let model = OcrLanguageModel(administering: stub)
        await model.load()
        await model.refresh()

        XCTAssertEqual(model.state, .ready(first))
        XCTAssertEqual(model.staleError, .timedOut)
    }

    func testASuccessfulRereadClearsTheStaleMark() async {
        let first = inventory([english], installed: 4_113_088)
        let stub = StubAdministrator(statuses: [
            .success(first), .failure(.timedOut), .success(inventory([])),
        ])
        let model = OcrLanguageModel(administering: stub)
        await model.load()
        await model.refresh()
        await model.refresh()

        XCTAssertEqual(model.state, .ready(inventory([])))
        XCTAssertNil(model.staleError)
    }

    /// `load` is what the window calls on open; calling it again is a no-op so
    /// reopening the pane does not re-run the service.
    func testLoadIsIdempotentUntilForced() async {
        let stub = StubAdministrator(statuses: [.success(inventory([])), .success(inventory([english]))])
        let model = OcrLanguageModel(administering: stub)
        await model.load()
        await model.load()
        XCTAssertEqual(stub.calls, [.status])
        await model.refresh()
        XCTAssertEqual(stub.calls, [.status, .status])
    }

    // MARK: Removal

    func testOnlyAnUnpinnedSetMayBeRemoved() {
        XCTAssertTrue(OcrLanguageModel.canRemove(english))
        XCTAssertFalse(OcrLanguageModel.canRemove(pinnedGerman))
    }

    /// A pinned set is refused before anything runs: §18.4's pin is not
    /// something an attempted removal may cancel.
    func testRemovingAPinnedSetIsRefusedWithoutRunningAnything() async {
        let stub = StubAdministrator(statuses: [.success(inventory([pinnedGerman]))])
        let model = OcrLanguageModel(administering: stub)
        await model.load()

        let result = await model.remove(pinnedGerman)
        guard case .failure(.refused(let code, let reason))? = Optional(result) else {
            return XCTFail("expected a refusal, got \(result)")
        }
        XCTAssertEqual(code, OcrResourceError.pinned)
        XCTAssertTrue(reason.contains("op-7"))
        XCTAssertEqual(stub.calls, [.status])   // nothing beyond the first read
        XCTAssertNil(model.busy)
    }

    /// A removal is followed by a fresh reading, and the fresh reading — not
    /// the removal's own copy of the inventory — is what the pane shows.
    func testARemovalRefreshesFromAFreshReading() async {
        let before = inventory([english], installed: 4_113_088)
        let afterMutation = inventory([english], installed: 4_113_088)   // the stale copy
        let afterRead = inventory([])
        let stub = StubAdministrator(
            statuses: [.success(before), .success(afterRead)],
            mutations: [.success(afterMutation)]
        )
        let model = OcrLanguageModel(administering: stub)
        await model.load()

        let result = await model.remove(english)
        XCTAssertNotNil(try? result.get())
        XCTAssertEqual(stub.calls, [.status, .remove(english.identity), .status])
        XCTAssertEqual(model.state, .ready(afterRead))
        XCTAssertNil(model.busy)
    }

    /// A refused removal still re-reads: the refusal is reported, and what is
    /// on screen afterwards is what the cache holds now.
    func testARefusedRemovalIsReportedAndStillRefreshes() async {
        let stub = StubAdministrator(
            statuses: [.success(inventory([english])), .success(inventory([english]))],
            mutations: [.failure(.refused(code: OcrResourceError.pinned, reason: "pinned by op-9"))]
        )
        let model = OcrLanguageModel(administering: stub)
        await model.load()
        let result = await model.remove(english)

        XCTAssertEqual(
            result.failure, .refused(code: OcrResourceError.pinned, reason: "pinned by op-9")
        )
        XCTAssertEqual(stub.calls, [.status, .remove(english.identity), .status])
        XCTAssertEqual(model.state, .ready(inventory([english])))
    }

    /// Conflicting actions are disabled while one is in flight. The flag is
    /// observed through `onChange`, because that is when the window redraws.
    func testAnOperationInFlightBlocksTheOthers() async {
        let stub = StubAdministrator(
            statuses: [.success(inventory([english])), .success(inventory([]))],
            mutations: [.success(inventory([]))]
        )
        let model = OcrLanguageModel(administering: stub)
        await model.load()

        var busyDuring: [String?] = []
        model.onChange = { busyDuring.append(model.busy) }
        await model.remove(english)

        XCTAssertEqual(busyDuring.first, OcrLanguageModel.operationKey(for: english.identity))
        XCTAssertNil(model.busy)
        XCTAssertTrue(model.canAct)
    }

    /// Two model revisions of one language can both be installed, so the busy
    /// key is the whole identity rather than the language.
    func testTheBusyKeyIsTheWholeIdentity() {
        let one = identity("eng", digest: String(repeating: "a", count: 64))
        let two = identity("eng", digest: String(repeating: "b", count: 64))
        XCTAssertNotEqual(
            OcrLanguageModel.operationKey(for: one), OcrLanguageModel.operationKey(for: two)
        )
    }

    // MARK: Repair

    func testARepairKeepsItsOutcomeAndRefreshes() async {
        let outcome = OcrReconcileOutcome(
            discardedStaging: ["staging-a"],
            discardedUnreferenced: [],
            quarantined: [identity("eng")],
            unrecoverablePinned: [],
            changed: true,
            inventory: inventory([])
        )
        let stub = StubAdministrator(
            statuses: [.success(inventory([english])), .success(inventory([]))],
            repairs: [.success(outcome)]
        )
        let model = OcrLanguageModel(administering: stub)
        await model.load()
        let result = await model.repair()

        XCTAssertEqual(try result.get(), outcome)
        XCTAssertEqual(model.lastRepair, outcome)
        XCTAssertEqual(stub.calls, [.status, .reconcile, .status])
        XCTAssertEqual(model.state, .ready(inventory([])))
    }

    func testAFailedRepairKeepsNoOutcome() async {
        let stub = StubAdministrator(
            statuses: [.success(inventory([english])), .success(inventory([english]))],
            repairs: [.failure(.timedOut)]
        )
        let model = OcrLanguageModel(administering: stub)
        await model.load()
        let result = await model.repair()

        XCTAssertEqual(result.failure, .timedOut)
        XCTAssertNil(model.lastRepair)
    }
}

extension Result {
    fileprivate var failure: Failure? {
        guard case .failure(let error) = self else { return nil }
        return error
    }
}
