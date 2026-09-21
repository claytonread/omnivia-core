// ============================================================================
// OmniVia Core Settings — readiness reducer (SPEC-CORE-MAC-READINESS-001 §7.5,
// §9.1–§9.2 mapping). Pure functions only: same input, same output, no OS calls.
// ============================================================================

import Foundation

/// User preferences this feature is allowed to know: what the user selected in
/// Core's own settings. These are Core preference facts, never OS truth.
struct ReadinessPreferences: Equatable, Sendable {
    var startCoreAtLoginSelected: Bool
    var attentionNotificationsSelected: Bool
    var hasWorkspace: Bool

    static let manualLocalOnly = ReadinessPreferences(
        startCoreAtLoginSelected: false,
        attentionNotificationsSelected: false,
        hasWorkspace: false
    )
}

/// The compact macOS access summary (amendment: `cs-sum`) and the General-pane
/// aggregation. Kinds mirror the prototype's summary kinds.
enum ReadinessSummaryKind: Equatable, Sendable {
    /// Optional features are off; nothing is checked as a failure.
    case noAdditionalAccess
    /// Applicable checks produced appropriate confirming evidence.
    case ok
    case checking
    /// At least one applicable selected-feature check is unresolved.
    case attention
    /// Material checks are unknown/stale; no blanket success is claimed (MT-006).
    case needsVerification
}

struct ReadinessIssue: Equatable, Sendable {
    let checkID: ReadinessCheckID
    let title: String
}

struct ReadinessSummary: Equatable, Sendable {
    let kind: ReadinessSummaryKind
    let headline: String
    let detail: String?
    let issues: [ReadinessIssue]
    let unverifiedSubjects: [String]
}

enum ReadinessReducer {
    /// Failure reason codes that mean a selected feature is genuinely blocked.
    private static let failureCodes: Set<ReadinessReasonCode> = [
        .approvalRequired,
        .accessDeniedUnclassified,
        .resourceMissing,
        .resourceOffline,
        .readOnlyDestination,
        .insufficientSpace,
        .tlsFailure,
        .authenticationRequired,
        .workspaceAccessDenied,
        .networkUnavailable,
    ]

    /// Applicability resolution (§6.2): checks derive from what the user
    /// selected. A feature that is installed but not chosen stays `optional`.
    /// Unimplemented conditional features are dropped entirely (MT-007) —
    /// they never become a "needs setup" checklist row.
    static func applicableChecks(
        preferences: ReadinessPreferences,
        candidates: [ReadinessCheck]
    ) -> [ReadinessCheck] {
        candidates.compactMap { check -> ReadinessCheck? in
            if check.availability == .unimplemented { return nil }
            let applicability = applicability(of: check, preferences: preferences)
            if applicability == .notApplicable { return nil }
            return ReadinessCheck(
                checkID: check.checkID,
                subject: check.subject,
                availability: check.availability,
                applicability: applicability,
                userIntent: check.userIntent,
                observedState: check.observedState,
                evidence: check.evidence,
                activity: check.activity,
                reasonCode: check.reasonCode,
                allowedActions: check.allowedActions,
                affectsFeatureRefs: check.affectsFeatureRefs
            )
        }
    }

    static func applicability(
        of check: ReadinessCheck,
        preferences: ReadinessPreferences
    ) -> ReadinessApplicability {
        switch check.checkID {
        case .companionLogin, .coreBackground:
            // Startup registration matters only if the user asked for it
            // (MT-005): manual startup is a legitimate choice, not a fault.
            return preferences.startCoreAtLoginSelected
                ? .requiredForSelectedFeature : .optional
        case .notificationsDelivery:
            return preferences.attentionNotificationsSelected
                ? .requiredForSelectedFeature : .optional
        case .sourceRead, .backupWrite:
            return preferences.hasWorkspace ? .requiredForSelectedFeature : .notApplicable
        case .connectionLocalNetwork:
            return preferences.hasWorkspace ? .optional : .notApplicable
        case .shareExtension:
            // Capture/Share is a separately gated capability (D08): the check
            // exists only when the accepted capture feature is installed. This
            // build has none, so it is simply absent from the applicable set.
            return .notApplicable
        }
    }

    /// Summary aggregation (§7.5). Confirmed unresolved issues win; otherwise
    /// material unknown/stale results yield "needs verification"; only complete
    /// appropriate evidence permits the clean summary. A user who chose manual
    /// startup and no notifications gets the neutral no-additional-access line,
    /// never a setup failure (MT-005).
    static func summarize(
        checks: [ReadinessCheck],
        preferences: ReadinessPreferences,
        refreshInFlight: Bool,
        everObserved: Bool
    ) -> ReadinessSummary {
        let applicable = applicableChecks(preferences: preferences, candidates: checks)

        var issues: [ReadinessIssue] = []
        var unverified: [String] = []

        for check in applicable where check.applicability == .requiredForSelectedFeature {
            switch check.availability {
            case .unimplemented, .unavailableOnThisOS:
                continue // MT-007: absent or truthfully unavailable, never actionable
            case .ownerOffline:
                unverified.append(displayName(for: check.checkID))
            case .supported:
                if check.activity == .checking || check.evidence.freshness == .unknown {
                    unverified.append(displayName(for: check.checkID))
                } else if failureCodes.contains(check.reasonCode) {
                    issues.append(
                        ReadinessIssue(
                            checkID: check.checkID,
                            title: issueTitle(for: check)
                        )
                    )
                }
                // A `userReported` evidence kind never produces a green verified
                // status; treat it as unverified material.
                if check.evidence.kind == .userReported {
                    unverified.append(displayName(for: check.checkID))
                }
                // An unrecognized native state or unclassified failure is
                // uncertainty, never success or denial (§7.4, MT-042).
                if check.reasonCode == .unsupportedState
                    || check.reasonCode == .accessDeniedUnclassified
                    || check.reasonCode == .componentUnavailable {
                    unverified.append(displayName(for: check.checkID))
                }
            }
        }

        if !issues.isEmpty {
            let detail = issues.count == 1 ? issues[0].title : nil
            return ReadinessSummary(
                kind: .attention,
                headline: issues.count == 1
                    ? "One selected feature needs attention."
                    : "\(issues.count) selected features need attention.",
                detail: detail,
                issues: issues,
                unverifiedSubjects: []
            )
        }

        if refreshInFlight && !everObserved {
            return ReadinessSummary(
                kind: .checking,
                headline: "Checking macOS status…",
                detail: "The window stays usable. Nothing is requested or started.",
                issues: [],
                unverifiedSubjects: []
            )
        }

        if applicable.isEmpty || applicable.allSatisfy({ $0.applicability == .optional }) {
            return ReadinessSummary(
                kind: .noAdditionalAccess,
                headline: "No additional macOS access is needed for your current setup.",
                detail: "Optional features you haven't chosen aren't checked.",
                issues: [],
                unverifiedSubjects: []
            )
        }

        if !unverified.isEmpty {
            return ReadinessSummary(
                kind: .needsVerification,
                headline: "Some access checks need verification.",
                detail: "Not checked: " + unverified.joined(separator: ", ") + ".",
                issues: [],
                unverifiedSubjects: unverified
            )
        }

        return ReadinessSummary(
            kind: .ok,
            headline: "No macOS issues detected for your current setup.",
            detail: nil,
            issues: [],
            unverifiedSubjects: []
        )
    }

    /// Startup registration and service health stay separate facts (§9.1,
    /// MT-010): an enabled registration says nothing about the runtime.
    static func startupRow(check: ReadinessCheck?, serviceRunning: Bool?) -> StartupRow {
        let registration: String
        var detail: String?
        switch check?.observedState {
        case .enabled:
            registration = "Enabled"
            if serviceRunning == false { detail = "The Core service is not running right now." }
        case .requiresApproval:
            registration = "Needs approval"
        case .notRegistered, .notInstalled, .off, nil:
            registration = "Not set up"
        case .notFound:
            registration = "Not set up"
            detail = "The registration could not be found; this is an installation matter, not a permission denial."
        case .unrecognized:
            registration = "Not checked"
            detail = "macOS reported a state this version doesn't recognise."
        default:
            registration = "Not checked"
        }
        return StartupRow(registration: registration, detail: detail)
    }

    struct StartupRow: Equatable, Sendable {
        let registration: String
        let detail: String?
    }

    static func displayName(for checkID: ReadinessCheckID) -> String {
        switch checkID {
        case .companionLogin: return "companion login"
        case .coreBackground: return "Core startup"
        case .notificationsDelivery: return "notifications"
        case .shareExtension: return "Share to Core"
        case .sourceRead(let ref): return "source \(ref)"
        case .backupWrite(let ref): return "backup destination \(ref)"
        case .connectionLocalNetwork(let ref): return "connection \(ref)"
        }
    }

    private static func issueTitle(for check: ReadinessCheck) -> String {
        switch check.checkID {
        case .coreBackground, .companionLogin:
            return check.reasonCode == .approvalRequired
                ? "Start Core at login needs approval in Login Items"
                : "Core startup needs attention"
        case .notificationsDelivery:
            switch check.observedState {
            case .denied: return "Notifications are blocked in macOS"
            case .limitedDelivery: return "Alerts are off in macOS; only badges are allowed"
            default: return "Notifications need your permission"
            }
        case .sourceRead(let ref): return "Source \(ref) needs attention"
        case .backupWrite: return "Backup destination needs attention"
        case .connectionLocalNetwork(let ref): return "Connection \(ref) needs attention"
        case .shareExtension: return "Share to Core needs attention"
        }
    }
}
