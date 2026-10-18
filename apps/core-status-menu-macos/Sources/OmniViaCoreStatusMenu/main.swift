import AppKit
import CoreSettingsMacOS
import Darwin
import Foundation

@MainActor
final class StatusMenuAppDelegate: NSObject, NSApplicationDelegate {
    private let configuration: CompanionConfiguration
    private let singleton: CompanionSingleton
    private let coordinator: LifecycleCoordinator
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let titleItem = NSMenuItem(title: "OmniVia Core — Checking…", action: nil, keyEquivalent: "")
    private let detailItem = NSMenuItem(title: "Contacting the Core Service", action: nil, keyEquivalent: "")
    private let refreshItem = NSMenuItem(title: "Refresh", action: #selector(refresh), keyEquivalent: "r")
    private let startItem = NSMenuItem(title: "Start Service", action: #selector(startService), keyEquivalent: "")
    private let stopItem = NSMenuItem(title: "Stop Service", action: #selector(stopService), keyEquivalent: "")
    private let logItem = NSMenuItem(title: "Show Service Log", action: #selector(showServiceLog), keyEquivalent: "l")
    private let settingsItem = NSMenuItem(title: "Core Settings…", action: #selector(openSettings), keyEquivalent: ",")
    private var pollTimer: Timer?
    /// One owned Settings window (MR-001). Created on first open; closing it
    /// never touches Core lifecycle.
    private var settingsWindowController: SettingsWindowController?
    /// The most recent poll, so a window opened between polls starts with the
    /// state already known rather than waiting out the poll interval.
    private var lastSnapshot: MenuSnapshot?
    /// When Core was last polled as running. Held here, not in the settings
    /// window, because this companion runs continuously while that window
    /// spends most of its life closed.
    private var lastSeenRunning: Date?
    /// The same resolved `omnivia` the lifecycle runner uses. Held so the
    /// Settings window can administer installed MCP hosts through it; nil when
    /// the CLI could not be found, which the Access pane then states.
    private let mcp: McpAdministering?

    init(configuration: CompanionConfiguration, singleton: CompanionSingleton) {
        self.configuration = configuration
        self.singleton = singleton
        let runner: LifecycleRunning
        var mcp: McpAdministering?
        do {
            let executable = try CLIResolver().resolve(override: configuration.cliOverride)
            runner = CLICommandRunner(
                executable: executable,
                installationState: configuration.installationState,
                workspaceID: configuration.workspaceID
            )
            // `mcp` commands take no workspace id — a host's setup names its
            // own workspace — so this needs only the installation state.
            mcp = McpCommandRunner(
                executable: executable,
                installationState: configuration.installationState
            )
        } catch {
            runner = UnavailableLifecycleRunner()
        }
        self.mcp = mcp
        coordinator = LifecycleCoordinator(runner: runner)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMenu()
        coordinator.onChange = { [weak self] snapshot in
            self?.render(snapshot)
        }
        singleton.startListening { [weak self] in
            self?.coordinator.refresh()
        }
        render(.checking)
        coordinator.refresh()

        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.coordinator.refresh()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
    }

    func applicationWillTerminate(_ notification: Notification) {
        // The status menu is a companion, not the service owner. Quitting it
        // deliberately performs no Core lifecycle command.
        pollTimer?.invalidate()
        coordinator.companionWillTerminate()
        singleton.close()
    }

    private func configureMenu() {
        menu.autoenablesItems = false
        titleItem.isEnabled = false
        detailItem.isEnabled = false
        for item in [refreshItem, startItem, stopItem, logItem, settingsItem] {
            item.target = self
        }

        menu.addItem(titleItem)
        menu.addItem(detailItem)
        menu.addItem(.separator())
        menu.addItem(settingsItem)
        menu.addItem(.separator())
        menu.addItem(refreshItem)
        menu.addItem(startItem)
        menu.addItem(stopItem)
        menu.addItem(logItem)
        menu.addItem(.separator())
        let quit = NSMenuItem(
            title: "Quit Status Menu",
            action: #selector(quitStatusMenu),
            keyEquivalent: "q"
        )
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
    }

    private func render(_ snapshot: MenuSnapshot) {
        lastSnapshot = snapshot
        if case .status(let status) = snapshot.condition, status.lifecycleState == .running,
           status.connectionState != .unreachable {
            lastSeenRunning = Date()
        }
        let projection = MenuProjection.project(snapshot)
        titleItem.title = projection.title
        detailItem.title = projection.detail
        refreshItem.isEnabled = projection.refreshEnabled
        startItem.isEnabled = projection.startEnabled
        stopItem.isEnabled = projection.stopEnabled

        guard let button = statusItem.button else { return }
        if let image = NSImage(
            systemSymbolName: projection.symbolName,
            accessibilityDescription: projection.title
        ) {
            image.isTemplate = true
            button.image = image
            button.title = ""
        } else {
            button.image = nil
            button.title = "O"
        }
        button.toolTip = "\(projection.title) — \(projection.detail)"
        reportServiceState(snapshot)
    }

    /// Forward the state this companion already polls to the settings window's
    /// sidebar footer. The window invokes no lifecycle command itself (MR-005),
    /// so the state is pushed here rather than fetched there.
    private func reportServiceState(_ snapshot: MenuSnapshot) {
        guard let controller = settingsWindowController else { return }
        switch snapshot.condition {
        case .checking:
            // The first poll, before any status: leave the footer as it was
            // rather than flicker a state we do not have yet.
            break
        case .unavailable:
            // The adapter itself could not be run, so Core's state is unknown
            // — which is not the same as Core being unreachable or stopped.
            controller.updateServiceState(.unknown)
        case .status(let status):
            let state: CoreServiceState
            if status.connectionState == .unreachable {
                state = .unreachable(lastSeen: lastSeenRunning)
            } else {
                switch status.lifecycleState {
                case .running: state = .running
                case .starting: state = .starting
                case .stopping: state = .stopping
                case .stopped: state = .stopped
                case .failed: state = .failed
                case .unknown: state = .unknown
                }
            }
            controller.updateServiceState(state, host: status.target.displayName)
        }
    }

    @objc private func refresh() {
        coordinator.refresh()
    }

    @objc private func openSettings() {
        if settingsWindowController == nil {
            let context = ReadinessContext(
                installationRevision: configuration.installationState.path,
                localSessionRef: UUID().uuidString,
                targetRef: "local",
                workspaceRef: configuration.workspaceID,
                configurationRevision: "1"
            )
            settingsWindowController = SettingsWindowController(
                coordinator: ReadinessCoordinator(context: context),
                mcp: mcp
            )
        }
        if let lastSnapshot { reportServiceState(lastSnapshot) }
        settingsWindowController?.openOrFocus()
    }

    @objc private func startService() {
        coordinator.start()
    }

    @objc private func stopService() {
        coordinator.stop()
    }

    @objc private func showServiceLog() {
        let installationHome = configuration.installationState.deletingLastPathComponent()
        let runDirectory = installationHome.appendingPathComponent("run", isDirectory: true)
        let log = runDirectory.appendingPathComponent("service.log")
        var isDirectory: ObjCBool = false

        if FileManager.default.fileExists(atPath: log.path, isDirectory: &isDirectory) {
            NSWorkspace.shared.activateFileViewerSelecting([log])
            return
        }
        for directory in [runDirectory, installationHome] {
            if FileManager.default.fileExists(atPath: directory.path, isDirectory: &isDirectory),
               isDirectory.boolValue
            {
                NSWorkspace.shared.open(directory)
                return
            }
        }

        // A fixed phrase: the companion may reveal a log that already exists, but
        // it names no path of its own — an installation location is not a word
        // this process puts on screen.
        let alert = NSAlert()
        alert.messageText = "No Core service log yet"
        alert.informativeText = "Start Core once to create the service log."
        alert.alertStyle = .informational
        alert.runModal()
    }

    @objc private func quitStatusMenu() {
        NSApplication.shared.terminate(nil)
    }
}

do {
    // Top-level `main.swift` code runs on the process's start thread, which is
    // the main thread; `assumeIsolated` states that rather than hopping through
    // an async entry point just to satisfy the isolation checker.
    try MainActor.assumeIsolated {
        let configuration = try CompanionConfiguration.parse(arguments: CommandLine.arguments)
        let singleton: CompanionSingleton
        switch try CompanionSingleton.acquire(installationState: configuration.installationState) {
        case .forwarded:
            return
        case let .primary(acquired):
            singleton = acquired
        }
        let application = NSApplication.shared
        let delegate = StatusMenuAppDelegate(configuration: configuration, singleton: singleton)
        application.setActivationPolicy(.accessory)
        application.delegate = delegate
        application.run()
    }
} catch is CompanionConfigurationError {
    // One fixed usage line. Even the companion's own argument errors echo the
    // words they were given, and this process writes none of those back.
    FileHandle.standardError.write(
        Data("usage: omnivia-core-status-menu [--cli <absolute path>] --installation-state <absolute path> --workspace-id <id>\n".utf8)
    )
    Darwin.exit(2)
} catch {
    FileHandle.standardError.write(Data("OmniVia Core status menu is unavailable\n".utf8))
    Darwin.exit(1)
}
