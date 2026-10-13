import AppKit
import CoreCaptureMacOS
import CoreSettingsMacOS
import DocumentIntakeMacOS
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
    private let addItem = NSMenuItem(title: "Add to Core…", action: #selector(openCapture), keyEquivalent: "a")
    private let filesItem = NSMenuItem(title: "Files & Folders…", action: #selector(openFilesAndFolders), keyEquivalent: "")
    private let settingsItem = NSMenuItem(title: "Core Settings…", action: #selector(openSettings), keyEquivalent: ",")
    private var pollTimer: Timer?
    private var monitoringTimer: Timer?
    private var folderEventObserver: FolderEventObserver?
    private var folderEventObserverFingerprint = ""
    private var folderEventDebounceTimer: Timer?
    private var firstFolderEventAt: Date?
    private var dirtyFolderSubscriptions = Set<String>()
    /// One owned Settings window (MR-001). Created on first open; closing it
    /// never touches Core lifecycle.
    private var settingsWindowController: SettingsWindowController?
    private var capturePanelController: CapturePanelController?
    private var folderManagementWindowController: FolderManagementWindowController?
    private var captureDeliveryCoordinator: CaptureDeliveryCoordinator?
    private var folderMonitoringCoordinator: FolderMonitoringCoordinator?

    init(configuration: CompanionConfiguration, singleton: CompanionSingleton) {
        self.configuration = configuration
        self.singleton = singleton
        let runner: LifecycleRunning
        let resolvedExecutable: URL?
        do {
            let executable = try CLIResolver().resolve(override: configuration.cliOverride)
            resolvedExecutable = executable
            runner = CLICommandRunner(
                executable: executable,
                installationState: configuration.installationState,
                workspaceID: configuration.workspaceID
            )
        } catch {
            resolvedExecutable = nil
            runner = UnavailableLifecycleRunner()
        }
        coordinator = LifecycleCoordinator(runner: runner)
        super.init()
        let intakeRoot = configuration.installationState.deletingLastPathComponent()
            .appendingPathComponent("native-intake", isDirectory: true)
        if let environment = try? DocumentIntakeEnvironment(stateRoot: intakeRoot) {
            folderMonitoringCoordinator = environment.folderMonitoring
            folderManagementWindowController = FolderManagementWindowController(
                store: environment.folderSubscriptions,
                monitoring: environment.folderMonitoring
            )
            let submitter = ManualCaptureSubmission(
                environment: environment,
                workspaceRef: configuration.workspaceID
            )
            let folderSubmitter = FolderCaptureSubmission(
                environment: environment,
                workspaceRef: configuration.workspaceID
            )
            if let resolvedExecutable {
                captureDeliveryCoordinator = CaptureDeliveryCoordinator(
                    queue: environment.queue,
                    transport: CLICaptureDelivery(
                        executable: resolvedExecutable,
                        installationState: configuration.installationState,
                        workspaceID: configuration.workspaceID
                    )
                )
            }
            capturePanelController = CapturePanelController(
                workspaceName: configuration.workspaceID,
                submitter: submitter,
                folderSubmitter: folderSubmitter,
                onQueued: { [weak self] in self?.deliverPendingCaptures() }
            )
        }
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
        deliverPendingCaptures()
        reconcileWatchedFolders()

        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.coordinator.refresh()
                self?.deliverPendingCaptures()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
        let monitoringTimer = Timer(timeInterval: 15 * 60, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.reconcileWatchedFolders() }
        }
        RunLoop.main.add(monitoringTimer, forMode: .common)
        self.monitoringTimer = monitoringTimer
    }

    func applicationWillTerminate(_ notification: Notification) {
        // The status menu is a companion, not the service owner. Quitting it
        // deliberately performs no Core lifecycle command.
        pollTimer?.invalidate()
        monitoringTimer?.invalidate()
        folderEventDebounceTimer?.invalidate()
        folderEventObserver = nil
        coordinator.companionWillTerminate()
        singleton.close()
    }

    private func configureMenu() {
        menu.autoenablesItems = false
        titleItem.isEnabled = false
        detailItem.isEnabled = false
        for item in [refreshItem, startItem, stopItem, logItem, addItem, filesItem, settingsItem] {
            item.target = self
        }

        menu.addItem(titleItem)
        menu.addItem(detailItem)
        menu.addItem(.separator())
        menu.addItem(addItem)
        menu.addItem(filesItem)
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
        addItem.isEnabled = capturePanelController != nil
        filesItem.isEnabled = folderManagementWindowController != nil
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

    @objc private func openSettings() {
        if settingsWindowController == nil {
            let context = ReadinessContext(
                installationRevision: configuration.installationState.path,
                localSessionRef: UUID().uuidString,
                targetRef: "local",
                workspaceRef: configuration.workspaceID,
                configurationRevision: "1"
            )
            settingsWindowController = SettingsWindowController(coordinator: ReadinessCoordinator(context: context))
        }
        settingsWindowController?.openOrFocus()
    }

    @objc private func openCapture() {
        capturePanelController?.openOrFocus()
    }

    @objc private func openFilesAndFolders() {
        folderManagementWindowController?.openOrFocus()
    }

    private func deliverPendingCaptures() {
        guard let captureDeliveryCoordinator else { return }
        let monitoring = folderMonitoringCoordinator
        Task {
            _ = await captureDeliveryCoordinator.deliverPending()
            _ = await monitoring?.reconcileReceipts()
            await self.refreshFolderEventObservation()
        }
    }

    private func reconcileWatchedFolders() {
        guard let folderMonitoringCoordinator else { return }
        let delivery = captureDeliveryCoordinator
        Task {
            _ = await folderMonitoringCoordinator.reconcileAll()
            _ = await delivery?.deliverPending()
            _ = await folderMonitoringCoordinator.reconcileReceipts()
            await self.refreshFolderEventObservation()
        }
    }

    private func refreshFolderEventObservation() async {
        guard let folderMonitoringCoordinator else { return }
        let roots = await folderMonitoringCoordinator.activeObservationRoots()
        let fingerprint = roots
            .map { "\($0.subscriptionID)\u{0}\($0.url.standardizedFileURL.path)" }
            .sorted()
            .joined(separator: "\u{1}")
        guard fingerprint != folderEventObserverFingerprint else { return }
        folderEventObserver = nil
        folderEventObserverFingerprint = fingerprint
        folderEventObserver = FolderEventObserver(roots: roots) { [weak self] signal in
            Task { @MainActor [weak self] in self?.receiveFolderEvent(signal) }
        }
    }

    private func receiveFolderEvent(_ signal: FolderEventSignal) {
        guard !signal.subscriptionIDs.isEmpty,
              let folderMonitoringCoordinator
        else { return }
        dirtyFolderSubscriptions.formUnion(signal.subscriptionIDs)
        if firstFolderEventAt == nil { firstFolderEventAt = Date() }
        Task { await folderMonitoringCoordinator.markDirty(subscriptionIDs: signal.subscriptionIDs) }
        folderEventDebounceTimer?.invalidate()
        let elapsed = Date().timeIntervalSince(firstFolderEventAt ?? Date())
        let interval = signal.requiresFullReconciliation ? 0.1 : min(3, max(0.1, 30 - elapsed))
        let timer = Timer(timeInterval: interval, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in self?.reconcileDirtyFolders() }
        }
        RunLoop.main.add(timer, forMode: .common)
        folderEventDebounceTimer = timer
    }

    private func reconcileDirtyFolders() {
        guard let folderMonitoringCoordinator else { return }
        let subscriptions = dirtyFolderSubscriptions
        dirtyFolderSubscriptions.removeAll()
        firstFolderEventAt = nil
        folderEventDebounceTimer = nil
        let delivery = captureDeliveryCoordinator
        Task {
            _ = await folderMonitoringCoordinator.reconcile(subscriptionIDs: subscriptions)
            _ = await delivery?.deliverPending()
            _ = await folderMonitoringCoordinator.reconcileReceipts()
            await self.refreshFolderEventObservation()
        }
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
