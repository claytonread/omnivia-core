// Administering the managed OCR language resources from the settings window.
//
// The documents below are the shapes `omnivia-core-service --ocr-resources`
// prints, as its own acceptance tests pin them (`sort_keys=True`, so the keys
// are in the order Python writes them).
//
// The process is injected. What is being checked is the argument vector, the
// bounds, and that a document this build does not fully understand is refused
// rather than half-read — none of which needs a service on the machine.

import Foundation
import XCTest

@testable import CoreSettingsMacOS

// MARK: - Fixtures

private let digest = "ea7f0e3fd9d5b0f69e8a31e1a0b5cdc2a4bb1c84b0e4a1d1b7a8c4c0b6c6d2f1"
private let otherDigest = "1f2d6c6b0c4c8a7b1d1a4e0b481cbb4a2cdcb5a0e1318a9e6f0b5d9df3e0f7ae"

private let installationState = URL(fileURLWithPath: "/Users/someone/Library/omnivia/state")

private func statusDocument(
    resources: String = "[]", installed: Int = 0, available: Int = 536_870_912
) -> String {
    """
    {"action": "status", "availableBytes": \(available), "capacityBytes": 536870912, \
    "installedBytes": \(installed), "ocr_resource_result_version": 1, \
    "resources": \(resources), "status": "accepted"}
    """
}

private let englishResource = """
[{"bytesPresent": true, "identity": {"byteSize": 4113088, "engineVersion": "5.5.3", \
"language": "eng", "modelVersion": "tessdata_fast-4.1.0", "sha256": "\(digest)"}, \
"pinned": false, "pinnedBy": []}]
"""

private let pinnedResource = """
[{"bytesPresent": true, "identity": {"byteSize": 4113088, "engineVersion": "5.5.3", \
"language": "eng", "modelVersion": "tessdata_fast-4.1.0", "sha256": "\(digest)"}, \
"pinned": true, "pinnedBy": ["op-7", "op-9"]}]
"""

private let englishIdentity = OcrResourceIdentity(
    language: "eng",
    engineVersion: "5.5.3",
    modelVersion: "tessdata_fast-4.1.0",
    byteSize: 4_113_088,
    sha256: digest
)

// MARK: - The injected process

/// Records every invocation and replays canned results in order.
private final class RecordingProcessRunner: OcrProcessRunning, @unchecked Sendable {
    struct Invocation: Equatable {
        let executable: URL
        let arguments: [String]
        let timeout: TimeInterval
        let stdoutLimit: Int
        let stderrLimit: Int
    }

    private(set) var invocations: [Invocation] = []
    private var queued: [Result<OcrProcessResult, OcrProcessFailure>]

    init(_ queued: [Result<OcrProcessResult, OcrProcessFailure>]) {
        self.queued = queued
    }

    convenience init(stdout: String, code: Int32 = 0, stderr: String = "", truncated: Bool = false) {
        self.init([.success(OcrProcessResult(
            stdout: stdout, stderr: stderr, code: code, truncated: truncated
        ))])
    }

    func run(
        executable: URL,
        arguments: [String],
        timeout: TimeInterval,
        stdoutLimit: Int,
        stderrLimit: Int
    ) async -> Result<OcrProcessResult, OcrProcessFailure> {
        invocations.append(Invocation(
            executable: executable,
            arguments: arguments,
            timeout: timeout,
            stdoutLimit: stdoutLimit,
            stderrLimit: stderrLimit
        ))
        guard !queued.isEmpty else {
            return .success(OcrProcessResult(stdout: "", stderr: "", code: 70, truncated: false))
        }
        return queued.removeFirst()
    }
}

/// The installation state is absolute, so the client always exists here; the
/// nil case has its own test.
private func runner(
    _ process: RecordingProcessRunner,
    executable: String = "/usr/local/bin/omnivia-core-service"
) -> OcrResourceCommandRunner {
    OcrResourceCommandRunner(
        executable: URL(fileURLWithPath: executable),
        installationState: installationState,
        process: process
    )!
}

// MARK: - Tests

final class OcrResourceAdministrationTests: XCTestCase {
    // MARK: Argument construction

    /// The whole vector, pinned. `--workspace` is required by the service's
    /// shared parser and never read by this mode, so it carries the one
    /// absolute path this client holds rather than an invented workspace.
    func testStatusRunsTheServiceWithTheExactArgumentVector() async throws {
        let process = RecordingProcessRunner(stdout: statusDocument())
        _ = await runner(process).status()

        let invocation = try XCTUnwrap(process.invocations.first)
        XCTAssertEqual(process.invocations.count, 1)
        XCTAssertEqual(invocation.executable.path, "/usr/local/bin/omnivia-core-service")
        XCTAssertEqual(invocation.arguments, [
            "--workspace", installationState.path,
            "--installation-state", installationState.path,
            "--ocr-resources", "status",
        ])
    }

    func testRemovePassesTheWholeIdentity() async throws {
        let removed = """
        {"action": "remove", "availableBytes": 536870912, "capacityBytes": 536870912, \
        "identity": {"byteSize": 4113088, "engineVersion": "5.5.3", "language": "eng", \
        "modelVersion": "tessdata_fast-4.1.0", "sha256": "\(digest)"}, \
        "installedBytes": 0, "ocr_resource_result_version": 1, "removed": true, \
        "resources": [], "status": "accepted"}
        """
        let process = RecordingProcessRunner(stdout: removed)
        let result = await runner(process).remove(englishIdentity)

        let invocation = try XCTUnwrap(process.invocations.first)
        XCTAssertEqual(invocation.arguments, [
            "--workspace", installationState.path,
            "--installation-state", installationState.path,
            "--ocr-resources", "remove",
            "--ocr-language", "eng",
            "--ocr-engine-version", "5.5.3",
            "--ocr-model-version", "tessdata_fast-4.1.0",
            "--ocr-byte-size", "4113088",
            "--ocr-sha256", digest,
        ])
        XCTAssertEqual(try result.get().resources, [])
    }

    /// The capability stays ready for a fully bound descriptor, and the
    /// payload is still an absolute path the client checks before spawning.
    func testInstallPassesThePayloadAndRefusesARelativeOne() async throws {
        let installed = """
        {"action": "install", "availableBytes": 532757824, "capacityBytes": 536870912, \
        "identity": {"byteSize": 4113088, "engineVersion": "5.5.3", "language": "eng", \
        "modelVersion": "tessdata_fast-4.1.0", "sha256": "\(digest)"}, \
        "installedBytes": 4113088, "newlyInstalled": true, \
        "ocr_resource_result_version": 1, "resources": \(englishResource), \
        "status": "accepted"}
        """
        let process = RecordingProcessRunner(stdout: installed)
        let client = runner(process)
        let result = await client.install(
            payload: URL(fileURLWithPath: "/tmp/downloads/eng.traineddata"),
            identity: englishIdentity
        )
        XCTAssertEqual(try result.get().installedBytes, 4_113_088)
        let invocation = try XCTUnwrap(process.invocations.first)
        XCTAssertEqual(
            Array(invocation.arguments[4...7]),
            ["--ocr-resources", "install", "--ocr-payload", "/tmp/downloads/eng.traineddata"]
        )

        // `URL(fileURLWithPath:)` always resolves to an absolute path, so a
        // genuinely relative one has to arrive as a plain URL.
        let relative = await client.install(
            payload: try XCTUnwrap(URL(string: "eng.traineddata")),
            identity: englishIdentity
        )
        // Nothing was spawned for the relative one.
        XCTAssertEqual(process.invocations.count, 1)
        guard case .failure(.usageFault) = relative else {
            return XCTFail("a relative payload must be a usage fault, got \(relative)")
        }
    }

    /// The client refuses to exist at all without an absolute installation
    /// state — the service rejects a relative one, and a client that cannot
    /// name the cache it administers has nothing truthful to show.
    func testARelativeInstallationStateHasNoClient() throws {
        let service = URL(fileURLWithPath: "/usr/local/bin/omnivia-core-service")
        XCTAssertNil(OcrResourceCommandRunner(
            executable: service,
            installationState: try XCTUnwrap(URL(string: "state"))
        ))
        XCTAssertNil(OcrResourceCommandRunner(
            executable: service,
            installationState: try XCTUnwrap(URL(string: "https://example.invalid/state"))
        ))
        XCTAssertNil(OcrResourceCommandRunner(
            executable: try XCTUnwrap(URL(string: "omnivia-core-service")),
            installationState: installationState
        ))
    }

    // MARK: Bounds

    func testEveryActionIsBoundedInTimeAndOutput() async throws {
        let process = RecordingProcessRunner([
            .success(OcrProcessResult(stdout: statusDocument(), stderr: "", code: 0, truncated: false)),
            .success(OcrProcessResult(stdout: "", stderr: "", code: 1, truncated: false)),
        ])
        let client = runner(process)
        _ = await client.status()
        _ = await client.reconcile()

        for invocation in process.invocations {
            XCTAssertEqual(invocation.stdoutLimit, 1 << 20)
            XCTAssertEqual(invocation.stderrLimit, 64 << 10)
            XCTAssertGreaterThan(invocation.timeout, 0)
        }
        XCTAssertEqual(process.invocations[0].timeout, OcrResourceCommandRunner.statusTimeout)
        XCTAssertEqual(process.invocations[1].timeout, OcrResourceCommandRunner.reconcileTimeout)
    }

    /// A document that hit the bound is not parsed: half a JSON object is not
    /// a smaller inventory.
    func testTruncatedOutputIsNotRead() async throws {
        let process = RecordingProcessRunner(stdout: statusDocument(), truncated: true)
        let result = await runner(process).status()
        XCTAssertEqual(result.failure, .outputTooLarge)
    }

    func testASpawnFailureIsUnavailableAndATimeoutIsATimeout() async throws {
        let dead = await runner(RecordingProcessRunner([.failure(.spawnFailed)])).status()
        XCTAssertEqual(dead.failure, .unavailable)
        let slow = await runner(RecordingProcessRunner([.failure(.timedOut)])).status()
        XCTAssertEqual(slow.failure, .timedOut)
    }

    // MARK: Status and capacity projection

    func testStatusProjectsIdentitiesPinsAndCapacity() async throws {
        let process = RecordingProcessRunner(stdout: statusDocument(
            resources: pinnedResource, installed: 4_113_088, available: 532_757_824
        ))
        let inventory = try await runner(process).status().get()

        XCTAssertEqual(inventory.capacityBytes, 536_870_912)
        XCTAssertEqual(inventory.installedBytes, 4_113_088)
        XCTAssertEqual(inventory.availableBytes, 532_757_824)
        let resource = try XCTUnwrap(inventory.resources.first)
        XCTAssertEqual(resource.identity, englishIdentity)
        XCTAssertEqual(resource.identity.displayLanguage, "English")
        XCTAssertTrue(resource.pinned)
        XCTAssertEqual(resource.pinnedBy, ["op-7", "op-9"])
        XCTAssertTrue(resource.bytesPresent)
        XCTAssertTrue(inventory.needingRepair.isEmpty)
    }

    func testAnEmptyCacheIsEmptyAndNotEnglish() async throws {
        let process = RecordingProcessRunner(stdout: statusDocument())
        let inventory = try await runner(process).status().get()
        XCTAssertEqual(inventory.resources, [])
        XCTAssertEqual(inventory.installedBytes, 0)
    }

    func testAResourceWhoseBytesAreMissingNeedsRepair() async throws {
        let divergent = """
        [{"bytesPresent": false, "identity": {"byteSize": 4113088, \
        "engineVersion": "5.5.3", "language": "eng", \
        "modelVersion": "tessdata_fast-4.1.0", "sha256": "\(digest)"}, \
        "pinned": false, "pinnedBy": []}]
        """
        let process = RecordingProcessRunner(stdout: statusDocument(
            resources: divergent, installed: 4_113_088, available: 532_757_824
        ))
        let inventory = try await runner(process).status().get()
        XCTAssertEqual(inventory.needingRepair.count, 1)
    }

    /// The exact byte count leads, because §18.4 reserves against a byte cap.
    func testBytesAreDescribedExactlyThenRounded() {
        XCTAssertEqual(
            OcrResourceInventory.describeBytes(4_113_088), "4,113,088 bytes (3.9 MiB)"
        )
        XCTAssertEqual(OcrResourceInventory.describeBytes(0), "0 bytes")
        XCTAssertEqual(
            OcrResourceInventory.describeBytes(536_870_912), "536,870,912 bytes (512.0 MiB)"
        )
    }

    // MARK: Refusals

    func testAFullCacheIsReportedWithItsStableCode() async throws {
        let refusal = """
        {"action": "install", "code": "language_storage_full", \
        "ocr_resource_result_version": 1, \
        "reason": "installing eng would exceed the managed OCR resource cap of 536870912 \
        bytes; 4113088 free", "status": "refused"}
        """
        let process = RecordingProcessRunner(stdout: refusal, code: 1, stderr: "language_storage_full: …")
        let result = await runner(process).install(
            payload: URL(fileURLWithPath: "/tmp/eng.traineddata"), identity: englishIdentity
        )
        guard case .refused(let code, let reason)? = result.failure else {
            return XCTFail("expected a refusal, got \(String(describing: result.failure))")
        }
        XCTAssertEqual(code, OcrResourceError.storageFull)
        XCTAssertTrue(reason.contains("536870912"))
    }

    /// A pinned set is refused by the service as well as hidden from removal
    /// by the pane. The code, not the sentence, is what the window branches on.
    func testRemovingAPinnedSetIsRefusedWithThePinnedCode() async throws {
        let refusal = """
        {"action": "remove", "code": "ocr_resource_pinned", \
        "ocr_resource_result_version": 1, \
        "reason": "eng is pinned by op-7, op-9", "status": "refused"}
        """
        let process = RecordingProcessRunner(stdout: refusal, code: 1)
        let result = await runner(process).remove(englishIdentity)
        XCTAssertEqual(
            result.failure,
            .refused(code: "ocr_resource_pinned", reason: "eng is pinned by op-7, op-9")
        )
    }

    /// Exit 2 is the caller's fault and arrives on stderr, before any path is
    /// opened. It is not a refusal and must not be shown as one.
    func testAUsageFaultIsNotARefusal() async throws {
        let process = RecordingProcessRunner(
            stdout: "", code: 2, stderr: "the resource identity needs --ocr-sha256\n"
        )
        let result = await runner(process).remove(englishIdentity)
        XCTAssertEqual(
            result.failure, .usageFault("the resource identity needs --ocr-sha256")
        )
    }

    func testAnUnexpectedExitStatusIsAProcessFailure() async throws {
        let process = RecordingProcessRunner(stdout: "", code: 70)
        let result = await runner(process).status()
        XCTAssertEqual(result.failure, .processFailed(code: 70))
    }

    // MARK: Reconcile

    func testReconcileReportsWhatItDid() async throws {
        let document = """
        {"action": "reconcile", "availableBytes": 536870912, "capacityBytes": 536870912, \
        "changed": true, "discardedStaging": ["staging-a"], \
        "discardedUnreferenced": ["resources/orphan"], "installedBytes": 0, \
        "ocr_resource_result_version": 1, \
        "quarantined": [{"byteSize": 4113088, "engineVersion": "5.5.3", "language": "eng", \
        "modelVersion": "tessdata_fast-4.1.0", "sha256": "\(digest)"}], "resources": [], \
        "status": "accepted", \
        "unrecoverablePinned": [{"byteSize": 10, "engineVersion": "5.5.3", \
        "language": "deu", "modelVersion": "tessdata_fast-4.1.0", \
        "sha256": "\(otherDigest)"}]}
        """
        let process = RecordingProcessRunner(stdout: document)
        let outcome = try await runner(process).reconcile().get()

        XCTAssertTrue(outcome.changed)
        XCTAssertEqual(outcome.discardedStaging, ["staging-a"])
        XCTAssertEqual(outcome.discardedUnreferenced, ["resources/orphan"])
        XCTAssertEqual(outcome.quarantined, [englishIdentity])
        XCTAssertEqual(outcome.unrecoverablePinned.map(\.language), ["deu"])
        XCTAssertEqual(outcome.inventory.resources, [])
    }

    // MARK: Strict decoding

    func testANewerDocumentVersionIsRefusedRatherThanRead() {
        let newer = statusDocument().replacingOccurrences(
            of: "\"ocr_resource_result_version\": 1", with: "\"ocr_resource_result_version\": 2"
        )
        let decoded = OcrResourceCommandRunner.Decoding.inventory(newer, action: "status")
        XCTAssertEqual(decoded?.failure, .unsupportedSchema(2))
    }

    func testAnExtraFieldMakesTheDocumentUnreadable() {
        let extra = statusDocument().replacingOccurrences(
            of: "\"status\": \"accepted\"", with: "\"status\": \"accepted\", \"evictedBytes\": 1"
        )
        XCTAssertNil(OcrResourceCommandRunner.Decoding.inventory(extra, action: "status"))
    }

    func testAMissingFieldMakesTheDocumentUnreadable() {
        let missing = """
        {"action": "status", "capacityBytes": 536870912, "installedBytes": 0, \
        "ocr_resource_result_version": 1, "resources": [], "status": "accepted"}
        """
        XCTAssertNil(OcrResourceCommandRunner.Decoding.inventory(missing, action: "status"))
    }

    /// The document says which action it answered. A `remove` document read as
    /// a `status` would report the inventory of a different call.
    func testTheActionMustMatchTheOneAskedFor() {
        XCTAssertNil(OcrResourceCommandRunner.Decoding.inventory(
            statusDocument(), action: "remove"
        ))
    }

    func testADocumentThatIsNotAResultIsNotADocument() {
        for text in ["", "not json", "[]", "{\"hosts\": []}", "{\"ocr_resource_result_version\": \"1\"}"] {
            XCTAssertNil(
                OcrResourceCommandRunner.Decoding.inventory(text, action: "status"),
                "unexpectedly decoded \(text)"
            )
        }
    }

    /// `NSNumber` bridges integers and booleans both ways, so without the type
    /// check `"pinned": 1` would silently satisfy a boolean field.
    func testAnIntegerIsNotABoolean() {
        let numeric = statusDocument(
            resources: englishResource.replacingOccurrences(
                of: "\"pinned\": false", with: "\"pinned\": 0"
            ),
            installed: 4_113_088,
            available: 532_757_824
        )
        XCTAssertNil(OcrResourceCommandRunner.Decoding.inventory(numeric, action: "status"))
        XCTAssertNil(OcrResourceCommandRunner.Decoding.boolean(1 as NSNumber))
        XCTAssertEqual(OcrResourceCommandRunner.Decoding.boolean(true as NSNumber), true)
        XCTAssertNil(OcrResourceCommandRunner.Decoding.integer(true as NSNumber))
        XCTAssertNil(OcrResourceCommandRunner.Decoding.integer(1.5 as NSNumber))
        XCTAssertEqual(OcrResourceCommandRunner.Decoding.integer(7 as NSNumber), 7)
    }

    /// A row that claims no pin while naming the operations holding one is not
    /// a row to render — either half could be the one the pane acts on.
    func testAContradictoryPinIsRejected() {
        let contradictory = statusDocument(
            resources: englishResource.replacingOccurrences(
                of: "\"pinnedBy\": []", with: "\"pinnedBy\": [\"op-7\"]"
            ),
            installed: 4_113_088,
            available: 532_757_824
        )
        XCTAssertNil(OcrResourceCommandRunner.Decoding.inventory(contradictory, action: "status"))
    }

    // MARK: Forged data

    /// The identity goes back out as arguments, so it is accepted only in a
    /// bounded token shape. A language carrying a flag, a path separator, a
    /// NUL or whitespace never becomes `--ocr-language`'s value.
    func testForgedIdentityFieldsAreRejected() {
        let forged = [
            "--ocr-payload",
            "/etc/passwd",
            "../../etc/passwd",
            "eng --ocr-payload /tmp/x",
            "eng\u{0000}",
            "eng\n--ocr-sha256",
            String(repeating: "e", count: 65),
            "",
        ]
        for language in forged {
            let document = statusDocument(
                resources: englishResource.replacingOccurrences(
                    of: "\"language\": \"eng\"",
                    with: "\"language\": \(encode(language))"
                ),
                installed: 4_113_088,
                available: 532_757_824
            )
            XCTAssertNil(
                OcrResourceCommandRunner.Decoding.inventory(document, action: "status"),
                "a language of \(encode(language)) must not decode"
            )
        }
    }

    /// A digest is 64 lowercase hex digits. Anything else is not the identity
    /// the service verified against.
    func testForgedDigestsAreRejected() {
        for value in [
            String(repeating: "A", count: 64),      // upper case
            String(repeating: "f", count: 63),      // short
            String(repeating: "f", count: 65),      // long
            "/tmp/" + String(repeating: "f", count: 59),
        ] {
            let document = statusDocument(
                resources: englishResource.replacingOccurrences(
                    of: "\"sha256\": \"\(digest)\"", with: "\"sha256\": \(encode(value))"
                ),
                installed: 4_113_088,
                available: 532_757_824
            )
            XCTAssertNil(
                OcrResourceCommandRunner.Decoding.inventory(document, action: "status"),
                "a digest of \(value) must not decode"
            )
        }
    }

    /// A negative or fractional byte size is not a size the cap can be
    /// reserved against.
    func testForgedByteSizesAreRejected() {
        for value in ["-1", "1.5", "\"4113088\""] {
            let document = statusDocument(
                resources: englishResource.replacingOccurrences(
                    of: "\"byteSize\": 4113088", with: "\"byteSize\": \(value)"
                ),
                installed: 4_113_088,
                available: 532_757_824
            )
            XCTAssertNil(
                OcrResourceCommandRunner.Decoding.inventory(document, action: "status"),
                "a byte size of \(value) must not decode"
            )
        }
    }

    // MARK: The unbound postures

    /// §14.5 installs from a trusted manifest held by the managed resource
    /// owner. This build has none, and the reason says exactly that rather
    /// than "not in this build".
    func testInstallIsUnavailableBecauseNothingBindsARelease() {
        let reason = OcrInstallAvailability.unavailableNoTrustedCatalogue.reason
        XCTAssertTrue(reason.contains("signed resource catalogue"))
        XCTAssertTrue(reason.contains("no downloader"))
        XCTAssertTrue(reason.contains("typed by hand"))
    }

    // MARK: The production runner

    /// The real runner takes an argument vector, so nothing in it is parsed by
    /// a shell: the metacharacters below arrive as one argument.
    func testTheSystemRunnerPassesArgumentsWithoutAShell() async throws {
        let result = try await SystemOcrProcessRunner().run(
            executable: URL(fileURLWithPath: "/bin/echo"),
            arguments: ["a b; rm -rf /", "$(whoami)"],
            timeout: 10,
            stdoutLimit: 1 << 16,
            stderrLimit: 1 << 16
        ).get()
        XCTAssertEqual(result.code, 0)
        XCTAssertEqual(result.stdout, "a b; rm -rf / $(whoami)\n")
        XCTAssertFalse(result.truncated)
    }

    func testTheSystemRunnerCapsOutput() async throws {
        let result = try await SystemOcrProcessRunner().run(
            executable: URL(fileURLWithPath: "/bin/dd"),
            arguments: ["if=/dev/zero", "bs=1024", "count=64"],
            timeout: 20,
            stdoutLimit: 1024,
            stderrLimit: 1 << 16
        ).get()
        XCTAssertTrue(result.truncated)
        XCTAssertEqual(result.stdout.utf8.count, 1024)
    }

    func testTheSystemRunnerStopsAChildThatOverrunsItsTime() async {
        let result = await SystemOcrProcessRunner().run(
            executable: URL(fileURLWithPath: "/bin/sleep"),
            arguments: ["30"],
            timeout: 0.5,
            stdoutLimit: 1 << 16,
            stderrLimit: 1 << 16
        )
        guard case .failure(.timedOut) = result else {
            return XCTFail("expected a timeout, got \(result)")
        }
    }

    func testTheSystemRunnerReportsAMissingExecutable() async {
        let result = await SystemOcrProcessRunner().run(
            executable: URL(fileURLWithPath: "/nonexistent/omnivia-core-service"),
            arguments: [],
            timeout: 5,
            stdoutLimit: 1 << 16,
            stderrLimit: 1 << 16
        )
        guard case .failure(.spawnFailed) = result else {
            return XCTFail("expected a spawn failure, got \(result)")
        }
    }

    private func encode(_ value: String) -> String {
        let data = try? JSONSerialization.data(
            withJSONObject: [value], options: [.fragmentsAllowed]
        )
        let text = data.map { String(decoding: $0, as: UTF8.self) } ?? "\"\""
        return String(text.dropFirst().dropLast())
    }
}

// MARK: - Result conveniences

extension Result {
    fileprivate var failure: Failure? {
        guard case .failure(let error) = self else { return nil }
        return error
    }
}
