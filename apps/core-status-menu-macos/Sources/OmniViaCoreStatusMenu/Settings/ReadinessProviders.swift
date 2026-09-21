// ============================================================================
// OmniVia Core Settings — passive readiness providers (§8.4, §9, §12.1).
//
// The provider protocol is deliberately incapable of side effects: there is no
// method here that can prompt, register, probe, connect or write. Effectful
// recovery lives behind the explicit-action path in the window controller and
// is gated on trusted interaction. That separation is enforced structurally
// (a provider cannot mutate anything), not by comments.
//
// Binding dispositions (§19): no qualified startup-registration mechanism
// exists in this installation, so the login/background providers report honest
// unimplemented states rather than guessing a bundle ID. The notification
// query is the documented passive read for this process's own notification
// centre and is the one qualified native adapter in R1.
// ============================================================================

import AppKit
import Foundation
import UserNotifications

/// What a passive provider produced for one subject.
enum ReadinessProviderOutcome: Equatable, Sendable {
    case observed(ReadinessObservedState, ReadinessEvidence, ReadinessReasonCode)
    /// The adapter is known not to exist on this installation. Honest absence.
    case unimplemented(explanation: String)
    case failed(code: ReadinessReasonCode)
}

protocol ReadinessProviding: Sendable {
    var checkID: ReadinessCheckID { get }
    /// Read the supported status for the named subject. Must not prompt, probe,
    /// register, connect or write (§8.4). Called only by the coordinator.
    func read() async -> ReadinessProviderOutcome
}

// MARK: - Notification authorisation (qualified: §9.2, A05–A07)

/// Reads the sender's own notification settings. Querying never requests
/// authorisation (MT-013): `UNUserNotificationCenter.getNotificationSettings`
/// is documented as passive; `requestAuthorization` is a separate explicit
/// action owned by the window, not by this provider.
struct NotificationReadinessProvider: ReadinessProviding {
    let checkID: ReadinessCheckID = .notificationsDelivery

    private let center: UNUserNotificationCenter?

    init(center: UNUserNotificationCenter? = UNUserNotificationCenter.current()) {
        self.center = center
    }

    func read() async -> ReadinessProviderOutcome {
        guard let center else {
            return .unimplemented(
                explanation: "This build has no notification centre identity."
            )
        }
        let settings = await center.notificationSettings()
        let native: NotificationNativeStatus
        switch settings.authorizationStatus {
        case .notDetermined:
            native = .notDetermined
        case .denied:
            native = .denied
        case .authorized, .provisional, .ephemeral:
            native = .authorized(
                alertsEnabled: settings.alertSetting == .enabled,
                soundsEnabled: settings.soundSetting == .enabled,
                badgesEnabled: settings.badgeSetting == .enabled
            )
        @unknown default:
            // Unknown future cases degrade honestly (MT-042): no invented ready state.
            native = .unrecognized(nativeCase: "unauthorizationstatus")
        }
        let evidence = ReadinessEvidence(
            kind: .nativeStatusQuery,
            observedAt: Date(),
            ownerRevision: nil,
            freshness: .currentObservation,
            confidence: "directForNamedSubject"
        )
        switch native {
        case .authorized(let alerts, _, _):
            if !alerts {
                // Authorised but alerts restricted: material limitation shown,
                // delivery certainty never claimed (MT-015).
                return .observed(.limitedDelivery, evidence, .noIssue)
            }
            return .observed(native.observedState, evidence, native.reasonCode)
        default:
            return .observed(native.observedState, evidence, native.reasonCode)
        }
    }
}

// MARK: - Startup registration (§9.1)

/// Binding disposition: this installation has no accepted
/// service-management registration (no SMAppService declaration, no login
/// helper, no launchd plist owned by this package). Per §19 the safe default
/// is to read nothing and show an installation-honest state rather than
/// attribute a status to a guessed service.
struct LoginItemReadinessProvider: ReadinessProviding {
    let checkID: ReadinessCheckID

    init(checkID: ReadinessCheckID) {
        self.checkID = checkID
    }

    func read() async -> ReadinessProviderOutcome {
        .unimplemented(
            explanation: "No qualified startup-registration mechanism is installed for this component."
        )
    }
}

// MARK: - Not-yet-qualified feature adapters (§6.1, §19)

/// Sources, backups, connections, hosting, capture and Share are separately
/// gated capabilities (D08). No owner seam exists in this build, so these
/// adapters report honest unimplemented states and the UI omits or shows them
/// as neutral — never as broken toggles (MT-007).
struct UnimplementedFeatureProvider: ReadinessProviding {
    let checkID: ReadinessCheckID

    func read() async -> ReadinessProviderOutcome {
        .unimplemented(explanation: "The owning feature is not installed in this build.")
    }
}

// MARK: - Provider registry (§6.1 applicability)

enum ReadinessProviderRegistry {
    /// All candidate checks this build can describe. The coordinator runs at
    /// most four concurrently (§12.3) and the reducer drops what is not
    /// applicable or unimplemented.
    static func providers() -> [ReadinessProviding] {
        [
            LoginItemReadinessProvider(checkID: .companionLogin),
            LoginItemReadinessProvider(checkID: .coreBackground),
            NotificationReadinessProvider(),
            UnimplementedFeatureProvider(checkID: .shareExtension),
        ]
    }

    static func baseCheck(for provider: ReadinessProviding) -> ReadinessCheck {
        ReadinessCheck(
            checkID: provider.checkID,
            subject: ReadinessSubject(
                componentRef: "omnivia-core-status-menu",
                resourceRef: nil,
                operationClass: operationClass(for: provider.checkID),
                identityVerified: true
            ),
            availability: .supported,
            applicability: .optional,
            userIntent: .unspecified,
            observedState: .accessUncertain,
            evidence: ReadinessEvidence(
                kind: .unknown,
                observedAt: nil,
                ownerRevision: nil,
                freshness: .unknown,
                confidence: "none"
            ),
            activity: .idle,
            reasonCode: .unsupportedState,
            allowedActions: allowedActions(for: provider.checkID),
            affectsFeatureRefs: []
        )
    }

    static func operationClass(for checkID: ReadinessCheckID) -> String {
        switch checkID {
        case .companionLogin, .coreBackground: return "startupRegistration"
        case .notificationsDelivery: return "notificationAuthorisation"
        case .sourceRead: return "sourceRead"
        case .backupWrite: return "backupWrite"
        case .connectionLocalNetwork: return "localNetworkConnection"
        case .shareExtension: return "shareExtension"
        }
    }

    static func allowedActions(for checkID: ReadinessCheckID) -> [ReadinessAction] {
        switch checkID {
        case .companionLogin, .coreBackground:
            return [.openLoginItems, .refreshStatus]
        case .notificationsDelivery:
            return [.requestNotifications, .openNotifications, .refreshStatus]
        case .sourceRead(let ref):
            return [.testSourceAccess(opaqueRef: ref), .reselectSource(opaqueRef: ref), .refreshStatus]
        case .backupWrite(let ref):
            return [.testBackupAccess(opaqueRef: ref), .refreshStatus]
        case .connectionLocalNetwork(let ref):
            return [.testConnection(opaqueRef: ref), .refreshStatus]
        case .shareExtension:
            return [.refreshStatus]
        }
    }
}
