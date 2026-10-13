import Foundation
import XCTest
@testable import DocumentIntakeMacOS

final class NativeIntakeCandidateStoreTests: XCTestCase {
    private let digest = "sha256:" + String(repeating: "ab", count: 32)
    private let offerDigest = "sha256:" + String(repeating: "01", count: 32)

    // MARK: Absent is not invalid is not unavailable

    func testAnAbsentRequestIsNilAndNothingElse() throws {
        let store = try makeStore()
        XCTAssertNil(try store.load(nativeRequestID: newID()))
        XCTAssertEqual(try store.nativeRequestIDs(), [])
    }

    func testMalformedOrInconsistentRecordsThrowInsteadOfLookingAbsent() throws {
        let nonce = newID()
        let slot = newID()
        let finder = original(nonce: nonce, slot: slot)
        let tooMany = (0..<11).map { _ in original(nonce: nonce, slot: newID()) }
        let aggregateOverQuota = (0..<5).map { index in
            snapshot(
                nonce: nonce,
                slot: newID(),
                content: "\(index + 1)-alpha.txt",
                state: .adopted,
                digest: digest,
                length: 41 * 1_024 * 1_024
            )
        }
        let sharedGrant = original(nonce: nonce, slot: newID(), lease: "candidate-access-shared")
        let sharedCopy = snapshot(nonce: nonce, slot: newID())
        let cases: [(String, Data)] = [
            ("not json", Data("nope".utf8)),
            ("not utf-8", Data([0xFF, 0xFE, 0x00])),
            ("empty", Data()),
            ("an object, not a list", Data("{}".utf8)),
            ("no records", Data("[]".utf8)),
            ("other schema", try mutated(finder) { $0.replacingOccurrences(of: "intake-candidate.v1", with: "intake-candidate.v2") }),
            ("unknown kind", try mutated(finder) { $0.replacingOccurrences(of: "original_file", with: "folder_root") }),
            ("unknown state", try mutated(finder) { $0.replacingOccurrences(of: "needs_access", with: "frozen") }),
            ("another request's record", try encode([original(nonce: newID(), slot: slot)])),
            ("repeated slot", try encode([finder, finder])),
            ("repeated candidate ref", try encode([
                finder,
                original(nonce: nonce, slot: newID(), candidateRef: finder.candidateRef),
            ])),
            ("more slots than a request may have", try encode(tooMany)),
            ("two slots sharing one grant", try encode([
                original(nonce: nonce, slot: slot, lease: "candidate-access-shared"),
                sharedGrant,
            ])),
            ("two slots sharing one owned copy", try encode([
                snapshot(nonce: nonce, slot: slot),
                sharedCopy,
            ])),
            ("revision zero", try encode([original(nonce: nonce, slot: slot, revision: 0)])),
            ("no display name", try encode([original(nonce: nonce, slot: slot, displayName: "")])),
            ("grant kind with no grant", try encode([original(nonce: nonce, slot: slot, lease: .some(nil))])),
            ("grant kind carrying owned bytes", try encode([original(nonce: nonce, slot: slot, content: "1-a.txt")])),
            ("mixed kinds", try encode([finder, snapshot(nonce: nonce, slot: newID())])),
            ("snapshot kind with no copy", try encode([snapshot(nonce: nonce, slot: slot, content: .some(nil))])),
            ("snapshot copy outside its directory", try encode([snapshot(nonce: nonce, slot: slot, content: "../escape.txt")])),
            ("snapshot copy hidden", try encode([snapshot(nonce: nonce, slot: slot, content: ".hidden")])),
            ("adopted snapshot with no bound digest", try encode([snapshot(nonce: nonce, slot: slot, state: .adopted)])),
            ("digest with no length", try encode([snapshot(nonce: nonce, slot: slot, digest: digest)])),
            ("length with no digest", try encode([snapshot(nonce: nonce, slot: slot, length: 5)])),
            ("malformed digest", try encode([snapshot(nonce: nonce, slot: slot, digest: "sha256:XYZ", length: 5)])),
            ("uppercase digest", try encode([snapshot(nonce: nonce, slot: slot, digest: "sha256:" + String(repeating: "AB", count: 32), length: 5)])),
            ("copy over the item limit", try encode([snapshot(nonce: nonce, slot: slot, digest: digest, length: OwnedSnapshotStore.maximumItemBytes + 1)])),
            ("copies over the selection limit", try encode(aggregateOverQuota)),
            ("copy bound to a grant kind", try encode([original(nonce: nonce, slot: slot, digest: digest, length: 5)])),
        ]
        for (name, bytes) in cases {
            let root = try temporaryDirectory()
            let store = try NativeIntakeCandidateStore(root: root)
            try bytes.write(to: root.appendingPathComponent("\(nonce).json"))

            XCTAssertThrowsError(try store.load(nativeRequestID: nonce), name) {
                XCTAssertEqual($0 as? NativeIntakeCandidateStoreError, .invalidRecord, name)
            }
            // It still counts as a request, so it stays blocked and visible.
            XCTAssertEqual(try store.nativeRequestIDs(), [nonce], name)
            // And it can be neither advanced nor released through the store.
            XCTAssertThrowsError(try store.write([finder]), name)
            XCTAssertThrowsError(try store.release(nativeRequestID: nonce), name)
            XCTAssertEqual(try Data(contentsOf: root.appendingPathComponent("\(nonce).json")), bytes, name)
        }
    }

    func testUnknownRecordFieldsFailClosed() throws {
        let root = try temporaryDirectory()
        let store = try NativeIntakeCandidateStore(root: root)
        let requestID = newID()
        let record = original(nonce: requestID, slot: newID())
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(record)) as? [String: Any]
        )
        object["newerWriterClaim"] = true
        let data = try JSONSerialization.data(withJSONObject: [object], options: [.sortedKeys])
        try data.write(to: root.appendingPathComponent("\(requestID).json"))

        XCTAssertThrowsError(try store.load(nativeRequestID: requestID)) { error in
            XCTAssertEqual(error as? NativeIntakeCandidateStoreError, .invalidRecord)
        }
    }

    func testAnUnreadableRecordIsUnavailableNotAbsent() async throws {
        try XCTSkipIf(getuid() == 0, "permission bits do not bind root")
        let root = try temporaryDirectory()
        let store = try NativeIntakeCandidateStore(root: root)
        let nonce = newID()
        try store.write([original(nonce: nonce, slot: newID())])
        let file = root.appendingPathComponent("\(nonce).json")

        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: file.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path) }
        XCTAssertThrowsError(try store.load(nativeRequestID: nonce)) {
            XCTAssertEqual($0 as? NativeIntakeCandidateStoreError, .unavailable)
        }
        XCTAssertThrowsError(try store.write([original(nonce: nonce, slot: newID())])) {
            XCTAssertEqual($0 as? NativeIntakeCandidateStoreError, .unavailable)
        }
    }

    func testAnUnlistableStoreIsUnavailableNotEmpty() throws {
        try XCTSkipIf(getuid() == 0, "permission bits do not bind root")
        let root = try temporaryDirectory()
        let store = try NativeIntakeCandidateStore(root: root)
        let nonce = newID()
        try store.write([original(nonce: nonce, slot: newID())])

        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: root.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: root.path) }
        XCTAssertThrowsError(try store.nativeRequestIDs()) {
            XCTAssertEqual($0 as? NativeIntakeCandidateStoreError, .unavailable)
        }
        XCTAssertThrowsError(try store.load(nativeRequestID: nonce)) {
            XCTAssertEqual($0 as? NativeIntakeCandidateStoreError, .unavailable)
        }
    }

    // MARK: Records

    func testARecordRoundTripsInOrderAndSurvivesReopening() throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let slots = [newID(), newID(), newID()]
        let reserved = slots.map { original(nonce: nonce, slot: $0) }
        try NativeIntakeCandidateStore(root: root).write(reserved)

        let reopened = try NativeIntakeCandidateStore(root: root)
        XCTAssertEqual(try reopened.load(nativeRequestID: nonce), reserved)
        XCTAssertEqual(try reopened.load(nativeRequestID: nonce)?.map(\.slotID), slots)
        XCTAssertEqual(try reopened.nativeRequestIDs(), [nonce])
        XCTAssertEqual(try permissions(root.appendingPathComponent("\(nonce).json")), 0o600)
        XCTAssertEqual(try permissions(root), 0o700)
    }

    func testNothingExpiresByAge() throws {
        let root = try temporaryDirectory()
        let nonce = newID()
        let store = try NativeIntakeCandidateStore(root: root)
        let reserved = [original(nonce: nonce, slot: newID())]
        try store.write(reserved)
        try store.write(reserved.map { $0.replacing(state: .adopted) })

        // A candidate is bounded by its request and review draft, not a clock.
        let file = root.appendingPathComponent("\(nonce).json")
        try FileManager.default.setAttributes(
            [.modificationDate: Date().addingTimeInterval(-30 * 24 * 60 * 60)],
            ofItemAtPath: file.path
        )
        let reopened = try NativeIntakeCandidateStore(root: root)
        XCTAssertEqual(try reopened.load(nativeRequestID: nonce)?.map(\.state), [.adopted])
        XCTAssertEqual(try reopened.nativeRequestIDs(), [nonce])
    }

    // MARK: Lifecycle only moves forward

    func testLifecycleOnlyMovesForward() throws {
        let store = try makeStore()
        let nonce = newID()
        let slot = newID()
        let reserved = [snapshot(nonce: nonce, slot: slot)]
        let adopted = reserved.map { $0.replacing(state: .adopted, content: (digest, 5)) }
        let released = adopted.map { $0.replacing(state: .released) }

        // A request is created reserved, never already adopted or released.
        XCTAssertThrowsError(try store.write(adopted)) { assertIllegal($0) }
        XCTAssertThrowsError(try store.write(released)) { assertIllegal($0) }
        XCTAssertNil(try store.load(nativeRequestID: nonce))

        try store.write(reserved)
        try store.write(reserved)
        try store.write(adopted)
        try store.write(adopted)
        XCTAssertThrowsError(try store.write(reserved), "adopted cannot become reserved again") { assertIllegal($0) }
        try store.write(released)
        try store.write(released)
        XCTAssertThrowsError(try store.write(adopted), "a released candidate cannot be resurrected") { assertIllegal($0) }
        XCTAssertThrowsError(try store.write(reserved)) { assertIllegal($0) }
        XCTAssertEqual(try store.load(nativeRequestID: nonce)?.map(\.state), [.released])
    }

    func testIdentityAndTheBoundDigestAreWriteOnce() throws {
        let store = try makeStore()
        let nonce = newID()
        let slot = newID()
        let reserved = [snapshot(nonce: nonce, slot: slot)]
        try store.write(reserved)

        let renamed = [snapshot(nonce: nonce, slot: slot, displayName: "other.txt")]
        let rebased = [snapshot(nonce: nonce, slot: slot, candidateRef: "nic-other")]
        let rerevised = [snapshot(nonce: nonce, slot: slot, revision: 2)]
        let moved = [snapshot(nonce: nonce, slot: slot, content: "2-other.txt")]
        let fewer = [snapshot(nonce: nonce, slot: slot), snapshot(nonce: nonce, slot: newID())]
        for (name, changed) in [("name", renamed), ("ref", rebased), ("revision", rerevised), ("content", moved), ("slots", fewer)] {
            XCTAssertThrowsError(try store.write(changed), name) { assertIllegal($0) }
        }

        try store.write(reserved.map { $0.replacing(state: .adopted, content: (digest, 5)) })
        let otherDigest = "sha256:" + String(repeating: "cd", count: 32)
        XCTAssertThrowsError(
            try store.write(reserved.map { $0.replacing(state: .adopted, content: (otherDigest, 5)) }),
            "a bound digest cannot be rebound"
        ) { assertIllegal($0) }
        XCTAssertThrowsError(
            try store.write(reserved.map { $0.replacing(state: .adopted, content: (digest, 6)) }),
            "a bound length cannot be rebound"
        ) { assertIllegal($0) }
        XCTAssertEqual(try store.load(nativeRequestID: nonce)?.first?.ownedContentDigest, digest)
    }

    func testReleaseIsIdempotentAndCanTargetOneCandidate() throws {
        let store = try makeStore()
        let nonce = newID()
        let slots = [newID(), newID()]
        let reserved = slots.map { original(nonce: nonce, slot: $0) }
        try store.write(reserved)
        try store.write(reserved.map { $0.replacing(state: .adopted) })

        try store.release(nativeRequestID: nonce, candidateRefs: [reserved[1].candidateRef])
        try store.release(nativeRequestID: nonce, candidateRefs: [reserved[1].candidateRef])
        XCTAssertEqual(try store.load(nativeRequestID: nonce)?.map(\.state), [.adopted, .released])

        try store.release(nativeRequestID: nonce)
        try store.release(nativeRequestID: nonce)
        XCTAssertEqual(try store.load(nativeRequestID: nonce)?.map(\.state), [.released, .released])

        // Releasing a request that has no record is not an error. Released
        // records remain as replay tombstones and have no public deletion path.
        try store.release(nativeRequestID: newID())
        XCTAssertEqual(try store.load(nativeRequestID: nonce)?.map(\.state), [.released, .released])
    }

    func testAReleasedNeedsAccessReservationIsAllowed() throws {
        // An orphaned reservation is released without ever having been adopted.
        let store = try makeStore()
        let nonce = newID()
        try store.write([original(nonce: nonce, slot: newID())])
        try store.release(nativeRequestID: nonce)
        XCTAssertEqual(try store.load(nativeRequestID: nonce)?.map(\.state), [.released])
    }

    // MARK: Helpers

    private func original(
        nonce: String,
        slot: String,
        candidateRef: String? = nil,
        revision: UInt64 = 1,
        displayName: String = "brief.pdf",
        lease: String?? = nil,
        content: String? = nil,
        state: NativeIntakeCandidateState = .needsAccess,
        digest: String? = nil,
        length: UInt64? = nil
    ) -> NativeIntakeCandidateV1 {
        NativeIntakeCandidateV1(
            nativeRequestID: nonce,
            offerDigest: offerDigest,
            slotID: slot,
            candidateRef: candidateRef ?? "nic-\(nonce)-\(slot)",
            candidateRevision: revision,
            kind: .originalFile,
            nativeAccessLeaseRef: lease ?? "candidate-access-\(nonce)-\(slot)",
            ownedContentRef: content,
            ownedContentDigest: digest,
            ownedContentByteLength: length,
            displayName: displayName,
            state: state
        )
    }

    private func snapshot(
        nonce: String,
        slot: String,
        candidateRef: String? = nil,
        revision: UInt64 = 1,
        displayName: String = "alpha.txt",
        content: String?? = nil,
        state: NativeIntakeCandidateState = .needsAccess,
        digest: String? = nil,
        length: UInt64? = nil
    ) -> NativeIntakeCandidateV1 {
        NativeIntakeCandidateV1(
            nativeRequestID: nonce,
            offerDigest: offerDigest,
            slotID: slot,
            candidateRef: candidateRef ?? "nic-\(nonce)-\(slot)",
            candidateRevision: revision,
            kind: .ownedSnapshot,
            ownedContentRef: content ?? "1-alpha.txt",
            ownedContentDigest: digest,
            ownedContentByteLength: length,
            displayName: displayName,
            state: state
        )
    }

    private func encode(_ records: [NativeIntakeCandidateV1]) throws -> Data {
        try JSONEncoder().encode(records)
    }

    private func mutated(
        _ record: NativeIntakeCandidateV1,
        _ change: (String) -> String
    ) throws -> Data {
        Data(change(String(decoding: try encode([record]), as: UTF8.self)).utf8)
    }

    private func assertIllegal(
        _ error: Error,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertEqual(error as? NativeIntakeCandidateStoreError, .illegalTransition, file: file, line: line)
    }

    private func makeStore() throws -> NativeIntakeCandidateStore {
        try NativeIntakeCandidateStore(root: try temporaryDirectory())
    }

    private func permissions(_ url: URL) throws -> Int {
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        return (attributes[.posixPermissions] as? NSNumber)?.intValue ?? -1
    }

    private func newID() -> String { UUID().uuidString.lowercased() }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock {
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
            try? FileManager.default.removeItem(at: url)
        }
        return url
    }
}
