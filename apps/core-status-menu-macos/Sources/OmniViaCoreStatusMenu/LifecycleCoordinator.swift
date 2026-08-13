import Foundation

final class LifecycleCoordinator: @unchecked Sendable {
    typealias RefreshWork = @Sendable () -> Void
    typealias RefreshScheduler = (@escaping RefreshWork) -> Void

    private let runner: LifecycleRunning
    private let schedulePostMutationRefresh: RefreshScheduler
    private var generation = 0
    private(set) var inFlight = false
    private(set) var snapshot: MenuSnapshot = .checking {
        didSet { onChange?(snapshot) }
    }

    var onChange: ((MenuSnapshot) -> Void)?

    init(
        runner: LifecycleRunning,
        schedulePostMutationRefresh: @escaping RefreshScheduler = { work in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: work)
        }
    ) {
        self.runner = runner
        self.schedulePostMutationRefresh = schedulePostMutationRefresh
    }

    func refresh() {
        issue(.status, operation: .refreshing)
    }

    func start() {
        issue(.start, operation: .starting)
    }

    func stop() {
        issue(.stop, operation: .stopping)
    }

    /// The companion is not the service owner: quitting it deliberately issues
    /// no lifecycle command, so Core outlives the menu that watches it.
    func companionWillTerminate() {}

    private func issue(_ action: LifecycleAction, operation: MenuOperation) {
        guard !inFlight else { return }
        inFlight = true
        generation += 1
        let requestGeneration = generation
        snapshot = MenuSnapshot(condition: snapshot.condition, operation: operation)

        runner.run(action) { [weak self] result in
            guard let self, requestGeneration == self.generation else { return }
            self.inFlight = false
            switch result {
            case let .success(document):
                self.snapshot = MenuSnapshot(document: document)
            case let .failure(error):
                self.snapshot = MenuSnapshot(condition: .unavailable(error))
            }

            if action != .status {
                self.schedulePostMutationRefresh { [weak self] in
                    self?.refresh()
                }
            }
        }
    }
}
