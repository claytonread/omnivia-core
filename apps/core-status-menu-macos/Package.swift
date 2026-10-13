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
        // Development-only launcher: opens the Core Settings window directly
        // so the screen can be exercised without the full companion. It hosts
        // the same window the companion opens — there is no second rendering
        // path to drift from it.
        .executable(
            name: "omnivia-core-settings",
            targets: ["CoreSettingsPreview"]
        ),
    ],
    targets: [
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
            dependencies: ["CoreSettingsMacOS"]
        ),
        .executableTarget(
            name: "CoreSettingsPreview",
            dependencies: ["CoreSettingsMacOS"]
        ),
        .testTarget(
            name: "OmniViaCoreStatusMenuTests",
            dependencies: ["OmniViaCoreStatusMenu", "CoreSettingsMacOS"]
        ),
    ]
)
