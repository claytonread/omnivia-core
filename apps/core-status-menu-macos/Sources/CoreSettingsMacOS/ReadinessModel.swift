// ============================================================================
// OmniVia Core Settings — macOS readiness model (SPEC-CORE-MAC-READINESS-001
// §6–§7, amendment UI-CORE-MAC-READINESS-001).
//
// Deterministic, dependency-free model types. The seven evidence dimensions of
// §7.1 stay separate: applicability, availability, user intent, observed state,
// freshness, operation lifecycle and policy (allowed actions). Nothing here
// flattens them into one permission Boolean, and nothing here talks to the OS.
// ============================================================================

import Foundation

// MARK: - Check identity

/// Closed set of readiness checks (§6.1). Parameterised checks carry an opaque
/// reference the owning feature minted; the readiness layer never resolves one.
public enum ReadinessCheckID: Equatable, Sendable, Hashable {
    case companionLogin
    case coreBackground
    case notificationsDelivery
    case shareExtension
    /// Opaque reference minted by the owning source feature.
    case sourceRead(opaqueRef: String)
    /// Opaque reference minted by the owning backup feature.
    case backupWrite(opaqueRef: String)
    /// Opaque reference minted by the owning connection feature.
    case connectionLocalNetwork(opaqueRef: String)

    var isFeatureScoped: Bool {
        switch self {
        case .companionLogin, .coreBackground, .notificationsDelivery, .shareExtension:
            return false
        case .sourceRead, .backupWrite, .connectionLocalNetwork:
            return true
        }
    }
}

// MARK: - The seven dimensions (§7.1)

/// 1. Applicability: is this check relevant, and did the user choose the feature?
public enum ReadinessApplicability: Equatable, Sendable {
    case notApplicable
    /// The feature exists but the user has not selected it. Neutral, never a failure.
    case optional
    case requiredForSelectedFeature
}

/// 2. Availability: can this adapter produce evidence at all on this installation?
public enum ReadinessAvailability: Equatable, Sendable {
    case supported
    /// The capability is not implemented yet. It is omitted from actionable UI.
    case unimplemented
    case unavailableOnThisOS
    case ownerOffline
}

/// 3. User intent: what the user asked for in Core preferences. Never OS truth.
public enum ReadinessUserIntent: Equatable, Sendable {
    case on
    case off
    case unspecified
}

/// 4. Observed state: what the responsible owner or native query reported.
public enum ReadinessObservedState: Equatable, Sendable {
    case enabled
    case notRegistered
    case requiresApproval
    case notFound
    case allowed
    case denied
    case notDetermined
    case limitedDelivery
    case off
    case accessConfirmed
    case accessUncertain
    case installed
    case notInstalled
    /// A native enum case this build does not know. It can never map to
    /// success (§7.4, MT-042); it degrades honestly.
    case unrecognized(nativeCase: String)
}

/// 5. Freshness of the evidence behind the observed state.
public enum ReadinessFreshness: Equatable, Sendable {
    case currentObservation
    case lastObserved
    case invalidated
    case unknown
}

/// 7.3 Observation categories: what a piece of evidence actually proves.
public enum ReadinessEvidenceKind: Equatable, Sendable {
    case nativeStatusQuery
    case ownerOperationResult
    case qualifiedInstallMetadata
    /// Guidance context only; never a green verified status.
    case userReported
    case unknown
}

/// 6. Operation lifecycle of any explicit action on this check.
public enum ReadinessActivity: Equatable, Sendable {
    case idle
    case checking
    case requesting
    case verifying
    case cancelled
    case unsettled
}

// MARK: - Reason codes (§7.4)

/// Safe, localisable reason codes. Raw NSError text never becomes one of these
/// or a UI action; unknown failures stay `unsupportedState` or
/// `accessDeniedUnclassified` rather than being guessed into a TCC denial.
public enum ReadinessReasonCode: String, Equatable, Sendable {
    case queryTimedOut
    case componentUnavailable
    case installationUnqualified
    case approvalRequired
    case accessDeniedUnclassified
    case resourceMissing
    case resourceOffline
    case readOnlyDestination
    case insufficientSpace
    case tlsFailure
    case authenticationRequired
    case workspaceAccessDenied
    case networkUnavailable
    case settingsLaunchFailed
    case verificationCancelled
    case unsupportedState
    case noIssue
}

// MARK: - Allowed actions (§7.2 policy dimension)

/// Policy: what the current actor may do, independent of the macOS state.
/// The set is closed; a raw URL, path or shell command can never enter it
/// (MT-035 — the navigator takes a typed destination, nothing else).
public enum ReadinessAction: Equatable, Sendable {
    case requestNotifications
    case openLoginItems
    case openNotifications
    case openSystemSettings
    case refreshStatus
    case testSourceAccess(opaqueRef: String)
    case testBackupAccess(opaqueRef: String)
    case testConnection(opaqueRef: String)
    case reselectSource(opaqueRef: String)
}

// MARK: - Evidence and snapshots

public struct ReadinessSubject: Equatable, Sendable {
    /// The component that actually performs the relevant action (§4.3). A
    /// companion read is never attributed to the Core reader.
    let componentRef: String
    let resourceRef: String?
    let operationClass: String
    let identityVerified: Bool
}

public struct ReadinessEvidence: Equatable, Sendable {
    let kind: ReadinessEvidenceKind
    let observedAt: Date?
    let ownerRevision: String?
    let freshness: ReadinessFreshness
    /// Direct for the named subject, or indirect; never promoted across subjects.
    let confidence: String
}

public struct ReadinessCheck: Equatable, Sendable {
    let checkID: ReadinessCheckID
    let subject: ReadinessSubject
    let availability: ReadinessAvailability
    let applicability: ReadinessApplicability
    let userIntent: ReadinessUserIntent
    let observedState: ReadinessObservedState
    let evidence: ReadinessEvidence
    let activity: ReadinessActivity
    let reasonCode: ReadinessReasonCode
    let allowedActions: [ReadinessAction]
    let affectsFeatureRefs: [String]
}

/// Context fencing (§8.3): every observation is bound to the installation,
/// session, target, workspace and configuration revision it was taken under.
/// A result from any other context must be discarded, not applied.
public struct ReadinessContext: Equatable, Sendable {
    public let installationRevision: String
    public let localSessionRef: String
    public let targetRef: String
    public let workspaceRef: String
    public let configurationRevision: String

    public init(
        installationRevision: String,
        localSessionRef: String,
        targetRef: String,
        workspaceRef: String,
        configurationRevision: String
    ) {
        self.installationRevision = installationRevision
        self.localSessionRef = localSessionRef
        self.targetRef = targetRef
        self.workspaceRef = workspaceRef
        self.configurationRevision = configurationRevision
    }
}

public struct ReadinessSnapshot: Equatable, Sendable {
    let generation: Int
    let context: ReadinessContext
    let checks: [ReadinessCheck]
    let observedAt: Date?
}

// MARK: - Native status mappings (§9.1, §9.2)

/// SMAppService status mapped per §9.1. The native enum can grow; an unknown
/// case maps to `unsupportedState`, never to enabled or off.
enum LoginItemNativeStatus: Equatable, Sendable {
    case enabled
    case notRegistered
    case requiresApproval
    case notFound
    case queryFailed
    case unrecognized(nativeCase: String)

    var observedState: ReadinessObservedState {
        switch self {
        case .enabled: return .enabled
        case .notRegistered: return .notRegistered
        case .requiresApproval: return .requiresApproval
        case .notFound: return .notFound
        case .queryFailed: return .accessUncertain
        case .unrecognized: return .unrecognized(nativeCase: "smappservice")
        }
    }

    var reasonCode: ReadinessReasonCode {
        switch self {
        case .enabled: return .noIssue
        case .notRegistered: return .noIssue
        case .requiresApproval: return .approvalRequired
        case .notFound: return .resourceMissing
        case .queryFailed: return .componentUnavailable
        case .unrecognized: return .unsupportedState
        }
    }
}

/// UNAuthorizationStatus + feature settings mapped per §9.2. Authorisation and
/// delivery settings stay separate facts.
enum NotificationNativeStatus: Equatable, Sendable {
    case notDetermined
    case denied
    case authorized(alertsEnabled: Bool, soundsEnabled: Bool, badgesEnabled: Bool)
    case queryFailed
    case unrecognized(nativeCase: String)

    var observedState: ReadinessObservedState {
        switch self {
        case .notDetermined: return .notDetermined
        case .denied: return .denied
        case .authorized: return .allowed
        case .queryFailed: return .accessUncertain
        case .unrecognized: return .unrecognized(nativeCase: "unnotificationsettings")
        }
    }

    var reasonCode: ReadinessReasonCode {
        switch self {
        case .notDetermined: return .noIssue
        case .denied: return .approvalRequired
        case .authorized: return .noIssue
        case .queryFailed: return .componentUnavailable
        case .unrecognized: return .unsupportedState
        }
    }
}
