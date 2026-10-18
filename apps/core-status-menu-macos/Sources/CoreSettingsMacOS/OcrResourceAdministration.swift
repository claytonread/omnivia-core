// ============================================================================
// Managed OCR language-resource administration, as the Document processing
// pane needs it.
//
// This is the native half of §14.5's language-resource flow. The owner-local
// authority is `omnivia-core-service --ocr-resources status|install|remove|
// reconcile`, which prints one versioned JSON document on stdout and an
// operator sentence on stderr. This window runs that executable and renders
// what comes back; it owns no cache, no state document and no capacity rule.
//
// Four things about that contract shape the code below:
//
// * Exit 0 is an accepted action, 1 a refusal carrying the lifecycle's stable
//   `code` (`language_storage_full`, `ocr_resource_pinned`, …) and 2 a usage
//   fault reported on stderr before any path is opened. They are three
//   different outcomes for the pane, not one failure.
// * The document is versioned. A version this build does not know is reported
//   as such rather than read with the fields that happen to match — a newer
//   service may mean something else by the same key.
// * Nothing decoded here may choose an executable, a path or an argument. The
//   executable and the installation-state root are fixed when this type is
//   built; identities are accepted only in a bounded token shape, so a forged
//   status document cannot smuggle a flag or a path back into the next call.
// * There is no catalogue, no downloader and no release authority in this
//   repository, so `install` is reachable in code and unreachable in the
//   window. See `OcrInstallAvailability`.
// ============================================================================

import Darwin
import Foundation

// MARK: - Model

/// One managed resource set, identified by the whole tuple. A partial identity
/// is never completed by a default — the service refuses one, and so does this.
public struct OcrResourceIdentity: Sendable, Equatable {
    public let language: String
    public let engineVersion: String
    public let modelVersion: String
    public let byteSize: Int
    public let sha256: String

    public init(
        language: String, engineVersion: String, modelVersion: String,
        byteSize: Int, sha256: String
    ) {
        self.language = language
        self.engineVersion = engineVersion
        self.modelVersion = modelVersion
        self.byteSize = byteSize
        self.sha256 = sha256
    }

    /// The recognition languages this build can name. A code absent from the
    /// table renders as itself: inventing a language name for an unknown code
    /// would be a claim about bytes nobody here has read.
    private static let names: [String: String] = [
        "eng": "English",
        "fra": "French",
        "deu": "German",
        "spa": "Spanish",
        "ita": "Italian",
        "por": "Portuguese",
        "nld": "Dutch",
        "jpn": "Japanese",
        "kor": "Korean",
        "rus": "Russian",
        "ara": "Arabic",
        "chi_sim": "Chinese (Simplified)",
        "chi_tra": "Chinese (Traditional)",
    ]

    public var displayLanguage: String { Self.displayName(for: language) }

    /// The same table, for a bare language code — a selected language is a
    /// code and has no installed identity behind it until it is installed.
    public static func displayName(for language: String) -> String {
        names[language] ?? language
    }
}

/// One installed resource set as `status` reports it.
public struct OcrInstalledResource: Sendable, Equatable {
    public let identity: OcrResourceIdentity
    public let pinned: Bool
    /// The operations pinning it. Pinned bytes may not be removed (§18.4).
    public let pinnedBy: [String]
    /// False when the state document claims this identity but its bytes are
    /// missing or no longer match — the repair pass is `reconcile`.
    public let bytesPresent: Bool

    public init(
        identity: OcrResourceIdentity, pinned: Bool, pinnedBy: [String], bytesPresent: Bool
    ) {
        self.identity = identity
        self.pinned = pinned
        self.pinnedBy = pinnedBy
        self.bytesPresent = bytesPresent
    }
}

/// The managed cache as the service reports it. The capacity is read from the
/// document rather than assumed: 512 MiB is the service's figure to state.
public struct OcrResourceInventory: Sendable, Equatable {
    public let capacityBytes: Int
    public let installedBytes: Int
    public let availableBytes: Int
    public let resources: [OcrInstalledResource]

    public init(
        capacityBytes: Int, installedBytes: Int, availableBytes: Int,
        resources: [OcrInstalledResource]
    ) {
        self.capacityBytes = capacityBytes
        self.installedBytes = installedBytes
        self.availableBytes = availableBytes
        self.resources = resources
    }

    /// Any installed identity whose bytes the service could not confirm.
    public var needingRepair: [OcrInstalledResource] { resources.filter { !$0.bytesPresent } }

    /// Exact bytes first, with a rounded figure after it. The exact number is
    /// the contract — §18.4 reserves against a byte cap, not against megabytes.
    public static func describeBytes(_ bytes: Int) -> String {
        let grouped = groupingFormatter.string(from: NSNumber(value: bytes)) ?? "\(bytes)"
        guard bytes >= 1024 else { return "\(grouped) bytes" }
        let mib = Double(bytes) / 1_048_576
        return String(format: "%@ bytes (%.1f MiB)", grouped, mib)
    }

    /// Fixed grouping, not the viewer's locale: this number is pinned by tests
    /// and read beside a digest, where a locale-dependent separator is noise.
    private static let groupingFormatter: NumberFormatter = {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.groupingSeparator = ","
        formatter.usesGroupingSeparator = true
        return formatter
    }()
}

/// What `reconcile` repaired. Every field is reported as returned — a repair
/// that quarantined bytes says so, and one that changed nothing says that.
public struct OcrReconcileOutcome: Sendable, Equatable {
    public let discardedStaging: [String]
    public let discardedUnreferenced: [String]
    public let quarantined: [OcrResourceIdentity]
    public let unrecoverablePinned: [OcrResourceIdentity]
    public let changed: Bool
    public let inventory: OcrResourceInventory

    public init(
        discardedStaging: [String], discardedUnreferenced: [String],
        quarantined: [OcrResourceIdentity], unrecoverablePinned: [OcrResourceIdentity],
        changed: Bool, inventory: OcrResourceInventory
    ) {
        self.discardedStaging = discardedStaging
        self.discardedUnreferenced = discardedUnreferenced
        self.quarantined = quarantined
        self.unrecoverablePinned = unrecoverablePinned
        self.changed = changed
        self.inventory = inventory
    }
}

/// Why the three outcomes of a run are not one error.
public enum OcrResourceError: Error, Sendable, Equatable {
    /// No packaged service executable to run, or it could not be started.
    case unavailable
    case timedOut
    /// stdout or stderr exceeded its bound; a truncated document is not read.
    case outputTooLarge
    /// Exit 0 or 1 but not the document this build understands.
    case unreadableOutput
    /// A result document of a version this build does not know.
    case unsupportedSchema(Int)
    /// Exit 2: the arguments were wrong. The service's sentence, for the log.
    case usageFault(String)
    /// Exit 1: the action was refused, with the lifecycle's stable code.
    case refused(code: String, reason: String)
    case processFailed(code: Int32)

    /// The refusal codes the pane branches on by name rather than by sentence.
    public static let storageFull = "language_storage_full"
    public static let pinned = "ocr_resource_pinned"
    public static let notInstalled = "ocr_resource_not_installed"
    public static let divergent = "ocr_resource_state_divergent"
}

public protocol OcrResourceAdministering: Sendable {
    func status() async -> Result<OcrResourceInventory, OcrResourceError>
    func remove(_ identity: OcrResourceIdentity) async -> Result<OcrResourceInventory, OcrResourceError>
    func reconcile() async -> Result<OcrReconcileOutcome, OcrResourceError>
    /// Reachable in code and unreachable in the window. The payload is a file
    /// a *trusted* catalogue already downloaded and verified; this build has no
    /// such catalogue, so nothing calls this. See `OcrInstallAvailability`.
    func install(
        payload: URL, identity: OcrResourceIdentity
    ) async -> Result<OcrResourceInventory, OcrResourceError>
}

/// Whether a language may be installed from this window, and why not.
///
/// §14.5 installs language resources "through the existing managed Core
/// resource/distribution owner, from its trusted manifest". This repository
/// ships no such owner for language resources: no signed catalogue of
/// languages, no downloader and no release authority. An install needs an
/// exact byte size and SHA-256, and the only other place those could come from
/// is the person typing them, which would make the caller the release
/// authority. So the action is unavailable, with that as its reason, rather
/// than a path-and-digest form worn as a product feature.
public enum OcrInstallAvailability: Sendable, Equatable {
    case unavailableNoTrustedCatalogue

    public var reason: String {
        "Installing a language needs a signed resource catalogue to say which "
            + "bytes are the release. This build has no such catalogue and no "
            + "downloader, so there is nothing here that could name a verified "
            + "resource. Core will not accept a size and digest typed by hand as "
            + "the release authority."
    }
}

// MARK: - The pane's model

/// What the Document processing pane knows about installed languages.
///
/// The window renders this and calls into it; it holds every decision that is
/// not drawing, so the pane's states can be tested without AppKit. Three of
/// those decisions are worth naming here:
///
/// * A failed re-read does not erase the previous reading. The figures stay,
///   marked stale, because a failed read is not proof of an empty cache.
/// * A pinned set is refused here, before anything is run. The service refuses
///   it too; this is the half that keeps the window from offering it.
/// * Every mutation is followed by a fresh `status`, never by the inventory
///   the mutation itself returned — only a new reading is what is there now.
@MainActor
public final class OcrLanguageModel {
    public enum State: Equatable {
        /// No route to the packaged service at all.
        case noAdministrator
        case loading
        /// Nothing has ever been read, and the last attempt failed.
        case unavailable(OcrResourceError)
        case ready(OcrResourceInventory)
    }

    public private(set) var state: State
    /// Set when the figures on screen are from an earlier, successful reading.
    public private(set) var staleError: OcrResourceError?
    /// The operation in flight, by `operationKey(for:)` or `repairOperation`.
    public private(set) var busy: String?
    public private(set) var lastRepair: OcrReconcileOutcome?

    /// Called after every state change, on the main actor.
    public var onChange: (() -> Void)?

    private let administering: OcrResourceAdministering?
    private var loaded = false

    public init(administering: OcrResourceAdministering?) {
        self.administering = administering
        state = administering == nil ? .noAdministrator : .loading
    }

    public static let repairOperation = "repair-language-storage"

    /// The busy key for one resource set: the whole identity, because two
    /// model revisions of one language can both be installed.
    public static func operationKey(for identity: OcrResourceIdentity) -> String {
        "\(identity.language)/\(identity.engineVersion)/\(identity.modelVersion)/\(identity.sha256)"
    }

    /// Only an unpinned set may be removed (§18.4). Pinned bytes belong to
    /// unfinished work and an attempted install cannot cancel that pin.
    public static func canRemove(_ resource: OcrInstalledResource) -> Bool { !resource.pinned }

    /// True while no operation is in flight and there is something to act on.
    public var canAct: Bool { administering != nil && busy == nil }

    public var inventory: OcrResourceInventory? {
        if case .ready(let inventory) = state { return inventory }
        return nil
    }

    /// Read the cache. Passive — `status` serves nothing and starts nothing —
    /// so this is safe on window open. Repeated calls are ignored until a
    /// mutation or an explicit refresh invalidates the reading.
    public func load(force: Bool = false) async {
        guard let administering else { return }
        if loaded && !force { return }
        loaded = true
        if case .unavailable = state { state = .loading }
        switch await administering.status() {
        case .success(let inventory):
            state = .ready(inventory)
            staleError = nil
        case .failure(let error):
            if case .ready = state {
                staleError = error
            } else {
                state = .unavailable(error)
            }
        }
        onChange?()
    }

    public func refresh() async { await load(force: true) }

    /// Remove one exact installed identity. Refused here when it is pinned, so
    /// a stale row cannot be the thing that asks.
    @discardableResult
    public func remove(
        _ resource: OcrInstalledResource
    ) async -> Result<Void, OcrResourceError> {
        guard let administering else { return .failure(.unavailable) }
        guard Self.canRemove(resource) else {
            return .failure(.refused(
                code: OcrResourceError.pinned,
                reason: "this language is in use by "
                    + resource.pinnedBy.joined(separator: ", ")
            ))
        }
        busy = Self.operationKey(for: resource.identity)
        onChange?()
        let result = await administering.remove(resource.identity)
        return await finish(result.map { _ in () })
    }

    @discardableResult
    public func repair() async -> Result<OcrReconcileOutcome, OcrResourceError> {
        guard let administering else { return .failure(.unavailable) }
        busy = Self.repairOperation
        lastRepair = nil
        onChange?()
        let result = await administering.reconcile()
        if case .success(let outcome) = result { lastRepair = outcome }
        _ = await finish(result.map { _ in () })
        return result
    }

    private func finish(
        _ result: Result<Void, OcrResourceError>
    ) async -> Result<Void, OcrResourceError> {
        busy = nil
        if case .failure(let error) = result, case .ready = state {
            staleError = error
        }
        await load(force: true)
        return result
    }
}

// MARK: - The bounded process seam

public struct OcrProcessResult: Sendable, Equatable {
    public let stdout: String
    public let stderr: String
    public let code: Int32
    /// Either stream hit its bound. The document is not parsed when it did.
    public let truncated: Bool

    public init(stdout: String, stderr: String, code: Int32, truncated: Bool) {
        self.stdout = stdout
        self.stderr = stderr
        self.code = code
        self.truncated = truncated
    }
}

public enum OcrProcessFailure: Error, Sendable, Equatable {
    case spawnFailed
    case timedOut
}

/// Running one bounded child process. A seam so the argument vector, the
/// bounds and the decoding can be tested without a service on the machine;
/// production always composes `SystemOcrProcessRunner`, which runs the
/// packaged executable with no shell between.
public protocol OcrProcessRunning: Sendable {
    func run(
        executable: URL,
        arguments: [String],
        timeout: TimeInterval,
        stdoutLimit: Int,
        stderrLimit: Int
    ) async -> Result<OcrProcessResult, OcrProcessFailure>
}

/// `Process` with an argument vector, no shell, no inherited stdin, bounded
/// output on both streams and a bounded wait.
///
/// Three workers run per call — one per stream, one on the child — and all
/// three are joined before `execute` returns, so nothing it started can still
/// be running when its result is handed back. That is why the collected bytes
/// live in `DrainedStream` rather than in `execute`'s frame: a worker writing
/// into the frame that already returned is the defect this shape removes, not
/// a race the joins happen to hide.
public struct SystemOcrProcessRunner: OcrProcessRunning {
    public init() {}

    /// Concurrent: a run is a blocking call that can last as long as
    /// `reconcileTimeout`, and a serial queue would put a cheap `status` read
    /// behind it.
    private static let queue = DispatchQueue(
        label: "com.omnivia.core-settings.ocr", qos: .userInitiated, attributes: .concurrent
    )

    /// How long a child gets to honour SIGTERM after the bounded wait expired,
    /// before it is killed outright.
    static let terminationGrace: TimeInterval = 2
    /// How long past the bounded wait a drain keeps reading before it stops of
    /// its own accord. Longer than `terminationGrace`, so the kill has already
    /// happened; finite, so a descendant holding the write end open cannot make
    /// a drain unjoinable.
    static let drainGrace: TimeInterval = 5

    /// One stream's collected bytes, owned by a lock rather than by the caller's
    /// frame. Written once by the drain, read once after it is joined.
    private final class DrainedStream: @unchecked Sendable {
        private let lock = NSLock()
        private var collected = Data()
        private var truncated = false

        func store(_ bytes: Data, truncated: Bool) {
            lock.lock()
            defer { lock.unlock() }
            collected = bytes
            self.truncated = truncated
        }

        var value: (bytes: Data, truncated: Bool) {
            lock.lock()
            defer { lock.unlock() }
            return (collected, truncated)
        }
    }

    public func run(
        executable: URL,
        arguments: [String],
        timeout: TimeInterval,
        stdoutLimit: Int,
        stderrLimit: Int
    ) async -> Result<OcrProcessResult, OcrProcessFailure> {
        await withCheckedContinuation { continuation in
            Self.queue.async {
                continuation.resume(returning: Self.execute(
                    executable: executable,
                    arguments: arguments,
                    timeout: timeout,
                    stdoutLimit: stdoutLimit,
                    stderrLimit: stderrLimit
                ))
            }
        }
    }

    private static func execute(
        executable: URL,
        arguments: [String],
        timeout: TimeInterval,
        stdoutLimit: Int,
        stderrLimit: Int
    ) -> Result<OcrProcessResult, OcrProcessFailure> {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        // Nothing is written to the child, and it must never inherit this
        // window's standard input.
        process.standardInput = FileHandle.nullDevice
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        // `waitUntilExit` has no deadline and was seen stuck after the child had
        // exited. The handler is set before `run`, so a child that exits at once
        // still signals, and every wait below carries a deadline.
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }

        do {
            try process.run()
        } catch {
            return .failure(.spawnFailed)
        }

        // Drain both streams while waiting, never after: a full pipe buffer on
        // either one would block the child against our own wait. Both drains
        // stop by `drainDeadline` whatever the child does, so both can be
        // joined unconditionally below.
        let deadline = DispatchTime.now() + timeout
        let drainDeadline = deadline + Self.drainGrace
        let outcome = DrainedStream()
        let diagnostics = DrainedStream()
        let outDrained = DispatchSemaphore(value: 0)
        let errDrained = DispatchSemaphore(value: 0)
        let outDescriptor = out.fileHandleForReading.fileDescriptor
        let errDescriptor = err.fileHandleForReading.fileDescriptor
        DispatchQueue.global().async {
            let read = drain(outDescriptor, limit: stdoutLimit, deadline: drainDeadline)
            outcome.store(read.bytes, truncated: read.truncated)
            outDrained.signal()
        }
        DispatchQueue.global().async {
            let read = drain(errDescriptor, limit: stderrLimit, deadline: drainDeadline)
            diagnostics.store(read.bytes, truncated: read.truncated)
            errDrained.signal()
        }

        var overran = false
        if finished.wait(timeout: deadline) == .timedOut {
            overran = true
            send(SIGTERM, to: process)
            if finished.wait(timeout: .now() + Self.terminationGrace) == .timedOut {
                // A child that ignores SIGTERM cannot ignore this one. The wait
                // is still bounded; `overran` means status is never read.
                send(SIGKILL, to: process)
                _ = finished.wait(timeout: .now() + Self.terminationGrace)
            }
        }
        // Join everything this call started before reading anything it wrote.
        // On normal completion the termination handler has fired before
        // `terminationStatus` is read below; after a timeout no status is read.
        // The drains settle before their handles are closed.
        outDrained.wait()
        errDrained.wait()
        try? out.fileHandleForReading.close()
        try? err.fileHandleForReading.close()

        if overran { return .failure(.timedOut) }
        let collected = outcome.value
        let reported = diagnostics.value
        return .success(OcrProcessResult(
            stdout: String(decoding: collected.bytes, as: UTF8.self),
            stderr: String(decoding: reported.bytes, as: UTF8.self),
            code: process.terminationStatus,
            truncated: collected.truncated || reported.truncated
        ))
    }

    /// Signal the child, and only the child.
    ///
    /// `kill(0, …)` signals this process' whole group and a reaped pid can have
    /// been handed to something else, so a signal goes out only while `Process`
    /// still reports a live child with a positive pid. The check and the send
    /// cannot be one atomic step — nothing in `Process` offers that — but a
    /// child that exits in between is one `Process` has not yet reaped, so the
    /// pid is still its own.
    private static func send(_ number: Int32, to process: Process) {
        let pid = process.processIdentifier
        guard process.isRunning, pid > 0 else { return }
        _ = kill(pid, number)
    }

    /// Read to EOF, keeping at most `limit` bytes and returning by `deadline`
    /// whatever the writer does.
    ///
    /// Reading continues past the bound so the child is never blocked on a full
    /// pipe — the excess is counted and discarded. `poll` on a non-blocking
    /// descriptor rather than `FileHandle.availableData`, for two reasons: a
    /// descendant of a killed child can hold the write end open for ever, and
    /// an uninterruptible read would then make this worker impossible to join;
    /// and `availableData` raises an exception on a closed descriptor instead
    /// of returning.
    private static func drain(
        _ descriptor: Int32, limit: Int, deadline: DispatchTime
    ) -> (bytes: Data, truncated: Bool) {
        let flags = fcntl(descriptor, F_GETFL)
        if flags >= 0 { _ = fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) }
        var collected = Data()
        var truncated = false
        var buffer = [UInt8](repeating: 0, count: 64 << 10)
        while true {
            let now = DispatchTime.now()
            guard now < deadline else { break }
            let waiting = (deadline.uptimeNanoseconds - now.uptimeNanoseconds) / 1_000_000
            var watched = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
            let ready = poll(&watched, 1, Int32(max(1, min(waiting, UInt64(Int32.max)))))
            if ready < 0 {
                if errno == EINTR { continue }
                break
            }
            // Nothing arrived before the deadline. The writer is still holding
            // the pipe, so this is as much as there is to read.
            if ready == 0 { break }
            let count = buffer.withUnsafeMutableBytes {
                read(descriptor, $0.baseAddress, $0.count)
            }
            if count < 0 {
                if errno == EINTR || errno == EAGAIN { continue }
                break
            }
            if count == 0 { break }
            let room = limit - collected.count
            if room > 0 { collected.append(contentsOf: buffer[0..<min(room, count)]) }
            if count > max(room, 0) { truncated = true }
        }
        return (collected, truncated)
    }
}

// MARK: - The client

/// Runs the packaged `omnivia-core-service` for one installation.
public struct OcrResourceCommandRunner: OcrResourceAdministering {
    private let executable: URL
    private let installationState: URL
    private let process: any OcrProcessRunning

    /// Nil unless the installation-state root is an absolute path. The service
    /// refuses a relative one with a usage fault, and a client that cannot name
    /// the cache it administers has nothing truthful to show.
    public init?(
        executable: URL,
        installationState: URL,
        process: any OcrProcessRunning = SystemOcrProcessRunner()
    ) {
        guard installationState.isFileURL, installationState.path.hasPrefix("/"),
              executable.isFileURL, executable.path.hasPrefix("/")
        else { return nil }
        self.executable = executable.standardizedFileURL
        self.installationState = installationState.standardizedFileURL
        self.process = process
    }

    /// `status` only reads the state document. `remove` rewrites it and unlinks
    /// one directory. `reconcile` re-digests every installed resource, so it is
    /// bounded by the whole 512 MiB cache rather than by one file.
    static let statusTimeout: TimeInterval = 20
    static let removeTimeout: TimeInterval = 120
    static let reconcileTimeout: TimeInterval = 600
    /// An install copies, counts and digests up to the declared size.
    static let installTimeout: TimeInterval = 900

    /// The result document is a few hundred bytes per installed resource and
    /// the cap holds at most a few dozen; a megabyte is far past any honest
    /// one. The operator sentence is a line.
    static let stdoutLimit = 1 << 20
    static let stderrLimit = 64 << 10

    // MARK: Arguments

    /// The exact argument vector, built here and nowhere else.
    ///
    /// `--workspace` is required by the service's shared parser and is never
    /// read by this mode: `--ocr-resources` administers installation-local
    /// state, owns no workspace, opens none and returns before the modes that
    /// fence one. The installation-state root is passed for it because it is
    /// the one absolute path this client legitimately holds — inventing a
    /// second path would be inventing a workspace location.
    static func arguments(
        installationState: URL,
        action: String,
        identity: OcrResourceIdentity? = nil,
        payload: URL? = nil
    ) -> [String] {
        var arguments = [
            "--workspace", installationState.path,
            "--installation-state", installationState.path,
            "--ocr-resources", action,
        ]
        if let payload {
            arguments += ["--ocr-payload", payload.path]
        }
        if let identity {
            arguments += [
                "--ocr-language", identity.language,
                "--ocr-engine-version", identity.engineVersion,
                "--ocr-model-version", identity.modelVersion,
                "--ocr-byte-size", String(identity.byteSize),
                "--ocr-sha256", identity.sha256,
            ]
        }
        return arguments
    }

    // MARK: Actions

    public func status() async -> Result<OcrResourceInventory, OcrResourceError> {
        await inventory(action: "status", timeout: Self.statusTimeout)
    }

    public func remove(
        _ identity: OcrResourceIdentity
    ) async -> Result<OcrResourceInventory, OcrResourceError> {
        // The identity goes back out exactly as it was decoded, and it was
        // decoded only in the bounded token shape `Decoding.identity` enforces.
        await inventory(action: "remove", timeout: Self.removeTimeout, identity: identity)
    }

    public func install(
        payload: URL, identity: OcrResourceIdentity
    ) async -> Result<OcrResourceInventory, OcrResourceError> {
        guard payload.isFileURL, payload.path.hasPrefix("/") else {
            return .failure(.usageFault("--ocr-payload must be an absolute file path"))
        }
        return await inventory(
            action: "install", timeout: Self.installTimeout, identity: identity, payload: payload
        )
    }

    public func reconcile() async -> Result<OcrReconcileOutcome, OcrResourceError> {
        let run = await invoke(action: "reconcile", timeout: Self.reconcileTimeout)
        return run.flatMap { output in
            Decoding.reconcile(output.stdout, action: "reconcile") ?? .failure(.unreadableOutput)
        }
    }

    private func inventory(
        action: String,
        timeout: TimeInterval,
        identity: OcrResourceIdentity? = nil,
        payload: URL? = nil
    ) async -> Result<OcrResourceInventory, OcrResourceError> {
        let run = await invoke(
            action: action, timeout: timeout, identity: identity, payload: payload
        )
        return run.flatMap { output in
            Decoding.inventory(output.stdout, action: action) ?? .failure(.unreadableOutput)
        }
    }

    /// One run, with the three exit outcomes separated before any decoding.
    private func invoke(
        action: String,
        timeout: TimeInterval,
        identity: OcrResourceIdentity? = nil,
        payload: URL? = nil
    ) async -> Result<OcrProcessResult, OcrResourceError> {
        let arguments = Self.arguments(
            installationState: installationState,
            action: action,
            identity: identity,
            payload: payload
        )
        let run = await process.run(
            executable: executable,
            arguments: arguments,
            timeout: timeout,
            stdoutLimit: Self.stdoutLimit,
            stderrLimit: Self.stderrLimit
        )
        switch run {
        case .failure(.spawnFailed): return .failure(.unavailable)
        case .failure(.timedOut): return .failure(.timedOut)
        case .success(let output):
            if output.truncated { return .failure(.outputTooLarge) }
            switch output.code {
            case 0:
                return .success(output)
            case 1:
                // A refusal still prints the versioned document, and its stable
                // `code` is the whole point of reading it rather than stderr.
                return .failure(Decoding.refusal(output.stdout, action: action) ?? .unreadableOutput)
            case 2:
                let sentence = output.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                return .failure(.usageFault(
                    sentence.isEmpty ? "the service reported a usage fault" : sentence
                ))
            default:
                return .failure(.processFailed(code: output.code))
            }
        }
    }

    // MARK: Decoding

    /// Strict reading of `{"ocr_resource_result_version":1, …}`.
    ///
    /// Strict means: the version must be the one this build knows, the action
    /// must be the one that was asked for, every key must be expected and every
    /// expected key must be present and of the right type. An unknown or extra
    /// field is a document from a build that means something this one does not,
    /// and rendering the fields that happen to match would be a guess about
    /// installed bytes.
    public enum Decoding {
        public static let version = 1

        private static let statusKeys: Set<String> = [
            "ocr_resource_result_version", "action", "status",
            "capacityBytes", "installedBytes", "availableBytes", "resources",
        ]
        private static let removeKeys = statusKeys.union(["identity", "removed"])
        private static let installKeys: Set<String> = [
            "ocr_resource_result_version", "action", "status",
            "capacityBytes", "installedBytes", "availableBytes", "resources",
            "identity", "newlyInstalled",
        ]
        private static let reconcileKeys = statusKeys.union([
            "discardedStaging", "discardedUnreferenced",
            "quarantined", "unrecoverablePinned", "changed",
        ])
        private static let refusalKeys: Set<String> = [
            "ocr_resource_result_version", "action", "status", "code", "reason",
        ]

        private enum Envelope {
            case document([String: Any])
            /// A result document this build must not read.
            case rejected(OcrResourceError)
            /// Not a result document at all.
            case absent
        }

        private static func envelope(_ text: String, action: String) -> Envelope {
            guard let data = text.data(using: .utf8),
                  let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let version = integer(root["ocr_resource_result_version"])
            else { return .absent }
            guard version == self.version else { return .rejected(.unsupportedSchema(version)) }
            guard root["action"] as? String == action,
                  let status = root["status"] as? String,
                  status == "accepted" || status == "refused"
            else { return .absent }
            return .document(root)
        }

        /// The refusal an exit-1 run printed, or nil when stdout is not a
        /// result document this build can read.
        public static func refusal(_ text: String, action: String) -> OcrResourceError? {
            switch envelope(text, action: action) {
            case .absent: return nil
            case .rejected(let error): return error
            case .document(let root):
                guard root["status"] as? String == "refused",
                      Set(root.keys) == refusalKeys,
                      let code = root["code"] as? String, !code.isEmpty,
                      let reason = root["reason"] as? String
                else { return nil }
                return .refused(code: code, reason: reason)
            }
        }

        /// The inventory an accepted `status`, `remove` or `install` printed.
        public static func inventory(
            _ text: String, action: String
        ) -> Result<OcrResourceInventory, OcrResourceError>? {
            let root: [String: Any]
            switch envelope(text, action: action) {
            case .absent: return nil
            case .rejected(let error): return .failure(error)
            case .document(let decoded): root = decoded
            }
            guard root["status"] as? String == "accepted" else { return nil }
            let expected: Set<String>
            switch action {
            case "status": expected = statusKeys
            case "remove": expected = removeKeys
            case "install": expected = installKeys
            default: return nil
            }
            guard Set(root.keys) == expected else { return nil }
            if action == "remove" {
                guard boolean(root["removed"]) == true, identity(root["identity"]) != nil
                else { return nil }
            }
            if action == "install" {
                guard boolean(root["newlyInstalled"]) != nil, identity(root["identity"]) != nil
                else { return nil }
            }
            return inventoryBody(root).map(Result.success)
        }

        /// The outcome an accepted `reconcile` printed.
        public static func reconcile(
            _ text: String, action: String
        ) -> Result<OcrReconcileOutcome, OcrResourceError>? {
            let root: [String: Any]
            switch envelope(text, action: action) {
            case .absent: return nil
            case .rejected(let error): return .failure(error)
            case .document(let decoded): root = decoded
            }
            guard root["status"] as? String == "accepted",
                  Set(root.keys) == reconcileKeys,
                  let staging = root["discardedStaging"] as? [String],
                  let unreferenced = root["discardedUnreferenced"] as? [String],
                  let quarantined = identities(root["quarantined"]),
                  let pinned = identities(root["unrecoverablePinned"]),
                  let changed = boolean(root["changed"]),
                  let inventory = inventoryBody(root)
            else { return nil }
            return .success(OcrReconcileOutcome(
                discardedStaging: staging,
                discardedUnreferenced: unreferenced,
                quarantined: quarantined,
                unrecoverablePinned: pinned,
                changed: changed,
                inventory: inventory
            ))
        }

        /// The capacity and resource half every accepted document carries.
        ///
        /// The three figures are checked against each other and against the
        /// sizes the same document declares, because that is how the service
        /// derives them: `availableBytes` is `capacityBytes - installedBytes`
        /// and `installedBytes` is the sum of the installed entries' declared
        /// sizes. A document whose arithmetic disagrees with itself is not a
        /// reading of one cache, and the pane would render the disagreement as
        /// free space. Every sum is checked for overflow rather than computed:
        /// the alternative to a refusal here is a trap, not a wrong figure.
        private static func inventoryBody(_ root: [String: Any]) -> OcrResourceInventory? {
            guard let capacity = integer(root["capacityBytes"]), capacity > 0,
                  let installed = integer(root["installedBytes"]), installed >= 0,
                  let available = integer(root["availableBytes"]), available >= 0,
                  let rows = root["resources"] as? [Any]
            else { return nil }
            let (occupied, occupiedOverflowed) = installed.addingReportingOverflow(available)
            guard !occupiedOverflowed, occupied == capacity else { return nil }
            var resources: [OcrInstalledResource] = []
            var declared = 0
            var previousKey = ""
            for row in rows {
                guard let row = row as? [String: Any],
                      Set(row.keys) == ["identity", "pinned", "pinnedBy", "bytesPresent"],
                      let identity = identity(row["identity"]),
                      let pinned = boolean(row["pinned"]),
                      let pinnedBy = row["pinnedBy"] as? [String],
                      let present = boolean(row["bytesPresent"])
                else { return nil }
                // The service reports both; a row claiming no pin while naming
                // the operations that hold one is not a row to render.
                guard pinned == !pinnedBy.isEmpty else { return nil }
                // The lifecycle holds one claim per (operation, resource set)
                // and sorts each row's pins, so a repeated or unordered
                // operation id is not the document it wrote.
                guard zip(pinnedBy, pinnedBy.dropFirst()).allSatisfy({ precedes($0.0, $0.1) })
                else { return nil }
                // Entries arrive in the store's stable-key order, which is
                // total over the five fields, so strictly ascending is both
                // the ordering rule and the duplicate rule. Two rows for one
                // identity would otherwise be counted twice against the cap
                // and offer the same bytes for removal twice.
                let key = stableKey(identity)
                guard precedes(previousKey, key) else { return nil }
                previousKey = key
                let (sum, sumOverflowed) = declared.addingReportingOverflow(identity.byteSize)
                guard !sumOverflowed else { return nil }
                declared = sum
                resources.append(OcrInstalledResource(
                    identity: identity, pinned: pinned, pinnedBy: pinnedBy, bytesPresent: present
                ))
            }
            guard declared == installed else { return nil }
            return OcrResourceInventory(
                capacityBytes: capacity,
                installedBytes: installed,
                availableBytes: available,
                resources: resources
            )
        }

        /// The store's own stable key: the five identity fields joined by a
        /// separator the token shape excludes, so distinct identities cannot
        /// collide and this is the order the service sorted by.
        static func stableKey(_ identity: OcrResourceIdentity) -> String {
            [
                identity.language, identity.engineVersion, identity.modelVersion,
                String(identity.byteSize), identity.sha256,
            ].joined(separator: "|")
        }

        /// Code-point order, which is what the service's `sorted()` produced.
        /// UTF-8 compares in code-point order; `String`'s own `<` does not.
        private static func precedes(_ lhs: String, _ rhs: String) -> Bool {
            lhs.utf8.lexicographicallyPrecedes(rhs.utf8)
        }

        private static func identities(_ any: Any?) -> [OcrResourceIdentity]? {
            guard let rows = any as? [Any] else { return nil }
            var decoded: [OcrResourceIdentity] = []
            for row in rows {
                guard let one = identity(row) else { return nil }
                decoded.append(one)
            }
            return decoded
        }

        /// One identity, in the bounded shape this client will hand back to the
        /// service. The three version fields are tokens, not free text: that is
        /// what stops a forged status document putting `--ocr-payload` or a
        /// path into the next argument vector.
        public static func identity(_ any: Any?) -> OcrResourceIdentity? {
            guard let row = any as? [String: Any],
                  Set(row.keys) == ["language", "engineVersion", "modelVersion", "byteSize", "sha256"],
                  let language = token(row["language"]),
                  let engine = token(row["engineVersion"]),
                  let model = token(row["modelVersion"]),
                  let size = integer(row["byteSize"]), size >= 0,
                  let digest = row["sha256"] as? String, isSha256(digest)
            else { return nil }
            return OcrResourceIdentity(
                language: language, engineVersion: engine, modelVersion: model,
                byteSize: size, sha256: digest
            )
        }

        /// Letters, digits and `._+-`, at most 64 of them. No separator, no
        /// whitespace, no leading dash, nothing that can be read as a flag.
        static func token(_ any: Any?) -> String? {
            guard let text = any as? String,
                  !text.isEmpty, text.utf8.count <= 64,
                  !text.hasPrefix("-"),
                  text.allSatisfy({ character in
                      character.isASCII
                          && (character.isLetter || character.isNumber
                              || "._+-".contains(character))
                  })
            else { return nil }
            return text
        }

        static func isSha256(_ text: String) -> Bool {
            text.count == 64 && text.allSatisfy { $0.isHexDigit && !$0.isUppercase }
        }

        /// JSON `true`/`false` only. `1` is not a boolean here: `NSNumber`
        /// bridges both ways, so without the type check an integer field would
        /// silently satisfy a boolean one.
        static func boolean(_ any: Any?) -> Bool? {
            guard let number = any as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID()
            else { return nil }
            return number.boolValue
        }

        /// A JSON integer, exactly, or nothing.
        ///
        /// Rejects booleans and any value written as a float — a byte count is
        /// counted, not measured — and any value with no exact `Int`. The last
        /// is not theoretical: `JSONSerialization` hands back an *unsigned*
        /// `NSNumber` for a literal above `Int.max`, which passes a float
        /// check, and `intValue` then wraps it to a negative figure. A forged
        /// `9223372036854775808` must be unreadable, not minus nine quintillion.
        static func integer(_ any: Any?) -> Int? {
            guard let number = any as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID()
            else { return nil }
            switch String(cString: number.objCType) {
            case "d", "f":
                return nil
            case "Q":
                let unsigned = number.uint64Value
                guard unsigned <= UInt64(Int.max) else { return nil }
                return Int(unsigned)
            default:
                return Int(exactly: number.int64Value)
            }
        }
    }
}
