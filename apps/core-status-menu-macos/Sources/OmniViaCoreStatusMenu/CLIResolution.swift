import Foundation

enum CompanionConfigurationError: LocalizedError, Equatable {
    case missingValue(String)
    case missingRequired(String)
    case unknownArgument(String)
    case pathMustBeAbsolute(String)

    var errorDescription: String? {
        switch self {
        case let .missingValue(option):
            return "\(option) requires an absolute path"
        case let .missingRequired(option):
            return "\(option) is required"
        case let .unknownArgument(argument):
            return "unknown argument: \(argument)"
        case let .pathMustBeAbsolute(option):
            return "\(option) must be an absolute path"
        }
    }
}

struct CompanionConfiguration: Equatable {
    let cliOverride: URL?
    let installationState: URL
    let workspaceID: String

    static func parse(arguments: [String]) throws -> CompanionConfiguration {
        var cli: URL?
        var installationState: URL?
        var workspaceID: String?
        var index = arguments.isEmpty ? 0 : 1

        func absoluteURL(option: String, value: String) throws -> URL {
            guard value.hasPrefix("/") else {
                throw CompanionConfigurationError.pathMustBeAbsolute(option)
            }
            return URL(fileURLWithPath: value).standardizedFileURL
        }

        while index < arguments.count {
            let option = arguments[index]
            guard ["--cli", "--installation-state", "--workspace-id"].contains(option) else {
                throw CompanionConfigurationError.unknownArgument(option)
            }
            let valueIndex = index + 1
            guard valueIndex < arguments.count else {
                throw CompanionConfigurationError.missingValue(option)
            }
            let value = arguments[valueIndex]
            if option == "--workspace-id" {
                workspaceID = value
            } else if option == "--cli" {
                cli = try absoluteURL(option: option, value: value)
            } else {
                installationState = try absoluteURL(option: option, value: value)
            }
            index += 2
        }
        guard let installationState else {
            throw CompanionConfigurationError.missingRequired("--installation-state")
        }
        guard let workspaceID, !workspaceID.isEmpty else {
            throw CompanionConfigurationError.missingRequired("--workspace-id")
        }
        return CompanionConfiguration(
            cliOverride: cli,
            installationState: installationState,
            workspaceID: workspaceID
        )
    }
}

enum CLIResolutionError: LocalizedError, Equatable {
    case overrideIsNotExecutable(String)
    case executableNotFound

    var errorDescription: String? {
        switch self {
        case let .overrideIsNotExecutable(path):
            return "the --cli path is not executable: \(path)"
        case .executableNotFound:
            return "could not find the fixed 'omnivia' executable"
        }
    }
}

struct CLIResolver {
    let isExecutable: (String) -> Bool

    init(isExecutable: @escaping (String) -> Bool = { path in
        FileManager.default.isExecutableFile(atPath: path)
    }) {
        self.isExecutable = isExecutable
    }

    func resolve(
        override: URL?,
        bundleURL: URL? = Bundle.main.bundleURL,
        companionExecutableURL: URL? = Bundle.main.executableURL,
        pathEnvironment: String? = ProcessInfo.processInfo.environment["PATH"],
        workingDirectory: URL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    ) throws -> URL {
        if let override {
            guard isExecutable(override.path) else {
                throw CLIResolutionError.overrideIsNotExecutable(override.path)
            }
            return override.standardizedFileURL
        }

        var candidates: [URL] = []
        if let bundleURL {
            candidates.append(
                bundleURL
                    .appendingPathComponent("Contents", isDirectory: true)
                    .appendingPathComponent("Resources", isDirectory: true)
                    .appendingPathComponent("omnivia")
            )
        }
        if let companionExecutableURL {
            candidates.append(
                companionExecutableURL.deletingLastPathComponent().appendingPathComponent("omnivia")
            )
        }
        candidates.append(URL(fileURLWithPath: "/opt/homebrew/bin/omnivia"))
        candidates.append(URL(fileURLWithPath: "/usr/local/bin/omnivia"))

        if let pathEnvironment {
            for entry in pathEnvironment.split(separator: ":", omittingEmptySubsequences: false) {
                let directory = entry.isEmpty ? workingDirectory.path : String(entry)
                let base = URL(fileURLWithPath: directory, relativeTo: workingDirectory)
                candidates.append(base.appendingPathComponent("omnivia").standardizedFileURL)
            }
        }

        var seen = Set<String>()
        for candidate in candidates {
            let path = candidate.standardizedFileURL.path
            guard seen.insert(path).inserted else { continue }
            if isExecutable(path) {
                return URL(fileURLWithPath: path)
            }
        }
        throw CLIResolutionError.executableNotFound
    }
}

struct CLIInvocation: Equatable {
    let executable: URL
    let arguments: [String]

    static func lifecycle(
        executable: URL,
        installationState: URL,
        workspaceID: String,
        action: LifecycleAction
    ) -> CLIInvocation {
        CLIInvocation(
            executable: executable,
            arguments: [
                "--installation-state", installationState.path,
                "--workspace-id", workspaceID,
                "service", action.rawValue, "--json",
            ]
        )
    }
}
