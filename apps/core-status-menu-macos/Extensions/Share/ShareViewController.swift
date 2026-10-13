import AppKit
import Foundation

@objc(ShareViewController)
final class ShareViewController: NSViewController {
    private let status = NSTextField(labelWithString: "Preparing captured content…")
    private var started = false

    override func loadView() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .centerX
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        status.alignment = .center
        status.maximumNumberOfLines = 3
        root.addArrangedSubview(status)
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancelAction))
        root.addArrangedSubview(cancel)
        view = root
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        guard !started else { return }
        started = true
        Task { @MainActor in await beginHandoff() }
    }

    @objc private func cancelAction() {
        extensionContext?.cancelRequest(withError: OmniViaExtensionError.invalidInput)
    }

    @MainActor
    private func beginHandoff() async {
        var nonce: String?
        do {
            guard let context = extensionContext else { throw OmniViaExtensionError.invalidInput }
            let providers = context.inputItems
                .compactMap { $0 as? NSExtensionItem }
                .flatMap { $0.attachments ?? [] }
            guard !providers.isEmpty,
                  providers.count <= NativeHandoffMailbox.maximumSlots
            else { throw OmniViaExtensionError.invalidInput }

            let mailbox = try NativeHandoffMailbox.appGroupMailbox()
            let requestNonce = UUID().uuidString.lowercased()
            nonce = requestNonce
            let directory = try mailbox.prepareSnapshotDirectory(nonce: requestNonce)
            var slots: [NativeHandoffSlotV1] = []
            var aggregate: UInt64 = 0
            for (index, provider) in providers.enumerated() {
                guard let typeIdentifier = provider.registeredTypeIdentifiers.first else {
                    throw OmniViaExtensionError.invalidInput
                }
                let proposed = provider.suggestedName ?? "Shared item"
                let filename = try safeProviderFilename(proposed, index: index)
                let destination = directory.appendingPathComponent(filename, isDirectory: false)
                let bytes = try await copyProviderFile(
                    from: provider,
                    typeIdentifier: typeIdentifier,
                    destination: destination,
                    maximumBytes: OwnedSnapshotLimits.maximumItemBytes
                )
                aggregate += bytes
                guard aggregate <= OwnedSnapshotLimits.maximumSelectionBytes else {
                    throw OmniViaExtensionError.selectionTooLarge
                }
                slots.append(NativeHandoffSlotV1(
                    slotID: UUID().uuidString.lowercased(),
                    displayName: proposed,
                    sourceKind: .capturedSnapshot,
                    snapshotRelativePath: filename
                ))
            }
            let now = Date()
            try mailbox.writeOffer(NativeHandoffOfferV1(
                requestNonce: requestNonce,
                expectedOwnerBundleID: NativeHandoffMailbox.companionBundleID,
                senderBundleID: NativeHandoffMailbox.shareBundleID,
                surface: .share,
                createdAt: now,
                expiresAt: now.addingTimeInterval(NativeHandoffMailbox.maximumLifetime),
                slots: slots
            ), now: now)
            try activateContainingApp()
            status.stringValue = "Waiting for OmniVia Core…"
            let acknowledgement = try await waitForAcknowledgement(
                mailbox: mailbox,
                nonce: requestNonce
            )
            guard acknowledgement.adoptions.map(\.slotID) == slots.map(\.slotID) else {
                throw OmniViaExtensionError.invalidInput
            }
            mailbox.removeAcknowledgement(nonce: requestNonce)
            context.completeRequest(returningItems: [], completionHandler: nil)
        } catch {
            if let nonce,
               let mailbox = try? NativeHandoffMailbox.appGroupMailbox()
            {
                mailbox.removeOffer(nonce: nonce, removeSnapshots: true)
            }
            status.stringValue = "Couldn’t prepare this shared content. Retry or cancel."
        }
    }
}

private enum OwnedSnapshotLimits {
    static let maximumItemBytes: UInt64 = 50 * 1_024 * 1_024
    static let maximumSelectionBytes: UInt64 = 200 * 1_024 * 1_024
}
