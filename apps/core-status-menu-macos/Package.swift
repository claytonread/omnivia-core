// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "OmniViaCoreStatusMenu",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(
            name: "omnivia-core-status-menu",
            targets: ["OmniViaCoreStatusMenu"]
        ),
        // Developer-only harness: opens the Core Settings window directly so
        // the screen can be exercised without the full companion.
        .executable(
            name: "omnivia-core-settings-harness",
            targets: ["CoreSettingsHarness"]
        ),
    ],
    targets: [
        // The settings screen and readiness model are a library so both the
        // companion executable and the developer harness can host them.
        .target(
            name: "CoreSettingsMacOS",
            resources: [
                .copy("Resources/prototype")
            ]
        ),
        .executableTarget(
            name: "OmniViaCoreStatusMenu",
            dependencies: ["CoreSettingsMacOS"]
        ),
        .executableTarget(
            name: "CoreSettingsHarness",
            dependencies: ["CoreSettingsMacOS"]
        ),
        .testTarget(
            name: "OmniViaCoreStatusMenuTests",
            dependencies: ["OmniViaCoreStatusMenu", "CoreSettingsMacOS"]
        ),
    ]
)
