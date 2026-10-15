// ============================================================================
// This workspace's durable document-processing preferences, as the Document
// processing pane needs them.
//
// The authority is Core's own: `document.settings.get` and
// `document.settings.update`, reached through the installed `omnivia` client as
// `document settings` and `document configure`. This window owns no settings
// store, no second copy of the defaults and no persistence of its own — it
// reads what Core holds and asks Core to change it.
//
// Four things about that surface shape the code below:
//
// * `revision` is a compare-and-swap token. An update must name the revision it
//   last read, so a change made elsewhere since that read is refused as a
//   `conflict` rather than silently overwritten. Nothing here invents one: a
//   write is only offered once a read has succeeded.
// * `document.settings.update` requires an idempotency key, and equal keys with
//   different inputs are an `idempotency_conflict`. The key is therefore derived
//   from the exact change and the revision it is made against, so a retry of one
//   change is a replay and two different changes can never share a key.
// * An update is a *partial* change: a member it does not name is held. One user
//   action names one member, which is why `DocumentProcessingChange` is a closed
//   pair rather than a whole settings document — re-sending the members the user
//   did not touch would overwrite a preference this build may not understand.
// * The preferences decide what a *future* capture is admitted under and nothing
//   else. An already captured document keeps the snapshot it was admitted with,
//   so changing a preference here never reprocesses anything. See
//   `DocumentProcessingModel.futureCapturesNote`.
//
// Nothing here installs a language resource. There is no install affordance on
// this surface at all: a selected language that is not installed is reported as
// exactly that, and installation remains the managed resource owner's explicit
// flow (`OcrInstallAvailability`).
// ============================================================================

import Foundation

// MARK: - Model

/// The closed pair of processing modes, in the order the pane offers them.
public enum DocumentProcessingMode: String, Sendable, CaseIterable {
    /// The default: native text, plus local recognition for a PDF or an image.
    case textAndLocalOcr = "text_and_local_ocr"
    /// The explicit faster alternate, which recognises nothing.
    case textOnly = "text_only"

    /// The segment label. Short, because it sits in a two-segment control.
    public var label: String {
        switch self {
        case .textAndLocalOcr: return "Text and local OCR"
        case .textOnly: return "Text only"
        }
    }

    /// What this mode does, as the row says it.
    public var summary: String {
        switch self {
        case .textAndLocalOcr:
            return "Reads native text, and recognises text in a PDF or an image on this Mac."
        case .textOnly:
            return "Faster, and recognises nothing — including a scanned page."
        }
    }
}

/// One complete reading of this workspace's preferences, with its token.
///
/// `modeCode` is kept as the service reported it. The mode vocabulary is open,
/// so a later Core may name one this build has never heard of; that is reported
/// as a mode this build does not understand rather than rendered as one of the
/// two it does.
public struct DocumentProcessingSettings: Sendable, Equatable {
    public let modeCode: String
    /// The ordered selection, at most three. Order is the selection order.
    public let recognitionLanguages: [String]
    public let embeddedImageOcr: Bool
    /// The compare-and-swap token. Zero means nothing is stored and these are
    /// the product defaults.
    public let revision: Int

    public init(
        modeCode: String, recognitionLanguages: [String], embeddedImageOcr: Bool, revision: Int
    ) {
        self.modeCode = modeCode
        self.recognitionLanguages = recognitionLanguages
        self.embeddedImageOcr = embeddedImageOcr
        self.revision = revision
    }

    /// The mode, when it is one this build knows. Nil is not a default.
    public var mode: DocumentProcessingMode? { DocumentProcessingMode(rawValue: modeCode) }

    /// True when nothing is stored, so the values shown are the product
    /// defaults rather than a choice anybody made.
    public var isProductDefault: Bool { revision == 0 }
}

/// One user action, as the one settings member it changes.
public enum DocumentProcessingChange: Sendable, Equatable {
    case mode(DocumentProcessingMode)
    case embeddedImageOcr(Bool)

    /// The busy key, and the tail of the idempotency key. Stable for one
    /// change and different for every other, which is what the key needs.
    public var key: String {
        switch self {
        case .mode(let mode): return "mode-\(mode.rawValue)"
        case .embeddedImageOcr(let on): return "embedded-\(on ? "on" : "off")"
        }
    }

    /// The `document.settings.update` input member this change names.
    fileprivate var member: (name: String, value: Any) {
        switch self {
        case .mode(let mode): return ("mode", mode.rawValue)
        case .embeddedImageOcr(let on): return ("embedded_image_ocr", on)
        }
    }

    /// Whether `settings` already says this, so there is nothing to write. A
    /// two-segment control fires on the segment that is already selected.
    public func isAlreadyInForce(in settings: DocumentProcessingSettings) -> Bool {
        switch self {
        case .mode(let mode): return settings.modeCode == mode.rawValue
        case .embeddedImageOcr(let on): return settings.embeddedImageOcr == on
        }
    }
}

/// Why the outcomes of one invocation are not one error.
public enum DocumentProcessingError: Error, Sendable, Equatable {
    /// No installed `omnivia` to run, or it could not be started.
    case unavailable
    case timedOut
    /// Exit, or output, that is not the response envelope this build reads.
    case unreadableOutput
    /// The client ran and produced no envelope, exiting with this status. Its
    /// local refusals are a fixed, distinct set — the service was not running,
    /// the credential was not accepted, the versions did not agree — and each
    /// is a different state from "Core answered something unreadable".
    case noAnswer(exitCode: Int32)
    /// A response envelope of an api version this build does not know.
    case unsupportedContract(String)
    /// The service answered, and the answer was a refusal carrying this frozen
    /// error code.
    case refused(code: String)

    /// `conflict`: the settings changed between the read and the write.
    public static let conflict = "conflict"

    /// What the pane says. Each sentence names the state rather than the step
    /// that failed: an unavailable control must never read as an enabled one.
    ///
    /// Every sentence has to be true of a failed read as well as a failed
    /// write, because the pane shows the same reason in both places. Only
    /// `conflict` is write-specific, and `document.settings.get` cannot return
    /// it.
    public var sentence: String {
        switch self {
        case .unavailable:
            return "This window has no route to the installed Core client, so it cannot "
                + "read or change these preferences."
        case .timedOut:
            return "Core did not answer in time. The preferences on screen are the last "
                + "ones read."
        case .unreadableOutput:
            return "Core answered with something this build cannot read, so nothing is "
                + "shown as changed."
        case .noAnswer(let exitCode):
            // The installed client's own local refusals, which it reports by
            // exit status before anything reaches the service.
            switch exitCode {
            case 2:
                return "The installed Core client refused the call here and never sent it, "
                    + "so nothing was read or changed."
            case 3:
                return "Core did not accept this client’s credential, so nothing was read "
                    + "or changed."
            case 4:
                return "This client and Core could not agree on a version, so nothing was "
                    + "read or changed."
            case 6:
                return "The call ran out of time or was cancelled, so nothing is shown as "
                    + "changed."
            default:
                return "Core could not be reached, so nothing was read or changed. It may "
                    + "not be running on this Mac."
            }
        case .unsupportedContract(let version):
            return "Core answered over contract version \(version), which this build does "
                + "not know. It will not read a newer answer as if it were this one."
        case .refused(let code):
            switch code {
            case DocumentProcessingError.conflict:
                return "These preferences changed elsewhere while this window held an "
                    + "older reading, so the change was refused rather than overwriting "
                    + "it. The values shown have been read again."
            case "authentication_required", "authorization_denied", "workspace_not_granted",
                 "capability_not_granted", "invalid_purpose":
                return "Core refused the call because this client is not authorised to make "
                    + "it (\(code)). Nothing was read or changed."
            case "workspace_busy", "workspace_lease_unavailable":
                return "Core is busy with this workspace (\(code)). Nothing was read or "
                    + "changed; it can be tried again."
            case "dependency_unavailable", "internal_recoverable", "rate_limited":
                return "Core could not complete the call (\(code)). Nothing was read or "
                    + "changed; it can be tried again."
            default:
                return "Core refused the call (\(code)). Nothing was read or changed."
            }
        }
    }
}

public protocol DocumentProcessingAdministering: Sendable {
    func read() async -> Result<DocumentProcessingSettings, DocumentProcessingError>
    /// `revision` is the one the caller last read. Returns the settings as they
    /// now stand, carrying the advanced revision.
    func update(
        revision: Int, change: DocumentProcessingChange
    ) async -> Result<DocumentProcessingSettings, DocumentProcessingError>
}

/// One selected recognition language, as the pane shows it.
public struct DocumentProcessingLanguage: Sendable, Equatable {
    /// Whether this installation can serve the language the selection names.
    /// Three states, because "could not be read" is not "not installed".
    public enum Availability: Sendable, Equatable {
        case installed
        case notInstalled
        case unconfirmed
    }

    public let code: String
    public let displayName: String
    public let availability: Availability

    public init(code: String, displayName: String, availability: Availability) {
        self.code = code
        self.displayName = displayName
        self.availability = availability
    }

    /// The chip the row carries.
    public var status: String {
        switch availability {
        case .installed: return "Installed on this Mac"
        case .notInstalled: return "Selected, not installed"
        case .unconfirmed: return "Installed languages couldn’t be read"
        }
    }
}

// MARK: - The pane's model

/// What the Document processing pane knows about this workspace's preferences.
///
/// The window renders this and calls into it; every decision that is not
/// drawing lives here, so the pane's states can be tested without AppKit.
/// Three of those decisions are worth naming:
///
/// * A write is offered only over a successful read, because an update must
///   name the revision it was made against and this build will not guess one.
/// * Every write is followed by a fresh read, success or failure. A write that
///   timed out may well have landed, and only a new reading is what Core holds.
/// * A failed write leaves the last reading on screen and the failure beside
///   it. The control goes back to the stored value rather than keeping the one
///   the user clicked, which was never saved.
@MainActor
public final class DocumentProcessingModel {
    public enum State: Equatable {
        /// No route to the installed client at all.
        case noAdministrator
        case loading
        /// Nothing has ever been read, and the last attempt failed.
        case unavailable(DocumentProcessingError)
        case ready(DocumentProcessingSettings)
    }

    public private(set) var state: State
    /// Set when the values on screen are from an earlier, successful reading.
    public private(set) var staleError: DocumentProcessingError?
    /// The change in flight, by `DocumentProcessingChange.key`.
    public private(set) var busy: String?
    /// The last write that did not take effect, until the next one is tried.
    public private(set) var lastWriteError: DocumentProcessingError?

    /// Called after every state change, on the main actor.
    public var onChange: (() -> Void)?

    private let administering: DocumentProcessingAdministering?
    private var loaded = false

    public init(administering: DocumentProcessingAdministering?) {
        self.administering = administering
        state = administering == nil ? .noAdministrator : .loading
    }

    /// The standing fact the pane states, which is the whole point of the
    /// settings being read once at admission: a change reaches what has not
    /// been captured yet, and nothing else. There is no selected source on this
    /// screen, so the recovery for an existing capture is named rather than
    /// offered as a button that would have nothing to act on.
    public static let futureCapturesNote =
        "These preferences apply to future captures. A document already captured keeps the "
        + "settings it was captured with, and changing them here reprocesses nothing. To read "
        + "an existing captured copy again, open Files and folders, select the file and use "
        + "Reprocess captured copy."

    /// Why a selected language is never installed as a side effect of being
    /// selected. §14.5's managed resource owner is the only installer.
    public static let noInstallNote =
        "Selecting a language never downloads or installs one. A selected language that is "
        + "not installed stays selected, and the capture that needed it reports the missing "
        + "language rather than quietly recognising in another."

    public var settings: DocumentProcessingSettings? {
        if case .ready(let settings) = state { return settings }
        return nil
    }

    /// True while no write is in flight and there is a reading to write over.
    public var canAct: Bool { administering != nil && busy == nil && settings != nil }

    /// Read the preferences. Passive — `document.settings.get` opens no capture
    /// and changes nothing — so this is safe on window open. Repeated calls are
    /// ignored until a write or an explicit refresh invalidates the reading.
    public func load(force: Bool = false) async {
        guard let administering else { return }
        if loaded && !force { return }
        loaded = true
        if case .unavailable = state { state = .loading }
        switch await administering.read() {
        case .success(let settings):
            state = .ready(settings)
            staleError = nil
        case .failure(let error):
            // A failed re-read is not proof the preferences changed, so the
            // previous reading stays and is marked as the previous one.
            if case .ready = state {
                staleError = error
            } else {
                state = .unavailable(error)
            }
        }
        onChange?()
    }

    public func refresh() async { await load(force: true) }

    /// Change one preference, under the revision this model last read.
    @discardableResult
    public func apply(
        _ change: DocumentProcessingChange
    ) async -> Result<Void, DocumentProcessingError> {
        guard let administering else { return .failure(.unavailable) }
        // Nothing to write over. Refused here rather than sent with a guessed
        // revision, which is the one thing the compare-and-swap cannot catch.
        guard let current = settings else { return .failure(.unavailable) }
        guard !change.isAlreadyInForce(in: current) else { return .success(()) }
        busy = change.key
        lastWriteError = nil
        onChange?()
        let result = await administering.update(revision: current.revision, change: change)
        busy = nil
        if case .failure(let error) = result { lastWriteError = error }
        // Always, and from a fresh read rather than from the write's own copy:
        // a write that timed out may have landed, and a refused one must leave
        // the stored value on screen.
        await load(force: true)
        return result.map { _ in () }
    }

    /// The selected languages, in their selection order, against what the
    /// managed cache reports.
    ///
    /// `installed` is nil when the inventory could not be read, which makes
    /// every row `.unconfirmed`: this window will not report a language as not
    /// installed on the strength of not having been able to look.
    ///
    /// English is here only when the stored selection names it. Core's product
    /// default is a configured selection of `eng`, and that is what a read
    /// reports at revision zero — nothing is added to a selection here, and
    /// nothing is installed to satisfy one.
    public static func selectedLanguages(
        _ settings: DocumentProcessingSettings, installed: OcrResourceInventory?
    ) -> [DocumentProcessingLanguage] {
        // ponytail: "installed" is the cache's own `bytes_present`, which is
        // what the inventory states. Whether the bytes match the engine
        // revision Core invokes is the service's judgement, and the extraction
        // reports it; the window does not second-guess it from here.
        let present: Set<String>? = installed.map { inventory in
            Set(inventory.resources.filter(\.bytesPresent).map(\.identity.language))
        }
        return settings.recognitionLanguages.map { code in
            let availability: DocumentProcessingLanguage.Availability
            switch present {
            case nil: availability = .unconfirmed
            case .some(let codes): availability = codes.contains(code) ? .installed : .notInstalled
            }
            return DocumentProcessingLanguage(
                code: code,
                displayName: OcrResourceIdentity.displayName(for: code),
                availability: availability
            )
        }
    }
}

// MARK: - The client

/// Runs the installed `omnivia` over the governed application surface.
///
/// The same thin-client pattern the Access pane uses: the operation is named by
/// its argv path, the input is one bounded JSON object, and the answer is the
/// canonical response envelope. There is no second path — no database is
/// opened, no workspace is overridden, no principal is claimed — and purpose,
/// scope and capability are the client's own table lookups keyed by operation.
///
/// The bounded process seam is the module's existing one. Its name carries the
/// `Ocr` prefix of the feature it was written for; what it does is run one
/// child with an argument vector, no shell, no inherited stdin and bounded
/// output on both streams, which is exactly what this needs too.
public struct DocumentProcessingCommandRunner: DocumentProcessingAdministering {
    private let executable: URL
    private let installationState: URL
    private let workspaceRef: String
    private let process: any OcrProcessRunning

    /// Nil unless both paths are absolute and a workspace is named. The CLI
    /// refuses a relative installation state with a usage fault, and a client
    /// that cannot name the workspace it reads has nothing truthful to show.
    public init?(
        executable: URL,
        installationState: URL,
        workspaceRef: String,
        process: any OcrProcessRunning = SystemOcrProcessRunner()
    ) {
        guard executable.isFileURL, executable.path.hasPrefix("/"),
              installationState.isFileURL, installationState.path.hasPrefix("/"),
              !workspaceRef.isEmpty
        else { return nil }
        self.executable = executable.standardizedFileURL
        self.installationState = installationState.standardizedFileURL
        self.workspaceRef = workspaceRef
        self.process = process
    }

    /// A read is a synchronous settings read. A write is one compare-and-swap
    /// transaction. The CLI's own deadline is set inside this call's wait, so
    /// the operation gives up before the child is killed and the answer is the
    /// service's rather than a severed pipe.
    static let readTimeout: TimeInterval = 20
    static let writeTimeout: TimeInterval = 60
    static let readDeadlineMilliseconds = 18_000
    static let writeDeadlineMilliseconds = 55_000

    /// One settings envelope is a few hundred bytes. A quarter of a megabyte is
    /// far past any honest one, and the diagnostic is a line.
    static let stdoutLimit = 256 << 10
    static let stderrLimit = 64 << 10

    /// The api version this build reads. A newer envelope is refused rather
    /// than read with the members that happen to match.
    static let supportedContractVersion = "1.3"

    // MARK: Arguments

    /// The exact argument vector, built here and nowhere else.
    ///
    /// The input travels in argv rather than on standard input because the
    /// shared process seam gives the child a null stdin, and because this input
    /// holds nothing private: a mode, a boolean and a revision.
    static func arguments(
        installationState: URL,
        workspaceRef: String,
        leaf: String,
        input: String,
        deadlineMilliseconds: Int,
        idempotencyKey: String? = nil
    ) -> [String] {
        var arguments = [
            "--installation-state", installationState.path,
            "--workspace-id", workspaceRef,
            "--timeout-ms", String(deadlineMilliseconds),
            "document", leaf,
            "--input-json", input,
            "--json",
        ]
        if let idempotencyKey {
            arguments += ["--idempotency-key", idempotencyKey]
        }
        return arguments
    }

    /// The key one change under one revision replays under. Equal keys with
    /// different inputs are an `idempotency_conflict`, so every member of the
    /// input is in here: the revision, and the one member being changed.
    static func idempotencyKey(revision: Int, change: DocumentProcessingChange) -> String {
        "docproc-\(revision)-\(change.key)"
    }

    // MARK: Actions

    public func read() async -> Result<DocumentProcessingSettings, DocumentProcessingError> {
        await invoke(
            arguments: Self.arguments(
                installationState: installationState,
                workspaceRef: workspaceRef,
                leaf: "settings",
                input: "{}",
                deadlineMilliseconds: Self.readDeadlineMilliseconds
            ),
            timeout: Self.readTimeout
        )
    }

    public func update(
        revision: Int, change: DocumentProcessingChange
    ) async -> Result<DocumentProcessingSettings, DocumentProcessingError> {
        let member = change.member
        // Sorted keys, so one change under one revision is always the same
        // bytes and the idempotency key above is a replay of it.
        guard let encoded = try? JSONSerialization.data(
            withJSONObject: ["revision": revision, member.name: member.value],
            options: [.sortedKeys]
        ) else { return .failure(.unreadableOutput) }
        return await invoke(
            arguments: Self.arguments(
                installationState: installationState,
                workspaceRef: workspaceRef,
                leaf: "configure",
                input: String(decoding: encoded, as: UTF8.self),
                deadlineMilliseconds: Self.writeDeadlineMilliseconds,
                idempotencyKey: Self.idempotencyKey(revision: revision, change: change)
            ),
            timeout: Self.writeTimeout
        )
    }

    private func invoke(
        arguments: [String], timeout: TimeInterval
    ) async -> Result<DocumentProcessingSettings, DocumentProcessingError> {
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
            // A truncated document is not read: a settings envelope that
            // overran the bound is not a settings envelope.
            guard !output.truncated else { return .failure(.unreadableOutput) }
            // `--json` prints the whole envelope on both branches and exits by
            // the error's own mapping, so a service refusal is an answer this
            // pane shows rather than a failure, and the status is not what
            // decides it. A non-zero status with *no* envelope is the other
            // thing: the client refused locally and never reached the service.
            let answer = Self.decode(output.stdout)
            if case .failure(.unreadableOutput) = answer, output.code != 0 {
                return .failure(.noAnswer(exitCode: output.code))
            }
            return answer
        }
    }

    // MARK: Decoding

    /// The canonical response envelope, read strictly.
    ///
    /// The envelope is split before anything is decoded, because its two
    /// branches are different answers rather than two shapes of one. A success
    /// branch is checked against `metadata.version.api_version` first: a newer
    /// Core is a refusal, never a result read as a subset of itself.
    static func decode(
        _ text: String
    ) -> Result<DocumentProcessingSettings, DocumentProcessingError> {
        guard let data = text.data(using: .utf8),
              let document = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let metadata = document["metadata"] as? [String: Any]
        else { return .failure(.unreadableOutput) }
        let result = document["result"] as? [String: Any]
        let failure = document["error"] as? [String: Any]
        // Exactly one branch, and a refusal naming no code is not one this
        // build can report.
        guard (result == nil) != (failure == nil) else { return .failure(.unreadableOutput) }
        if let failure {
            guard let code = failure["code"] as? String, !code.isEmpty,
                  code.unicodeScalars.count <= 128
            else { return .failure(.unreadableOutput) }
            return .failure(.refused(code: code))
        }
        guard let version = metadata["version"] as? [String: Any],
              let apiVersion = version["api_version"] as? String
        else { return .failure(.unreadableOutput) }
        guard apiVersion == supportedContractVersion else {
            return .failure(.unsupportedContract(apiVersion))
        }
        guard let settings = (result?["settings"] as? [String: Any]).flatMap(self.settings(from:))
        else { return .failure(.unreadableOutput) }
        return .success(settings)
    }

    /// One `DocumentProcessingSettings` member, bounded in every direction the
    /// contract bounds it: a mode code of the open-code shape, at most three
    /// language codes, a boolean that is a boolean and a revision that is not
    /// negative. Nothing is defaulted or repaired — a document this cannot read
    /// is one this build must not render as a preference anybody chose.
    static func settings(from member: [String: Any]) -> DocumentProcessingSettings? {
        guard let mode = member["mode"] as? String, isOpenCode(mode),
              let embedded = member["embedded_image_ocr"] as? Bool,
              let revision = member["revision"] as? Int, revision >= 0,
              let languages = member["recognition_languages"] as? [Any],
              (1...3).contains(languages.count)
        else { return nil }
        var codes: [String] = []
        for language in languages {
            guard let code = language as? String, isOpenCode(code) else { return nil }
            codes.append(code)
        }
        guard Set(codes).count == codes.count else { return nil }
        return DocumentProcessingSettings(
            modeCode: mode,
            recognitionLanguages: codes,
            embeddedImageOcr: embedded,
            revision: revision
        )
    }

    /// The bounded token shape of the contract's `OpenCode`: a length, a
    /// leading lower-case ASCII letter, and nothing but lower-case ASCII,
    /// digits and the two separators after it.
    ///
    /// The character classes rather than the pattern itself, deliberately: what
    /// this has to guarantee is that a decoded value cannot be spent as a flag
    /// or a path on the next call, and cannot be rendered as a language name it
    /// is not. It is laxer than the pattern about where a separator may fall,
    /// which is the service's own refusal to make.
    static func isOpenCode(_ value: String) -> Bool {
        guard (1...128).contains(value.unicodeScalars.count),
              let first = value.first, isLowerAscii(first),
              !value.hasSuffix("_"), !value.hasSuffix(".")
        else { return false }
        return value.allSatisfy { character in
            isLowerAscii(character)
                || (character.isASCII && character.isNumber)
                || character == "_" || character == "."
        }
    }

    private static func isLowerAscii(_ character: Character) -> Bool {
        character.isASCII && character.isLetter && character.isLowercase
    }
}
