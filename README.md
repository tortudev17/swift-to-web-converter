# SwiftUI to Web

Convert a **supported subset of a local SwiftUI app** into a ZIP containing exactly one `index.html`. CSS, JavaScript, images, and the conversion report are embedded. Extract the ZIP and open the HTML in a modern browser; no server or web build step is required. A recognized Firebase Realtime Database setup can make REST calls to its literal HTTPS database URL.

Requires **Python 3.9+**. The converter uses only Python's standard library. It does not run Xcode, Swift, package manifests, build scripts, an AI service, or dependency downloads.

## Usage

Run from this repository:

```sh
python3 swift_to_web.py /path/to/MyApp --output app.zip
python3 swift_to_web.py /path/to/MyApp.xcodeproj --target MyApp --output app.zip
python3 swift_to_web.py /path/to/Package.swift --target MyApp --output app.zip
```

Select a particular SwiftUI `App` or `View` struct if necessary:

```sh
python3 swift_to_web.py /path/to/MyApp --entry ContentView --output preview.zip
```

With no `--entry`, there must be exactly one `App` struct in the selected target. With multiple source targets, supply `--target` using its exact name. A directory containing both an Xcode project and a package requires an explicit `.xcodeproj` or `Package.swift` input. `.xcworkspace` files are not accepted.

The default output is `<selected-target-name>-web.zip` in the current directory. The destination directory must exist. Existing output is preserved unless you pass `--overwrite`. Conversion errors return exit code `1`; argument errors return `2`. A successful conversion, including a conversion with warnings, returns `0`.

Warnings are printed with source file and line, and included in an expandable **Conversion report** in the generated page. Unsupported views become labeled placeholders. Unsupported modifiers are skipped while preserving their supported content. Unsupported expressions use a fallback value and emit a warning; unsupported actions are omitted and reported. Check the report and manually review the result before using it.

## Supported SwiftUI subset

| Area | Supported forms |
| --- | --- |
| Entry and composition | `struct …: App`, `struct …: View`, `var body`, `WindowGroup`, custom views with named primitive stored-property arguments and literal defaults |
| Layout | `VStack`, `HStack`, `ZStack`, `Group`, `GroupBox`, `Form`, `Section`, `LazyVGrid`, `LazyHGrid`, `Circle`, `Spacer`, `Divider`, `ScrollView`, `ScrollViewReader` content, `EmptyView`, `ContentUnavailableView`; basic stack spacing/alignment and vertical/horizontal scrolling |
| Content | `Text` with literal strings and simple `\(expression)` interpolation; `Image("localName")` and interpolated names drawn from selected-target assets; built-in embedded `Image(systemName: "gear")` and `"gearshape"` |
| Controls | `Button("Label") { … }`, `Button { … } label: { … }`, `Button(action: { … }, label: { … })`, `TextField`, `SecureField`, `Toggle`, tagged `Picker`, numeric `Stepper`, and `DatePicker` |
| State | Primitive `@State` defaults, literal arrays and local structs with literal property defaults; `$state` bindings to nested fields and dictionary entries; custom views can receive parent state through a matching `@Binding`; independent custom-view state in repeated rows |
| Pure model methods | Local model structs and literal initializers; side-effect-free methods using local variables, `for`, `if` / `else`, `break`, arithmetic assignment, `return`, array `.removeFirst()`, and `.sorted { … }` comparators |
| Pure global helpers | Non-generic helpers with typed parameters and return values using the supported expression subset; Foundation date components and `String(format:)` with integer `%d` placeholders |
| Actions | Inline `state = expression`, `state += expression`, `state -= expression`, `state.toggle()`, supported array `append`/`removeAll`, view callback closures, and translated synchronous component helpers |
| Expressions | Primitive literals, state/property/loop references, optional member access, parentheses, arrays and tuples, `+ - * / %`, comparisons, `&&`, `\|\|`, `??`, `!`, unary minus, ternaries, `.count`, `.isEmpty`, `.first`, `.last`, `.prefix(count)`, `.map`, `.compactMap`, `.joined`, and `Array.uniqued()` |
| Conditional/repeated content | `if` / `else if` / `else`, optional `if let` bindings, enum `switch`, and view-builder local `let`; `ForEach` over primitive arrays, numeric ranges, `.prefix`, and computed arrays; `List(_:rowContent:)` over a model array |
| Navigation and presentation | `NavigationStack(path:)`, simple enum route pushes, `.navigationDestination(for:)`, destination-view `NavigationLink`, `.sheet`, and `.confirmationDialog`; `toolbar(content:)` with view content and an in-page Back button |
| Modifiers | `padding`, `frame`, `fixedSize`, `foregroundColor`, named-color `foregroundStyle`, `background` including `Capsule()` and `RoundedRectangle(cornerRadius:)`, `backgroundStyle`, `toolbar`, `tint`, `font`, `fontWeight`, `bold`, `italic`, `cornerRadius`, `opacity`, `resizable`, `scaledToFit`, `scaledToFill`, `navigationTitle`, `disabled`, `multilineTextAlignment`, and basic `listStyle` |
| Firebase and storage | Literal HTTPS Firebase Realtime Database URL, recognized `readFirebase(path:as:)` calls, matching model `@State`, `.task` load, ten-second refresh, and browser localStorage state cache. A write connector is emitted only when a translated action calls the supported local `writeFirebase` helper. |

Colors support built-in names such as `.blue`, `Color.red`, `.primary`, `.secondary`, and `.clear`. A no-argument `.background()` uses a default surface color, and `.backgroundStyle(.blue)` supplies the inherited color for that surface. Fonts support standard styles such as `.title` and `.body`, `.title2.bold()`, plus `.system(size: 40)` and `.system(size: 20, weight: .bold)`. Padding supports a custom numeric length, edge-specific forms such as `.padding(.horizontal, 12)` and `.padding(.top, 5)`, and `.horizontal`, `.vertical`, `.all`. Frames support explicit/min/max width and height, `.infinity` for maximum width, and basic alignment. Modifier ordering is retained through HTML wrappers where applicable. Firebase `.task` loading is recognized only for a literal HTTPS RTDB URL plus a matching typed state model and `readFirebase` call; other async work is reported and skipped. Reads refresh every ten seconds and fetched state is cached in browser localStorage under a source-version key. Translated `writeFirebase` button actions issue a REST `PUT`; the converter never writes during conversion or testing. Use `?firebase=off` to disable network calls or `?cache=off` to disable local storage. Firebase REST reads require the target database's read rules; translated writes require write rules. Cross-origin browser access must be allowed by the database; no remote-script fallback is used.

Example:

```swift
import SwiftUI

@main
struct DemoApp: App {
    var body: some Scene {
        WindowGroup { CounterView() }
    }
}

struct CounterView: View {
    @State private var count = 0
    @State private var name = "World"
    var body: some View {
        VStack(spacing: 12) {
            Text("Hello, \(name)!").font(.title)
            TextField("Name", text: $name)
            Text("Count: \(count)")
            Button("Add") { count += 1 }
        }.padding()
    }
}
```

## Project and resource discovery

- **SwiftPM:** reads literal `.target` / `.executableTarget` declarations and their `name`, optional `path`, `sources`, and `exclude`. Defaults to `Sources/<target>`. Test targets are not selected. Resource images are discovered under the selected target directory, honoring `exclude`. Manifest expressions and external target dependencies are not evaluated or resolved.
- **Xcode:** reads `project.pbxproj`, selects a `PBXNativeTarget`, follows its source/resource build phases and local file groups, and supports basic Xcode 16 folder-synchronized groups with target membership exceptions. Build settings, configuration-specific membership, schemes, and target dependencies are not evaluated. Files outside the project directory or paths using build-setting substitutions are omitted/rejected with diagnostics.
- **Images:** local PNG, JPEG, GIF, and SVG resources in the selected target are discovered and embedded as data URLs when referenced by `Image("name")`; basic `.imageset/Contents.json` catalogs select a universal/default image when available. Multiple device/appearance/scale variants generate a warning and are flattened to one image. Duplicate image names are reported as ambiguous. The `gear` and `gearshape` system names use embedded SVG approximations; other system symbols, PDF/HEIC assets, colorsets, dynamic image names, and SVGs that reference external files are unsupported.
- Input source files are only read. Output is atomically published as a deterministic ZIP with a fixed archive timestamp. No external styles, fonts, or libraries are required. If Firebase is configured, only its exact HTTPS origin is enabled for network requests by the generated page's content security policy.

## Limits

This is a bounded translator, **not a general Swift compiler**. A whole project is inspected to find the selected target's declarations, but only reachable supported views are converted. A warning-free report means no unsupported feature was detected by this parser, not proof of full behavioral equivalence.

UIKit/AppKit, arbitrary Swift methods, custom initializers, arbitrary generic functions, general-purpose networking, persistence beyond localStorage and recognized Firebase REST calls, authentication flows, transactions, external packages, platform services, full browser history integration, SwiftUI animation modifiers, gestures, and arbitrary modifier closures are not translated. Async task bodies and Firebase transaction workflows that depend on ETags, `If-Match`, multi-path writes, or rollback are reported and skipped; the converted LaLigaBrains app currently reads its `main` record but does not enable database writes. `#Preview` blocks are skipped, and `#Playground` code is never executed. The browser adds small built-in animations for state text updates, conditional content, and navigation push/back; these do not require `.animation` or `withAnimation` in Swift. Animations honor the browser's reduced-motion preference. Other conditional compilation and macros are reported and not evaluated; select source targets that do not require them for correctness. Duplicate view names, including conditional variants, require a separate target/input.

Rendering uses approximate web layout and a Helvetica Neue font stack, not pixel-identical Apple layout. Expressions use JavaScript numeric semantics (including division); Swift integer overflow and full type checking are not reproduced. State is saved in browser localStorage when available. Navigation has an in-page stack, with preserved state on Back; browser history, deep links, native transitions, and native lifecycle behavior are not reproduced. Repeated rows use fixed index identity. Native localization and asset appearance switching are not reproduced.

## Examples and validation

```sh
python3 swift_to_web.py tests/fixtures/SwiftDemo --output demo.zip
python3 swift_to_web.py tests/fixtures/XcodeDemo --output xcode-demo.zip
python3 -m unittest discover -s tests -v
```

Both fixtures use the same SwiftUI source. The Xcode fixture is a buildable iOS reference app; the SwiftPM fixture supplies the command-line converter input.

### iOS reference comparison

`tests/fixtures/XcodeDemo/XcodeDemo/App.swift` and `tests/fixtures/SwiftDemo/Sources/SwiftDemo/App.swift` are kept identical. The Xcode target runs the native SwiftUI view in an iOS simulator; the SwiftPM target runs the converter. This sample exercises `NavigationStack`, `.navigationTitle`, `.font`, `.foregroundStyle`, `.frame`, `.padding`, state, controls, navigation, and a bundled image.

The current comparison targets a light appearance on iPhone 18 Pro Max at the default text size. It tunes HTML spacing and controls to the iOS reference, uses the requested Helvetica Neue font stack, and applies viewport safe-area insets when a browser provides them. Safari chrome and the native status bar are owned by their respective platforms, so full-device screenshots include different system UI; view content is compared at the same logical screen dimensions. Other platforms, OS releases, accessibility font sizes, and dark appearance may render differently.

Reference images from the current build:

- Native iOS: [`swiftui-native.jpg`](output/playwright/swiftui-native.jpg)
- Converted HTML: [`web-ios-sized.png`](output/playwright/web-ios-sized.png)
- Interactive browser checks: [`browser_checks.js`](tests/browser_checks.js)

The translation is close to this measured iOS sample for the supported components; the requested Helvetica Neue intentionally differs from SwiftUI's default San Francisco font. It does not promise pixel equality for arbitrary SwiftUI views. `frame` constraints, Dynamic Type, navigation-bar scroll collapse, and Apple framework behavior still depend on native layout and runtime APIs that plain HTML/CSS/JavaScript do not reproduce.

Browser regression checks are in `tests/browser_checks.js` and use the optional Playwright CLI (it is not needed to run the converter):

```sh
mkdir -p output/playwright/demo
python3 swift_to_web.py tests/fixtures/SwiftDemo --output output/playwright/demo.zip --overwrite
python3 -m zipfile -e output/playwright/demo.zip output/playwright/demo
# For direct file opening, create output/playwright/config.json containing:
# {"allowUnrestrictedFileAccess":true,"outputDir":"output/playwright"}
playwright-cli open "file://$PWD/output/playwright/demo/index.html" --config output/playwright/config.json
playwright-cli run-code --filename tests/browser_checks.js
playwright-cli close
```

The browser checks cover the parenthesized Button closure form, named `ForEach(lastUsedApps.prefix(4), id: \.self) { app in … }` and conditional gear image, custom font size and padding rendering, counter/reset, input updates and focus, toggles, conditional branches, independent repeated-view state, navigation/back, embedded local images/report, mobile overflow, offline reload, and external-request detection. Unit tests also cover the implicit `$0` ForEach form. Screenshots are written under `output/playwright/`.
