import AppKit
import Darwin
import Foundation

@MainActor
final class StatusMenuAppDelegate: NSObject, NSApplicationDelegate {
    private let configuration: CompanionConfiguration
    private let coordinator: LifecycleCoordinator
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let titleItem = NSMenuItem(title: "OmniVia Core — Checking…", action: nil, keyEquivalent: "")
    private let detailItem = NSMenuItem(title: "Contacting the Core Service", action: nil, keyEquivalent: "")
    private let refreshItem = NSMenuItem(title: "Refresh", action: #selector(refresh), keyEquivalent: "r")
    private let startItem = NSMenuItem(title: "Start Service", action: #selector(startService), keyEquivalent: "")
    private let stopItem = NSMenuItem(title: "Stop Service", action: #selector(stopService), keyEquivalent: "")
    private let logItem = NSMenuItem(title: "Show Service Log", action: #selector(showServiceLog), keyEquivalent: "l")
    private var pollTimer: Timer?

    init(configuration: CompanionConfiguration) {
        self.configuration = configuration
        let runner: LifecycleRunning
        do {
            let executable = try CLIResolver().resolve(override: configuration.cliOverride)
            runner = CLICommandRunner(
                executable: executable,
                installationState: configuration.installationState,
                workspaceID: configuration.workspaceID
            )
        } catch {
            runner = UnavailableLifecycleRunner()
        }
        coordinator = LifecycleCoordinator(runner: runner)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMenu()
        coordinator.onChange = { [weak self] snapshot in
            self?.render(snapshot)
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
    }

    private func configureMenu() {
        menu.autoenablesItems = false
        titleItem.isEnabled = false
        detailItem.isEnabled = false
        for item in [refreshItem, startItem, stopItem, logItem] {
            item.target = self
        }

        menu.addItem(titleItem)
        menu.addItem(detailItem)
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
    }

    @objc private func refresh() {
        coordinator.refresh()
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
        let application = NSApplication.shared
        let delegate = StatusMenuAppDelegate(configuration: configuration)
        application.setActivationPolicy(.accessory)
        application.delegate = delegate
        application.run()
    }
} catch {
    // One fixed usage line. Even the companion's own argument errors echo the
    // words they were given, and this process writes none of those back.
    FileHandle.standardError.write(
        Data("usage: omnivia-core-status-menu [--cli <absolute path>] --installation-state <absolute path> --workspace-id <id>\n".utf8)
    )
    Darwin.exit(2)
}
