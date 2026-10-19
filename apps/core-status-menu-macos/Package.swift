// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "OmniViaCoreStatusMenu",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .library(
            name: "DocumentIntakeMacOS",
            targets: ["DocumentIntakeMacOS"]
        ),
        .library(
            name: "CoreCaptureMacOS",
            targets: ["CoreCaptureMacOS"]
        ),
        .executable(
            name: "omnivia-core-status-menu",
            targets: ["OmniViaCoreStatusMenu"]
        ),
        // Development-only launcher: opens the Core Settings window directly
        // so the screen can be exercised without the full companion. It hosts
        // the same window the companion opens — there is no second rendering
        // path to drift from it.
        .executable(
            name: "omnivia-core-settings",
            targets: ["CoreSettingsPreview"]
        ),
        .executable(
            name: "omnivia-core-capture-harness",
            targets: ["CoreCaptureHarness"]
        ),
    ],
    targets: [
        .target(name: "DocumentIntakeMacOS"),
        .target(
            name: "CoreCaptureMacOS",
            dependencies: ["DocumentIntakeMacOS"]
        ),
        // The settings screen and readiness model are a library so both the
        // companion executable and the development launcher can host them.
        .target(
            name: "CoreSettingsMacOS",
            resources: [
                .copy("Resources/brand"),
            ]
        ),
        .executableTarget(
            name: "OmniViaCoreStatusMenu",
            dependencies: ["CoreSettingsMacOS", "CoreCaptureMacOS", "DocumentIntakeMacOS"]
        ),
        .executableTarget(
            name: "CoreSettingsPreview",
            dependencies: ["CoreSettingsMacOS"]
        ),
        .executableTarget(
            name: "CoreCaptureHarness",
            dependencies: ["CoreCaptureMacOS", "DocumentIntakeMacOS"]
        ),
        .testTarget(
            name: "OmniViaCoreStatusMenuTests",
            dependencies: ["OmniViaCoreStatusMenu", "CoreSettingsMacOS", "CoreCaptureMacOS", "DocumentIntakeMacOS"]
        ),
    ]
)
