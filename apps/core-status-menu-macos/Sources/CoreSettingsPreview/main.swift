// ============================================================================
// Standalone launcher for the Core Settings window.
//
// Opens the native AppKit settings window on its own, without the status-menu
// companion, so the seven panes can be looked at and driven during
// development. It is not a product surface and ships in no bundle.
//
//   swift run --package-path apps/core-status-menu-macos omnivia-core-settings
//
// It hosts the same `SettingsWindowController` the companion opens, with no
// alternate rendering path: whatever appears here is what the companion shows.
// The context below is this launcher's own, and is visibly synthetic so a
// screenshot taken from it can never be mistaken for a real installation.
// ============================================================================

import AppKit
import Foundation
import CoreSettingsMacOS

// Top-level code runs on the process's main thread; the settings types are
// MainActor-isolated, so state that rather than hopping through an async
// entry point.
MainActor.assumeIsolated {
    let application = NSApplication.shared
    application.setActivationPolicy(.regular)
    // The brand app icon (Resources/brand/omnivia-appicon.svg).
    if let icon = BrandAssets.image(named: "omnivia-appicon") {
        application.applicationIconImage = icon
    }

    // Optional, so the preview can exercise a real installation:
    //   --cli <path to omnivia> --installation-state <path> [--workspace-id <id>]
    // Without them the window opens against a synthetic context and the
    // Access pane says it has no route to the CLI, which is the truth.
    func argument(_ name: String) -> String? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
        return args[i + 1]
    }
    var mcp: McpAdministering?
    // Dev-only: a roster with the service reachable, so the Access pane and
    // the Add-application sheet can be looked at on a machine with no Core
    // running. Nothing here reaches a service: `configure` returns a snippet
    // of the real shape over this launcher's own synthetic path, so what a
    // screenshot shows can never be mistaken for a real installation's.
    if CommandLine.arguments.contains("--stub-mcp") {
        mcp = StubMcp()
    } else if let cli = argument("--cli"), let state = argument("--installation-state") {
        mcp = McpCommandRunner(
            executable: URL(fileURLWithPath: cli),
            installationState: URL(fileURLWithPath: state)
        )
    }

    // The workspace a configure would be bound to. Synthetic unless given,
    // so the launcher cannot quietly grant access to a real one: the companion
    // takes the same `--workspace-id`.
    let workspace = argument("--workspace-id") ?? "preview-workspace"
    let coordinator = ReadinessCoordinator(
        context: ReadinessContext(
            installationRevision: "preview-install",
            localSessionRef: UUID().uuidString,
            targetRef: "local",
            workspaceRef: workspace,
            configurationRevision: "1"
        )
    )
    // Optional, so the Document processing pane can be exercised against a
    // real installation's managed OCR cache:
    //   --service-cli <path to omnivia-core-service> --installation-state <path>
    // Without them the pane says it has no route to the service, which is the
    // truth here. There is no stub: a fabricated language inventory in a
    // screenshot would be a claim about installed bytes.
    let ocr = (argument("--service-cli").map { URL(fileURLWithPath: $0) })
        .flatMap { service in
            argument("--installation-state").flatMap { state in
                OcrResourceCommandRunner(
                    executable: service,
                    installationState: URL(fileURLWithPath: state)
                )
            }
        }

    // Optional, so the Document processing pane's two preference rows can be
    // exercised against a real workspace:
    //   --cli <path to omnivia> --installation-state <path> --workspace-id <id>
    // Without them the pane says it has no route to the client, which is the
    // truth here. There is no stub: fabricated preferences in a screenshot
    // would be a claim about what a future capture will do.
    let documentProcessing = (argument("--cli").map { URL(fileURLWithPath: $0) })
        .flatMap { cli in
            argument("--installation-state").flatMap { state in
                DocumentProcessingCommandRunner(
                    executable: cli,
                    installationState: URL(fileURLWithPath: state),
                    workspaceRef: workspace
                )
            }
        }

    // Optional, so General > Updates can run its read-only check against a real
    // installation:
    //   --cli <path to omnivia> --installation-state <path> --workspace-id <id>
    // The CLI needs the installation state and a workspace to run a `service`
    // command at all. Without --cli the row says it has no route, which is the
    // truth here. There is no stub: a fabricated result would be a claim about
    // what the channel recommends.
    let updates = (argument("--cli").map { URL(fileURLWithPath: $0) })
        .flatMap { cli in
            argument("--installation-state").flatMap { state in
                CoreUpdateCommandRunner(
                    executable: cli,
                    installationState: URL(fileURLWithPath: state),
                    workspaceRef: workspace
                )
            }
        }

    let controller = SettingsWindowController(
        coordinator: coordinator,
        mcp: mcp,
        ocr: ocr,
        documentProcessing: documentProcessing,
        updates: updates
    )

    // Optional, so the sidebar footer's service states can be looked at
    // without a companion to report them:
    //   --service-state running|stopped|unreachable|starting|stopping|failed
    // Without it the footer says it has no feed, which is the truth here.
    if let name = argument("--service-state") {
        let state: CoreServiceState?
        switch name {
        case "running": state = .running
        case "stopped": state = .stopped
        case "starting": state = .starting
        case "stopping": state = .stopping
        case "failed": state = .failed
        case "unreachable":
            // A fixed time, so this launcher's output is reproducible.
            var when = Calendar.current.dateComponents([.year, .month, .day], from: Date())
            when.hour = 9
            when.minute = 41
            state = .unreachable(lastSeen: Calendar.current.date(from: when))
        default: state = nil
        }
        guard let state else {
            FileHandle.standardError.write(Data("unknown --service-state \(name)\n".utf8))
            exit(2)
        }
        controller.updateServiceState(state, host: "This Mac")
    }

    controller.openOrFocus()
    application.run()
}


/// Answers without reaching a service. See `--stub-mcp` above.
struct StubMcp: McpAdministering {
    func status() async -> Result<McpStatus, McpError> {
        .success(
            McpStatus(
                version: 1,
                hosts: [
                    McpHostStatus(
                        host: "claude-code", configuration: .absent, credential: .absent,
                        grant: .absent, profile: nil, principalID: nil, workspaceID: nil,
                        serviceReachable: true, advertisedToolCount: nil, authoringIntent: nil
                    ),
                    McpHostStatus(
                        host: "codex", configuration: .present, credential: .present,
                        grant: .active, profile: .authoring, principalID: "mcp-codex-preview",
                        workspaceID: "preview-workspace", serviceReachable: true,
                        advertisedToolCount: 11, authoringIntent: true
                    ),
                ]
            )
        )
    }

    /// The shape `host_snippet` prints, over a path that is visibly this
    /// launcher's rather than any installation's.
    func configure(host: String, workspace: String, profile: String) async -> Result<String, McpError> {
        let path = "/preview-installation/.installed-mcp/\(host).json"
        if host == "codex" {
            return .success(
                """
                [mcp_servers.omnivia-core]
                command = "omnivia-core-mcp"
                args = ["--config", "\(path)"]
                """
            )
        }
        return .success(
            """
            {
              "mcpServers": {
                "omnivia-core": {
                  "command": "omnivia-core-mcp",
                  "args": [
                    "--config",
                    "\(path)"
                  ]
                }
              }
            }
            """
        )
    }

    func revoke(host: String) async -> Result<Void, McpError> { .success(()) }
}
