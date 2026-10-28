// swift-tools-version: 5.9
import PackageDescription
let package = Package(
    name: "SwiftDemo",
    targets: [.executableTarget(name: "SwiftDemo", resources: [.process("Resources")])]
)
