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
    ],
    targets: [
        .executableTarget(
            name: "OmniViaCoreStatusMenu"
        ),
        .testTarget(
            name: "OmniViaCoreStatusMenuTests",
            dependencies: ["OmniViaCoreStatusMenu"]
        ),
    ]
)
