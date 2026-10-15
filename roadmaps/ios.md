# iOS roadmap

Each `##` is a series and each numbered line is one card. Cards from this file use topic `swe`. The reader is a working iOS engineer, so go past the basics: explain the mechanism, and show real Swift code, LLDB or Instruments output.

## swift-concurrency: Swift concurrency for real
1. async/await: what a suspension point really is
2. Tasks, structured concurrency and cancellation
3. Actors and actor reentrancy
4. @MainActor and global actors
5. Sendable and Swift 6 strict concurrency checking
6. async let and task groups
7. AsyncSequence and AsyncStream
8. Bridging callback APIs with continuations

## swiftui-internals: How SwiftUI actually updates
1. Views are values: body, identity and lifetime
2. Structural vs. explicit identity, and why id() resets state
3. @State, @Binding and where state really lives
4. The Observation framework (@Observable) vs. ObservableObject
5. Why a view's body runs again: dependency tracking
6. Layout: proposed sizes and the Layout protocol
7. Environment values and preferences
8. Keeping body cheap: performance pitfalls

## ios-memory: ARC and memory on iOS
1. ARC: retain counts and when an object is freed
2. Retain cycles in closures: weak vs. unowned
3. Value types and copy-on-write
4. Finding leaks with the Memory Graph Debugger
5. Memory warnings, jetsam and why iOS kills apps

## ios-lifecycle: The app and the system
1. App launch: from tap to first frame
2. Scenes and app lifecycle states
3. The main run loop, and why blocking it freezes the UI
4. Background work with BGTaskScheduler, and its limits
5. Push notifications: APNs end to end
6. Universal links and deep linking

## ios-networking: Networking on iOS
1. URLSession configurations and task types
2. App Transport Security and what it enforces
3. Certificate pinning: benefits and risks
4. HTTP caching with URLCache
5. Network.framework and NWPathMonitor

## ios-security: Securing iOS apps
1. The app sandbox and data protection classes
2. Keychain: storing secrets correctly
3. Code signing, provisioning profiles and entitlements
4. Why jailbreak detection is not a security boundary
5. Passkeys and Sign in with Apple on iOS

## ios-tooling: Debugging and performance
1. LLDB beyond po: breakpoint actions and expressions
2. Instruments: reading a Time Profiler trace
3. Hangs and hitches: what they are and how to find them
4. Why Swift type checking gets slow, and how to fix it
5. Swift Package Manager: packages, targets and version resolution
