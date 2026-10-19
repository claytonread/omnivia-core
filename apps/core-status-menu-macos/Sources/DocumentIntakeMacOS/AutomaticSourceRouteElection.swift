import Foundation

/// The route currently responsible for automatically reading one verified
/// source. Route identity is opaque and stable; paths and bookmark material are
/// never part of the election record.
enum AutomaticSourceRoute: Equatable, Hashable, Sendable {
    case folder(subscriptionID: String, memberID: String)
    case direct(subscriptionID: String)
}

/// Selects one eligible automatic reader for every canonical source.
///
/// The election is derived from durable route/subscription state each time it
/// is asked, under the shared publication gate. No separate winner document can
/// drift from the routes it describes. Containing roots rank before a direct
/// fallback; the shortest member-relative locator is the most specific root,
/// then durable subscription/member identifiers provide stable order.
final class AutomaticSourceRouteElection: @unchecked Sendable {
    private struct Candidate: Comparable {
        let route: AutomaticSourceRoute
        let rootDistance: Int?
        let subscriptionID: String
        let memberID: String

        static func < (lhs: Candidate, rhs: Candidate) -> Bool {
            let leftRank = lhs.rootDistance == nil ? 1 : 0
            let rightRank = rhs.rootDistance == nil ? 1 : 0
            return (
                leftRank,
                lhs.rootDistance ?? .max,
                lhs.subscriptionID,
                lhs.memberID
            ) < (
                rightRank,
                rhs.rootDistance ?? .max,
                rhs.subscriptionID,
                rhs.memberID
            )
        }
    }

    private let accessStore: NativeAccessStore
    private let folders: FolderSubscriptionStore
    private let files: FileSubscriptionStore
    private let canonicalSources: CanonicalSourceRegistry
    private let gate: PublicationGate

    init(
        accessStore: NativeAccessStore,
        folders: FolderSubscriptionStore,
        files: FileSubscriptionStore,
        canonicalSources: CanonicalSourceRegistry,
        gate: PublicationGate
    ) {
        self.accessStore = accessStore
        self.folders = folders
        self.files = files
        self.canonicalSources = canonicalSources
        self.gate = gate
    }

    /// Tests and already-persisted routes use this form. A non-canonical source
    /// cannot overlap by proof, so its own route remains its scheduler.
    func isElected(
        _ route: AutomaticSourceRoute,
        workspaceRef: String,
        sourceIdentity: String
    ) -> Bool {
        gate.withLock {
            guard canonicalSources.record(sourceIdentity: sourceIdentity)?.workspaceRef
                    == workspaceRef
            else { return true }
            let candidates = eligibleCandidates(
                workspaceRef: workspaceRef,
                sourceIdentity: sourceIdentity
            )
            return candidates.min()?.route == route
        }
    }

    /// A newly discovered member has a durable access route but is not in the
    /// folder subscription yet. Include it for this election only; publication
    /// rechecks the same provisional candidate under the gate.
    func isElectedProvisionalFolder(
        subscription: FolderSubscription,
        member: FolderSubscriptionMember
    ) -> Bool {
        gate.withLock {
            guard canonicalSources.record(sourceIdentity: member.requestedSourceIdentity)?
                    .workspaceRef == subscription.workspaceRef
            else { return true }
            var candidates = eligibleCandidates(
                workspaceRef: subscription.workspaceRef,
                sourceIdentity: member.requestedSourceIdentity
            )
            let route = AutomaticSourceRoute.folder(
                subscriptionID: subscription.subscriptionID,
                memberID: member.memberID
            )
            if provisionalFolderIsEligible(subscription: subscription, member: member),
               !candidates.contains(where: { $0.route == route })
            {
                candidates.append(Candidate(
                    route: route,
                    rootDistance: member.relativeComponents.count,
                    subscriptionID: subscription.subscriptionID,
                    memberID: member.memberID
                ))
            }
            return candidates.min()?.route == route
        }
    }

    private func eligibleCandidates(
        workspaceRef: String,
        sourceIdentity: String
    ) -> [Candidate] {
        let descriptors = Dictionary(
            uniqueKeysWithValues: accessStore.descriptors().map { ($0.accessRef, $0) }
        )
        let roots = Dictionary(
            uniqueKeysWithValues: accessStore.rootDescriptors().map { ($0.rootRef, $0) }
        )
        var result: [Candidate] = []

        for subscription in folders.all()
            where subscription.workspaceRef == workspaceRef && subscription.state == .active
        {
            guard roots[subscription.rootRef]?.state == .active else { continue }
            for member in subscription.members
                where member.requestedSourceIdentity == sourceIdentity && member.state == .present
            {
                guard let descriptor = descriptors[member.accessRef],
                      descriptor.state == .connected,
                      descriptor.generation == member.membershipGeneration
                else { continue }
                result.append(Candidate(
                    route: .folder(
                        subscriptionID: subscription.subscriptionID,
                        memberID: member.memberID
                    ),
                    rootDistance: member.relativeComponents.count,
                    subscriptionID: subscription.subscriptionID,
                    memberID: member.memberID
                ))
            }
        }

        for subscription in files.all()
            where subscription.workspaceRef == workspaceRef
                && subscription.requestedSourceIdentity == sourceIdentity
                && subscription.state == .active
        {
            guard let descriptor = descriptors[subscription.accessRef],
                  descriptor.state == .connected,
                  descriptor.lifecycle == .active,
                  descriptor.generation == subscription.bindingGeneration
            else { continue }
            result.append(Candidate(
                route: .direct(subscriptionID: subscription.subscriptionID),
                rootDistance: nil,
                subscriptionID: subscription.subscriptionID,
                memberID: ""
            ))
        }
        return result
    }

    private func provisionalFolderIsEligible(
        subscription: FolderSubscription,
        member: FolderSubscriptionMember
    ) -> Bool {
        guard folders.subscription(id: subscription.subscriptionID)?.state == .active,
              accessStore.rootDescriptors().first(where: {
                  $0.rootRef == subscription.rootRef
              })?.state == .active,
              let descriptor = accessStore.descriptors().first(where: {
                  $0.accessRef == member.accessRef
              }),
              descriptor.state == .connected,
              descriptor.generation == member.membershipGeneration
        else { return false }
        return true
    }
}
