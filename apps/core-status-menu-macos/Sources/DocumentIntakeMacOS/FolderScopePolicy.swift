import Foundation

/// Why one examined entry did not become a reviewed candidate. The list is
/// finite and versioned: `FolderScopePolicy.exclusionsPolicyRef` names the exact
/// revision frozen into a consent, so a later app version cannot widen or
/// narrow an already approved scope without a new reviewed generation.
public enum FolderExclusionReason: String, Codable, Sendable, CaseIterable {
    case hidden
    case link
    case package
    case otherVolume
    case temporary
    case toolCache
    case controlledStore
    case depthLimit
    case notRecursive
    case unsupportedFormat
    case tooLarge
    case emptyFile
    case irregularKind
    case unknownSize

    /// Which reviewed count this reason is reported under.
    public var bucket: FolderScopeBucket {
        switch self {
        case .hidden, .link, .package, .otherVolume, .temporary, .toolCache,
             .controlledStore, .depthLimit, .notRecursive:
            .excluded
        case .unsupportedFormat, .tooLarge, .emptyFile, .irregularKind:
            .unsupported
        case .unknownSize:
            .unavailable
        }
    }

    /// One short reviewed sentence. These never echo a path or a filename.
    public var reviewDetail: String {
        switch self {
        case .hidden: "Hidden items"
        case .link: "Symbolic links and Finder aliases"
        case .package: "Application bundles and document packages"
        case .otherVolume: "Items on another mounted volume"
        case .temporary: "Lock, swap, partial-download and temporary files"
        case .toolCache: "Dependency and build-cache folders"
        case .controlledStore: "OmniVia Core's own stores"
        case .depthLimit: "Folders deeper than the reviewed limit"
        case .notRecursive: "Subfolders, because Include subfolders is off"
        case .unsupportedFormat: "Formats Core does not read"
        case .tooLarge: "Files over the supported size for their format"
        case .emptyFile: "Empty files"
        case .irregularKind: "Items that are not ordinary files"
        case .unknownSize: "Items whose size could not be read"
        }
    }
}

public enum FolderScopeBucket: String, Sendable {
    case excluded
    case unsupported
    case unavailable
}

/// Part of the scope the traversal could not see. This is never an exclusion:
/// an exclusion is a reviewed decision about an entry that *was* read, while a
/// coverage failure is an entry or subtree that could not be read at all, so
/// the scan is not the whole folder and must not be submitted as if it were.
public struct FolderScopeCoverageFailure: Equatable, Sendable {
    public enum Kind: String, Sendable {
        /// The traversal could not list this location, so anything beneath it
        /// was never examined.
        case enumerationFailed
        /// The entry's own facts could not be read — a provider placeholder
        /// that fails to materialise arrives this way — so neither it nor
        /// anything beneath it was examined.
        case entryUnreadable
    }

    public let kind: Kind
    /// Root-relative locator. Empty means the selected root itself.
    public let relativeComponents: [String]

    /// One reviewed sentence for a whole set. Counts only: a scan that could
    /// not read an item does not then echo its location back into review.
    public static func reviewDetail(_ failures: [FolderScopeCoverageFailure]) -> String? {
        guard !failures.isEmpty else { return nil }
        return "\(failures.count) item\(failures.count == 1 ? "" : "s") in this folder "
            + "could not be read, so this is not the whole folder. "
            + "Check access to this folder, or choose a narrower scope, and try again."
    }
}

/// Deterministic traversal-failure injection. Production never passes one;
/// tests use it to prove a failure cannot produce a complete scan without
/// depending on real permission or file-provider behaviour.
typealias FolderScanFailureProbe = @Sendable (URL) -> FolderScopeCoverageFailure.Kind?

/// A reviewed scan that hit a bound. The first subset is never silently
/// accepted as the whole folder: the breach is displayed and submission is
/// refused until the scope is narrowed.
public enum FolderScopeLimitBreach: Equatable, Sendable {
    case eligibleFiles(Int)
    case examinedEntries(Int)

    public var reviewDetail: String {
        switch self {
        case let .eligibleFiles(limit):
            "This folder exceeds \(limit) supported files. Choose a smaller scope or add exclusions."
        case let .examinedEntries(limit):
            "This folder exceeds \(limit) examined items. Choose a smaller scope or add exclusions."
        }
    }
}

/// Shared monitoring bounds from the design's limit tables. Injectable so the
/// decision table can be proven without materialising ten thousand files;
/// `standard` is the only value production uses.
public struct FolderScopeLimits: Equatable, Sendable {
    public let maximumEligibleFiles: Int
    public let maximumExaminedEntries: Int
    public let maximumDepth: Int

    public init(maximumEligibleFiles: Int, maximumExaminedEntries: Int, maximumDepth: Int) {
        self.maximumEligibleFiles = maximumEligibleFiles
        self.maximumExaminedEntries = maximumExaminedEntries
        self.maximumDepth = maximumDepth
    }

    public static let standard = FolderScopeLimits(
        maximumEligibleFiles: 10_000,
        maximumExaminedEntries: 100_000,
        maximumDepth: 32
    )
}

/// Everything the policy is allowed to know about one examined entry. The
/// traversal establishes these from native resource values; the policy itself
/// touches no filesystem, so every branch below is directly provable.
public struct FolderEntryFacts: Equatable, Sendable {
    public let relativeComponents: [String]
    public let isDirectory: Bool
    public let isRegularFile: Bool
    public let isSymbolicLink: Bool
    public let isAliasFile: Bool
    public let isPackage: Bool
    public let isHidden: Bool
    /// False for a nested mount point or a share mounted beneath the root.
    public let onRootVolume: Bool
    /// Nil when the size could not be read at all, which is an unavailable
    /// item rather than an excluded or unsupported one.
    public let byteLength: UInt64?
    /// A canonical Core/native-owned store, resolved equivalents included.
    public let isControlledStore: Bool

    public init(
        relativeComponents: [String],
        isDirectory: Bool = false,
        isRegularFile: Bool = false,
        isSymbolicLink: Bool = false,
        isAliasFile: Bool = false,
        isPackage: Bool = false,
        isHidden: Bool = false,
        onRootVolume: Bool = true,
        byteLength: UInt64? = nil,
        isControlledStore: Bool = false
    ) {
        self.relativeComponents = relativeComponents
        self.isDirectory = isDirectory
        self.isRegularFile = isRegularFile
        self.isSymbolicLink = isSymbolicLink
        self.isAliasFile = isAliasFile
        self.isPackage = isPackage
        self.isHidden = isHidden
        self.onRootVolume = onRootVolume
        self.byteLength = byteLength
        self.isControlledStore = isControlledStore
    }
}

public enum FolderScopeDecision: Equatable, Sendable {
    case eligible(kind: CaptureContentKind, byteLength: UInt64)
    /// Count it under its reason and never descend into it.
    case skip(FolderExclusionReason)
    case descend
}

/// The one reviewed folder scope policy. Exclusions are applied before any
/// descent or byte load, so an unapproved descendant is never opened.
public enum FolderScopePolicy {
    public static let approvedFormatPolicyRef = "omnivia.folder-formats.v1"
    public static let exclusionsPolicyRef = "omnivia.folder-exclusions.v1"

    /// Finite, versioned temporary/lock patterns. No arbitrary substring match.
    static let temporaryNames: Set<String> = [".DS_Store"]
    static let temporaryPrefixes = ["~$"]
    static let temporarySuffixes = [".swp", ".swo", ".tmp", ".part", ".crdownload"]
    /// Named dependency/cache trees. Shown in review rather than inferred from
    /// every folder that happens to be called `build` or `dist`.
    static let toolCacheDirectoryNames: Set<String> = [
        ".git", ".venv", "node_modules", "__pycache__", "DerivedData",
    ]

    /// Filesystem roots a folder scope is never accepted for. Ordinary explicit
    /// file selection stays a separate action.
    private static let refusedRootPaths: Set<String> = [
        "/", "/System", "/Library", "/private", "/Volumes", "/Users",
        "/Applications", "/bin", "/sbin", "/usr", "/etc", "/var", "/tmp",
        "/opt", "/cores", "/dev", "/Network",
    ]

    public static func decide(
        _ facts: FolderEntryFacts,
        recursive: Bool,
        includeHidden: Bool,
        limits: FolderScopeLimits = .standard
    ) -> FolderScopeDecision {
        guard let name = facts.relativeComponents.last,
              !facts.relativeComponents.isEmpty
        else { return .skip(.irregularKind) }
        // Self-ingestion is an invariant, not a toggle, so it is decided first.
        if facts.isControlledStore { return .skip(.controlledStore) }
        if facts.isSymbolicLink || facts.isAliasFile { return .skip(.link) }
        if facts.relativeComponents.count > limits.maximumDepth { return .skip(.depthLimit) }
        if !includeHidden, facts.isHidden || name.hasPrefix(".") { return .skip(.hidden) }
        if !facts.onRootVolume { return .skip(.otherVolume) }
        if isTemporary(name) { return .skip(.temporary) }
        if facts.isDirectory, toolCacheDirectoryNames.contains(name) { return .skip(.toolCache) }
        if facts.isPackage { return .skip(.package) }
        if facts.isDirectory {
            // Recursion off means the descendant list is never even read.
            return recursive ? .descend : .skip(.notRecursive)
        }
        guard facts.isRegularFile else { return .skip(.irregularKind) }
        guard let kind = CaptureContentKind.approvedFolderFormat(forName: name) else {
            return .skip(.unsupportedFormat)
        }
        guard let byteLength = facts.byteLength else { return .skip(.unknownSize) }
        guard byteLength > 0 else { return .skip(.emptyFile) }
        guard byteLength <= maximumSourceBytes(for: kind) else { return .skip(.tooLarge) }
        return .eligible(kind: kind, byteLength: byteLength)
    }

    /// Per-format source bounds from the design's limit table.
    public static func maximumSourceBytes(for kind: CaptureContentKind) -> UInt64 {
        switch kind {
        case .text, .markdown, .link: 1 * 1024 * 1024
        case .rtf, .html: 20 * 1024 * 1024
        case .pdf, .docx, .xlsx, .pptx, .csv, .tsv, .image: 50 * 1024 * 1024
        }
    }

    static func isTemporary(_ name: String) -> Bool {
        if temporaryNames.contains(name) { return true }
        if temporaryPrefixes.contains(where: name.hasPrefix) { return true }
        let lower = name.lowercased()
        return temporarySuffixes.contains(where: lower.hasSuffix)
    }

    /// Refuses a filesystem, home, Trash, system or Core-owned selection before
    /// any grant is taken, and asks for a narrower document folder instead.
    public static func isRefusedRoot(_ canonicalRoot: URL, controlledStores: [URL]) -> Bool {
        if refusedRootPaths.contains(canonicalRoot.path) { return true }
        let home = FileManager.default.homeDirectoryForCurrentUser
            .resolvingSymlinksInPath().standardizedFileURL
        if isSame(canonicalRoot, home) { return true }
        if canonicalRoot.pathComponents.contains(where: { $0 == ".Trash" || $0 == ".Trashes" }) {
            return true
        }
        return controlledStores.contains {
            isSame(canonicalRoot, $0) || isContained(canonicalRoot, in: $0)
        }
    }

    /// Component-boundary containment, never a string prefix: `/a/bc` is not
    /// inside `/a/b`. Comparing components also makes a directory URL written
    /// with a trailing slash identical to the same one written without.
    static func isContained(_ candidate: URL, in ancestor: URL) -> Bool {
        let ancestorComponents = ancestor.pathComponents
        let candidateComponents = candidate.pathComponents
        guard candidateComponents.count > ancestorComponents.count else { return false }
        return Array(candidateComponents.prefix(ancestorComponents.count)) == ancestorComponents
    }

    static func isSame(_ candidate: URL, _ other: URL) -> Bool {
        candidate.pathComponents == other.pathComponents
    }

    /// The one bounded traversal both the reviewed preview and the watched-root
    /// scan use, so preview and actual membership cannot disagree. It reads
    /// directory metadata only, applies every exclusion before descending, and
    /// stops at the first limit it reaches rather than accepting a subset.
    ///
    /// Nothing the traversal fails to read is dropped: every failure is kept in
    /// `coverageFailures`, which makes the scan incomplete, and an incomplete
    /// scan is refused rather than submitted as the whole folder.
    static func scan(
        root canonicalRoot: URL,
        recursive: Bool,
        includeHidden: Bool,
        controlledStores: [URL],
        limits: FolderScopeLimits = .standard,
        failureProbe: FolderScanFailureProbe? = nil
    ) throws -> FolderScopeScan {
        let keys: Set<URLResourceKey> = [
            .isRegularFileKey,
            .isDirectoryKey,
            .isSymbolicLinkKey,
            .isAliasFileKey,
            .isHiddenKey,
            .isPackageKey,
            .fileSizeKey,
            .volumeIdentifierKey,
        ]
        guard failureProbe?(canonicalRoot) == nil else {
            throw FolderScopeScanError.rootUnavailable
        }
        var coverageFailures: [FolderScopeCoverageFailure] = []
        guard let rootVolume = try? canonicalRoot
            .resourceValues(forKeys: [.volumeIdentifierKey]).volumeIdentifier,
            let enumerator = FileManager.default.enumerator(
                at: canonicalRoot,
                includingPropertiesForKeys: Array(keys),
                options: [.skipsPackageDescendants],
                errorHandler: { url, _ in
                    // Continuing past a failure is only safe because the
                    // failure is kept: the subtree beneath this location was
                    // never listed, so the scan is incomplete from here on.
                    coverageFailures.append(
                        FolderScopeCoverageFailure(
                            kind: .enumerationFailed,
                            relativeComponents: (try? relativeComponents(
                                root: canonicalRoot,
                                member: url
                            )) ?? []
                        )
                    )
                    return true
                }
            )
        else { throw FolderScopeScanError.rootUnavailable }

        var members: [FolderScopeScanMember] = []
        var exclusionCounts: [FolderExclusionReason: Int] = [:]
        var examinedEntries = 0
        var limit: FolderScopeLimitBreach?
        for case let candidate as URL in enumerator {
            examinedEntries += 1
            if examinedEntries > limits.maximumExaminedEntries {
                limit = .examinedEntries(limits.maximumExaminedEntries)
                break
            }
            let relative = try relativeComponents(root: canonicalRoot, member: candidate)
            if let injected = failureProbe?(candidate) {
                coverageFailures.append(
                    FolderScopeCoverageFailure(kind: injected, relativeComponents: relative)
                )
                enumerator.skipDescendants()
                continue
            }
            // Not an exclusion: with no facts at all the entry's kind is
            // unknown, so this may be a whole subtree that was never examined.
            guard let values = try? candidate.resourceValues(forKeys: keys) else {
                coverageFailures.append(
                    FolderScopeCoverageFailure(kind: .entryUnreadable, relativeComponents: relative)
                )
                enumerator.skipDescendants()
                continue
            }
            let isDirectory = values.isDirectory == true
            let facts = FolderEntryFacts(
                relativeComponents: relative,
                isDirectory: isDirectory,
                isRegularFile: values.isRegularFile == true,
                isSymbolicLink: values.isSymbolicLink == true,
                isAliasFile: values.isAliasFile == true,
                isPackage: values.isPackage == true,
                isHidden: values.isHidden == true,
                onRootVolume: values.volumeIdentifier?.isEqual(rootVolume) == true,
                byteLength: values.fileSize.map(UInt64.init),
                isControlledStore: isControlledStore(candidate, stores: controlledStores)
            )
            switch decide(
                facts,
                recursive: recursive,
                includeHidden: includeHidden,
                limits: limits
            ) {
            case .descend:
                continue
            case let .skip(reason):
                exclusionCounts[reason, default: 0] += 1
                // Exclusions apply before descending, so an unapproved subtree
                // is never even listed.
                if isDirectory { enumerator.skipDescendants() }
            case let .eligible(kind, byteLength):
                guard members.count < limits.maximumEligibleFiles else {
                    limit = .eligibleFiles(limits.maximumEligibleFiles)
                    break
                }
                members.append(
                    FolderScopeScanMember(
                        url: candidate,
                        relativeComponents: relative,
                        displayName: relative.joined(separator: "/"),
                        contentKind: kind,
                        byteLength: byteLength
                    )
                )
            }
            if limit != nil { break }
        }
        // A root that could not be listed is not an empty folder: there is no
        // partial scope to review and nothing to narrow.
        if coverageFailures.contains(where: { $0.relativeComponents.isEmpty }) {
            throw FolderScopeScanError.rootUnavailable
        }
        members.sort { $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending }
        coverageFailures.sort { $0.relativeComponents.lexicographicallyPrecedes($1.relativeComponents) }
        return FolderScopeScan(
            members: members,
            exclusionCounts: exclusionCounts,
            examinedEntries: examinedEntries,
            limit: limit,
            coverageFailures: coverageFailures
        )
    }

    static func isControlledStore(_ url: URL, stores: [URL]) -> Bool {
        guard !stores.isEmpty else { return false }
        let canonical = url.resolvingSymlinksInPath().standardizedFileURL
        return stores.contains { isSame(canonical, $0) || isContained(canonical, in: $0) }
    }

    /// Validated root-relative locator. A received path is never trusted: the
    /// member has to sit under the root this scan actually enumerated.
    static func relativeComponents(root: URL, member: URL) throws -> [String] {
        let rootComponents = root.pathComponents
        let memberComponents = member.standardizedFileURL.pathComponents
        guard memberComponents.count > rootComponents.count,
              Array(memberComponents.prefix(rootComponents.count)) == rootComponents,
              memberComponents.dropFirst(rootComponents.count)
                  .allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." })
        else { throw FolderScopeScanError.unsafeMember }
        return Array(memberComponents.dropFirst(rootComponents.count))
    }
}

enum FolderScopeScanError: Error, Equatable {
    case rootUnavailable
    case unsafeMember
}

struct FolderScopeScanMember: Equatable, Sendable {
    let url: URL
    let relativeComponents: [String]
    let displayName: String
    let contentKind: CaptureContentKind
    let byteLength: UInt64
}

struct FolderScopeScan: Equatable, Sendable {
    let members: [FolderScopeScanMember]
    let exclusionCounts: [FolderExclusionReason: Int]
    let examinedEntries: Int
    let limit: FolderScopeLimitBreach?
    let coverageFailures: [FolderScopeCoverageFailure]

    /// The only state in which this membership may be treated as the folder.
    var isComplete: Bool { limit == nil && coverageFailures.isEmpty }
}

extension CaptureContentKind {
    /// The exact approved qualified-format set for folder discovery. Nothing is
    /// sniffed or executed to find out whether a file might be a document.
    public static func approvedFolderFormat(forName name: String) -> CaptureContentKind? {
        switch (name as NSString).pathExtension.lowercased() {
        case "pdf": .pdf
        case "docx": .docx
        case "xlsx": .xlsx
        case "pptx": .pptx
        case "csv": .csv
        case "tsv": .tsv
        case "rtf": .rtf
        case "html", "htm": .html
        case "md", "markdown": .markdown
        case "txt": .text
        case "png", "jpg", "jpeg", "heic", "tif", "tiff", "webp": .image
        default: nil
        }
    }
}
