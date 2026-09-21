// ============================================================================
// OmniVia Core Settings — allowlisted System Settings navigation (§10).
//
// The navigator accepts a closed destination identifier — never a URL, bundle
// ID, path, scheme or shell fragment from a client, model or source (MT-035).
// Resolution order: official dedicated API where it applies (SMAppService
// login-items route), then the generic System Settings application with manual
// instructions. A launch means "navigation requested", never "permission
// granted" (MT-033).
// ============================================================================

import AppKit
import Foundation
import ServiceManagement

enum SettingsDestination: String, Equatable, Sendable {
    case loginItems
    case notifications
    case filesAndFolders
    case localNetwork
    case firewall
    case sharingExtensions
    /// Only through the exceptional broad-access source policy (§9.8).
    case fullDiskAccess
    case privacyAndSecurity
    /// Always-permitted generic fallback: navigation, not a permission grant.
    case systemSettings
}

enum NavigationResult: Equatable, Sendable {
    /// The dedicated API reported it asked the OS to open the pane.
    case navigationRequested
    /// The System Settings application was launched via generic discovery.
    case genericSettingsOpened
    case launchFailed
    case unsupportedDestination
    case cancelled
}

struct SettingsNavigator {
    /// The affected component's display name for manual instructions. Display
    /// data only — never executable input (§10.1).
    let componentName: String

    /// §10.2 resolution order. `openSystemSettingsLoginItems` is the one
    /// qualified dedicated API in this build; everything else uses the generic
    /// fallback with version-appropriate manual instructions. No private
    /// x-apple.systempreferences links are shipped unqualified.
    func open(_ destination: SettingsDestination) async -> (NavigationResult, String?) {
        switch destination {
        case .loginItems:
            if #available(macOS 13.0, *) {
                do {
                    try SMAppService.openSystemSettingsLoginItems()
                    return (.navigationRequested, nil)
                } catch {
                    return await openGeneric(instructionsFor: destination)
                }
            }
            return await openGeneric(instructionsFor: destination)
        default:
            return await openGeneric(instructionsFor: destination)
        }
    }

    /// Generic fallback (§10.2 step 3): resolve System Settings through the
    /// platform's application discovery — never an assumed absolute path —
    /// and return the manual route for display.
    private func openGeneric(instructionsFor destination: SettingsDestination) async -> (NavigationResult, String?) {
        guard let url = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: "com.apple.systempreferences"
        ) else {
            return (.launchFailed, manualRoute(for: destination))
        }
        let configuration = NSWorkspace.OpenConfiguration()
        do {
            try await NSWorkspace.shared.openApplication(at: url, configuration: configuration)
            return (.genericSettingsOpened, manualRoute(for: destination))
        } catch {
            return (.launchFailed, manualRoute(for: destination))
        }
    }

    /// Manual routes (§10.3). English reference labels for this build's
    /// supported release; localisation and per-release qualification are the
    /// route registry's job.
    func manualRoute(for destination: SettingsDestination) -> String? {
        switch destination {
        case .loginItems:
            return "System Settings → General → Login Items & Extensions. Review \"\(componentName)\" under Allow in the background."
        case .notifications:
            return "System Settings → Notifications → \(componentName). Turn on Allow notifications and Alerts."
        case .filesAndFolders:
            return "System Settings → Privacy & Security → Files & Folders. Review folder access for \"\(componentName)\"."
        case .localNetwork:
            return "System Settings → Privacy & Security → Local Network. Allow \"\(componentName)\"."
        case .firewall:
            return "System Settings → Network → Firewall → Options, where applicable."
        case .sharingExtensions:
            return "System Settings → General → Login Items & Extensions → Sharing. Turn on Share to Core."
        case .fullDiskAccess:
            return "System Settings → Privacy & Security → Full Disk Access."
        case .privacyAndSecurity, .systemSettings:
            return "Open System Settings and review Privacy & Security."
        }
    }
}
