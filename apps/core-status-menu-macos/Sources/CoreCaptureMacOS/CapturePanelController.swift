import AppKit
import DocumentIntakeMacOS

@MainActor
public final class CapturePanelController: NSObject, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private var window: NSWindow?
    private var draft = CaptureDraft()
    private let workspaceName: String
    private let submitter: ManualCaptureSubmission
    private let table = NSTableView()
    private let primaryButton = NSButton(title: "Add", target: nil, action: nil)
    private let statusLabel = NSTextField(labelWithString: "")
    private let note = NSTextView()

    public init(workspaceName: String, submitter: ManualCaptureSubmission) {
        self.workspaceName = workspaceName
        self.submitter = submitter
        super.init()
    }

    public func openOrFocus() {
        if let window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let built = buildWindow()
        window = built
        built.delegate = self
        built.center()
        built.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    public func addIncoming(_ urls: [URL]) {
        openOrFocus()
        add(urls)
    }

    public func windowWillClose(_ notification: Notification) { window = nil }

    private func buildWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 540),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Add to Core"
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(srgbRed: 0.12, green: 0.12, blue: 0.13, alpha: 1)
        window.minSize = NSSize(width: 380, height: 460)

        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.edgeInsets = NSEdgeInsets(top: 22, left: 20, bottom: 18, right: 20)
        root.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Add to Core")
        title.font = .systemFont(ofSize: 22, weight: .bold)
        root.addArrangedSubview(title)
        let subtitle = NSTextField(wrappingLabelWithString: "Paste a note or choose files. Original files stay in their current folders.")
        subtitle.textColor = .secondaryLabelColor
        root.addArrangedSubview(subtitle)

        let noteScroll = NSScrollView()
        noteScroll.hasVerticalScroller = true
        noteScroll.borderType = .bezelBorder
        noteScroll.documentView = note
        note.font = .systemFont(ofSize: 14)
        note.string = ""
        note.isRichText = false
        root.addArrangedSubview(noteScroll)
        noteScroll.heightAnchor.constraint(equalToConstant: 92).isActive = true
        noteScroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let chooseFiles = NSButton(title: "Choose files…", target: self, action: #selector(chooseFilesAction))
        let chooseFolder = NSButton(title: "Choose folder…", target: self, action: #selector(chooseFolderAction))
        let chooser = NSStackView(views: [chooseFiles, chooseFolder])
        chooser.spacing = 8
        root.addArrangedSubview(chooser)

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("selection"))
        column.title = "Selection"
        table.addTableColumn(column)
        table.headerView = nil
        table.rowHeight = 34
        table.dataSource = self
        table.delegate = self
        let listScroll = NSScrollView()
        listScroll.documentView = table
        listScroll.hasVerticalScroller = true
        listScroll.borderType = .bezelBorder
        root.addArrangedSubview(listScroll)
        listScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
        listScroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let destination = NSTextField(labelWithString: "Destination: \(workspaceName)")
        destination.font = .systemFont(ofSize: 12, weight: .medium)
        root.addArrangedSubview(destination)

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.maximumNumberOfLines = 2
        root.addArrangedSubview(statusLabel)

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(closeAction))
        primaryButton.target = self
        primaryButton.action = #selector(submitAction)
        primaryButton.keyEquivalent = "\r"
        primaryButton.bezelStyle = .rounded
        primaryButton.contentTintColor = NSColor(srgbRed: 0.82, green: 0.46, blue: 0.42, alpha: 1)
        let actions = NSStackView(views: [spacer, cancel, primaryButton])
        actions.spacing = 8
        root.addArrangedSubview(actions)
        actions.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        let host = NSView()
        host.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            root.topAnchor.constraint(equalTo: host.topAnchor),
            root.bottomAnchor.constraint(equalTo: host.bottomAnchor),
        ])
        window.contentView = host
        refresh()
        return window
    }

    @objc private func chooseFilesAction() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.treatsFilePackagesAsDirectories = false
        panel.resolvesAliases = false
        if panel.runModal() == .OK { add(panel.urls) }
    }

    @objc private func chooseFolderAction() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.treatsFilePackagesAsDirectories = false
        panel.resolvesAliases = false
        if panel.runModal() == .OK { add(panel.urls) }
    }

    private func add(_ urls: [URL]) {
        do {
            try draft.add(urls)
            statusLabel.stringValue = draft.containsFolders
                ? "Folders require scope review before files can be added."
                : "Ready to add."
        } catch CaptureDraftError.tooManyItems {
            statusLabel.stringValue = "Choose no more than 10 files at a time."
        } catch CaptureDraftError.duplicateItem {
            statusLabel.stringValue = "That item is already in this selection."
        } catch {
            statusLabel.stringValue = "That item is not a supported document."
        }
        refresh()
    }

    @objc private func submitAction() {
        guard draft.canSubmitFiles else { return }
        draft.note = note.string
        primaryButton.isEnabled = false
        statusLabel.stringValue = "Preparing current file versions…"
        let candidates = draft.fileCandidates
        Task {
            do {
                let entries = try await submitter.submit(candidates)
                statusLabel.stringValue = entries.count == 1
                    ? "Queued on this Mac."
                    : "\(entries.count) items queued on this Mac."
                draft = CaptureDraft()
                note.string = ""
                refresh()
            } catch {
                statusLabel.stringValue = "Couldn’t queue this selection. Check access and try again."
                primaryButton.isEnabled = true
            }
        }
    }

    @objc private func closeAction() { window?.close() }

    private func refresh() {
        table.reloadData()
        primaryButton.isEnabled = draft.canSubmitFiles
        if draft.items.isEmpty && statusLabel.stringValue.isEmpty {
            statusLabel.stringValue = "Choose files to begin."
        }
    }

    public func numberOfRows(in tableView: NSTableView) -> Int { draft.items.count }

    public func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let item = draft.items[row]
        let cell = NSTableCellView()
        let icon = NSImageView(image: NSWorkspace.shared.icon(forFile: item.url.path))
        icon.translatesAutoresizingMaskIntoConstraints = false
        let label = NSTextField(labelWithString: item.displayName)
        label.translatesAutoresizingMaskIntoConstraints = false
        label.lineBreakMode = .byTruncatingMiddle
        label.toolTip = item.displayName
        cell.addSubview(icon)
        cell.addSubview(label)
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
            icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 22),
            icon.heightAnchor.constraint(equalToConstant: 22),
            label.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 8),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        cell.imageView = icon
        cell.textField = label
        return cell
    }
}
