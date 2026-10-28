import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

from swiftweb import Compiler, ConversionError, discover, document, write_zip
from swiftweb.project import Project, Plist
from swiftweb.syntax import Expr, lex, matching, values

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def walk(node):
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


class SyntaxTests(unittest.TestCase):
    def test_nested_comments_and_string_interpolation(self):
        ts = lex('/* outer /* nested */ end */\nText("A // \\(name) { }") // skipped\n', "a.swift")
        self.assertEqual(values(ts), ["Text", "(", '"A // \\(name) { }"', ")"])
        self.assertEqual(ts[0].line, 2)
        self.assertEqual(matching(ts, 1), 3)
        result = Expr.parse(ts[2:3], {"name": {"op": "state", "key": "n"}})
        self.assertEqual(result["op"], "concat")
        self.assertEqual(result["items"][1]["key"], "n")

    def test_expression_precedence_and_rejection(self):
        result = Expr.parse(lex("1 + 2 * 3 == 7 && true"), {})
        self.assertEqual(result["kind"], "&&")
        self.assertEqual(result["a"]["a"]["b"]["kind"], "*")
        result = Expr.parse(lex('false ? "no" : "yes"'), {})
        self.assertEqual(result["op"], "conditional")
        result = Expr.parse(lex("nil ?? 3"), {})
        self.assertEqual(result["kind"], "??")
        with self.assertRaises(ValueError):
            Expr.parse(lex("fetch(\"https://example.com\")"), {})
        with self.assertRaises(ValueError):
            Expr.parse(lex("unknown + 1"), {})

    def test_unclosed_syntax_has_location(self):
        with self.assertRaisesRegex(ConversionError, "a.swift:1"):
            lex('Text("oops)', "a.swift")
        with self.assertRaisesRegex(ConversionError, "a.swift:1"):
            matching(lex("VStack {", "a.swift"), 1)


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def compile_source(self, source, entry=None):
        path = self.root / "App.swift"
        path.write_text(source)
        project = Project("Test", self.root, [path], [self.root], [])
        return Compiler(project).compile(entry)

    def test_swiftpm_interactive_ir(self):
        project = discover(FIXTURES / "SwiftDemo")
        model = Compiler(project).compile()
        self.assertEqual(model["diagnostics"], [])
        nodes = list(walk(model["root"]))
        types = {n.get("type") for n in nodes}
        self.assertTrue({"Button", "TextField", "Toggle", "if", "ForEach", "NavigationLink", "Image"} <= types)
        self.assertTrue(any(n.get("src", "").startswith("data:image/svg+xml;base64,") for n in nodes))
        self.assertTrue(any(n.get("kind") == "+=" for n in nodes))
        self.assertTrue(any(n.get("kind") == "toggle" for n in nodes))

    def test_xcode_target_membership_and_assets(self):
        project = discover(FIXTURES / "XcodeDemo" / "XcodeDemo.xcodeproj")
        self.assertEqual([p.name for p in project.sources], ["App.swift"])
        model = Compiler(project).compile()
        self.assertEqual(model["diagnostics"], [])
        self.assertTrue(any(n.get("src", "").startswith("data:") for n in walk(model)))

    def test_unknown_views_modifiers_frameworks_and_expressions(self):
        model = self.compile_source('''import SwiftUI
import MapKit
@main struct Demo: App { var body: some Scene { WindowGroup { Screen() } } }
struct Screen: View {
 var body: some View {
  VStack {
   Text(network()).shadow(radius: 2)
   Text("task safe").task { fatalError() }
   Map { Text("not a sibling") }
   Image(systemName: "star")
   Image("missing")
   Text
   Text("preserved")
  }
 }
}''')
        warnings = model["diagnostics"]
        self.assertTrue(all(d["file"] == "App.swift" and d["line"] > 0 for d in warnings))
        self.assertTrue(any(n.get("type") == "missing-image" for n in walk(model)))
        self.assertTrue(any(n.get("type") == "unsupported" and "Text requires" in n.get("message", "") for n in walk(model)))
        messages = "\n".join(d["message"] for d in warnings)
        for expected in ("MapKit", "shadow", "Unsupported view: Map", "network", "system symbol", "missing", "Text requires", ".task work is skipped"):
            self.assertIn(expected, messages)
        literals = [n.get("value") for n in walk(model) if n.get("op") == "literal"]
        self.assertIn("preserved", literals)
        self.assertNotIn("not a sibling", literals)

    def test_malformed_declaration_does_not_swallow_following_view(self):
        model = self.compile_source('''import SwiftUI
struct
struct Screen: View { var body: some View { Text("Recovered") } }
@main struct Demo: App { var body: some Scene { WindowGroup { Screen() } } }''')
        diagnostic = next(d for d in model["diagnostics"] if "Malformed struct declaration" in d["message"])
        self.assertEqual((diagnostic["file"], diagnostic["line"]), ("App.swift", 2))
        self.assertTrue(any(n.get("type") == "Text" and n.get("text", {}).get("value") == "Recovered" for n in walk(model["root"])))

    def test_global_functions_are_reported_without_executing_or_swallowing_app(self):
        model = self.compile_source('''import SwiftUI
func makeBlankData() -> String { writeFirebase(data: <#unfinished#>) }
@main struct Demo: App { var body: some Scene { WindowGroup { Text("Still rendered") } } }''')
        diagnostic = next(d for d in model["diagnostics"] if "Global Swift function makeBlankData" in d["message"])
        self.assertEqual((diagnostic["file"], diagnostic["line"]), ("App.swift", 2))
        self.assertTrue(any(n.get("type") == "Text" and n.get("text", {}).get("value") == "Still rendered" for n in walk(model["root"])))
        self.assertNotIn("unfinished", [n.get("value") for n in walk(model)])

    def test_optional_global_helper_bold_font_and_rounded_rectangle(self):
        model = self.compile_source('''func maybeNum(input: Int?) -> String {
 if input == nil { return "-" }
 else { return "\\(input!)" }
}
struct Score { var value: Int? }
struct Screen: View {
 @State var score = Score(value: nil)
 var body: some View {
  Text(maybeNum(input: score.value)).font(.title2.bold()).foregroundStyle(.white)
   .padding(.horizontal, 24).background(.blue, in: RoundedRectangle(cornerRadius: 18)).fixedSize()
 }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        text = next(node for node in walk(model["root"]) if node.get("type") == "Text")
        self.assertEqual(text["text"]["op"], "globalCall")
        font = next(modifier for modifier in text["modifiers"] if modifier["name"] == "font")
        self.assertEqual((font["value"]["value"], font["weight"]), (22, "700"))
        background = next(modifier for modifier in text["modifiers"] if modifier["name"] == "background")
        self.assertEqual(background["shape"], "RoundedRectangle")
        self.assertEqual(background["radius"]["value"], 18)

    def test_foundation_date_formatting_and_reference_date_interval(self):
        model = self.compile_source('''func formatDate(_ date: Date) -> String {
 let calendar = Calendar.current
 let day = calendar.component(.day, from: date)
 let month = calendar.component(.month, from: date)
 let year = calendar.component(.year, from: date) % 100
 return String(format: "%02d/%02d/%02d", day, month, year)
}
struct Match { var date: Int }
struct Screen: View {
 @State var match = Match(date: 0)
 var body: some View {
  Text("\\(formatDate(Date(timeIntervalSinceReferenceDate: TimeInterval(match.date))))")
 }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        text = next(node for node in walk(model["root"]) if node.get("type") == "Text")
        date_call = text["text"]["items"][1]
        self.assertEqual(date_call["op"], "globalCall")
        self.assertEqual(date_call["args"]["date"]["op"], "date")
        self.assertTrue(any(statement.get("value", {}).get("op") == "format" for statement in model["functions"]["formatDate"]["body"]))

    def test_view_builder_if_let_dictionary_counts_and_compact_map(self):
        model = self.compile_source('''struct Player { var id: Int; var name: String }
struct Match { var scorers: [Int] }
struct ScoreData {
 var matches: [Match]
 var players: [Player]
 func latest() -> Match? { matches.first }
}
extension Array where Element: Hashable {
 func uniqued() -> [Element] { self }
}
struct Screen: View {
 @State var data = ScoreData(matches: [Match(scorers: [1, 1, 2])], players: [Player(id: 1, name: "Alex"), Player(id: 2, name: "Sam")])
 var body: some View {
  VStack {
   if let players = data.latest()?.scorers, !players.isEmpty {
    let counts = Dictionary(players.map { ($0, 1) }, uniquingKeysWith: +)
    let names = players.uniqued().compactMap { id -> String? in
     guard let player = data.players.first(where: { $0.id == id }) else { return nil }
     let count = counts[id, default: 0]
     return player.name + (count > 1 ? " x\\(count)" : "")
    }
    Text(names.joined(separator: ", ")).multilineTextAlignment(.center)
   } else { Text("No scorers") }
  }
 }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        nodes = list(walk(model["root"]))
        conditional = next(node for node in nodes if node.get("type") == "if")
        self.assertEqual(conditional["bindings"][0]["name"], "players")
        self.assertTrue(any(node.get("type") == "let" and node.get("name") == "counts" for node in nodes))
        self.assertTrue(any(node.get("type") == "let" and node.get("name") == "names" for node in nodes))
        compact_map = next(node for node in nodes if node.get("op") == "call" and node.get("name") == "compactMap")
        self.assertEqual(compact_map["args"][0]["op"], "closure")
        joined = next(node for node in nodes if node.get("op") == "call" and node.get("name") == "joined")
        self.assertEqual(joined["args"][0]["value"], ", ")

    def test_list_rows_from_model_method_and_list_style(self):
        model = self.compile_source('''struct Match { var id: Int; var score: Int }
struct MatchData {
 var matches: [Match]
 func orderedMatches() -> [Match] { matches.sorted { $0.id < $1.id } }
}
struct Screen: View {
 @State var data = MatchData(matches: [Match(id: 2, score: 3)])
 var body: some View {
  List(data.orderedMatches(), rowContent: { match in Text("Score: \\(match.score)") })
   .listStyle(.inset)
 }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        list_node = next(node for node in walk(model["root"]) if node.get("type") == "List")
        self.assertEqual(list_node["items"]["op"], "call")
        self.assertEqual(list_node["children"][0]["text"]["op"], "concat")
        style = next(modifier for modifier in list_node["modifiers"] if modifier["name"] == "listStyle")
        self.assertEqual(style["value"], "inset")

    def test_firebase_read_cache_configuration_and_button_write(self):
        model = self.compile_source('''let databaseURL = "https://demo-default-rtdb.europe-west1.firebasedatabase.app"
struct RemoteData { var title: String }
struct Screen: View {
 @State var data = RemoteData(title: "Local")
 var body: some View {
  VStack {
   Text(data.title)
   Button(action: { try writeFirebase(path: "main", data: data) }, label: { Text("Save") })
  }.task { await refreshFirebase() }
 }
 private func refreshFirebase() async {
  let received = try await readFirebase(path: "main", as: RemoteData.self)
  data = received
 }
}
func readFirebase<T: Decodable>(path: String, as type: T.Type = T.self) throws -> T { type }
func writeFirebase<T: Encodable>(path: String, data: T) throws { }
''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        self.assertEqual(model["firebase"]["baseUrl"], "https://demo-default-rtdb.europe-west1.firebasedatabase.app")
        self.assertEqual((model["firebase"]["path"], model["firebase"]["type"], model["firebase"]["writeEnabled"]), ("main", "RemoteData", True))
        button = next(node for node in walk(model["root"]) if node.get("type") == "Button")
        self.assertEqual(button["actions"][0]["kind"], "firebaseWrite")
        html = document(model)
        self.assertIn("connect-src https://demo-default-rtdb.europe-west1.firebasedatabase.app", html)
        self.assertIn("?firebase=off", html)
        self.assertIn("?cache=off", html)

    def test_firebase_helper_alone_does_not_enable_write_capability(self):
        model = self.compile_source('''let databaseURL = "https://demo-default-rtdb.europe-west1.firebasedatabase.app"
struct RemoteData { var title: String }
struct Screen: View {
 @State var data = RemoteData(title: "Local")
 var body: some View { Text(data.title).task { await refreshFirebase() } }
 private func refreshFirebase() async {
  let received = try await readFirebase(path: "main", as: RemoteData.self)
  data = received
 }
}
func readFirebase<T: Decodable>(path: String, as type: T.Type = T.self) throws -> T { type }
func writeFirebase<T: Encodable>(path: String, data: T) throws { }
''', "Screen")
        self.assertFalse(model["firebase"]["writeEnabled"])
        self.assertNotIn("write", model["firebase"])

    def test_firebase_write_only_action_is_configured_without_background_read(self):
        model = self.compile_source('''let databaseURL = "https://demo-default-rtdb.europe-west1.firebasedatabase.app"
struct Screen: View {
 @State var title = "Local"
 var body: some View {
  Button(action: { try writeFirebase(path: "main", data: title) }, label: { Text("Save") })
 }
}
func writeFirebase<T: Encodable>(path: String, data: T) throws { }
''', "Screen")
        self.assertEqual(model["firebase"]["readEnabled"], False)
        self.assertEqual(model["firebase"]["writeEnabled"], True)

    def test_firebase_write_helper_runs_only_when_a_translated_button_calls_it(self):
        source = '''let databaseURL = "https://demo-default-rtdb.europe-west1.firebasedatabase.app"
struct RemoteData { var title: String }
func makeBlankData() -> RemoteData {
 let data = RemoteData(title: "")
 try! writeFirebase(path: "main", data: data)
 return data
}
struct Screen: View {
 var body: some View {
  Button(action: { makeBlankData() }, label: { Text("Reset remote data") })
 }
}
func writeFirebase<T: Encodable>(path: String, data: T) throws { }
'''
        unused_source = source.replace('Button(action: { makeBlankData() }, label: { Text("Reset remote data") })',
                                       'Text("No remote write yet")')
        unused = self.compile_source(unused_source, "Screen")
        self.assertNotIn("firebase", unused)
        self.assertIn("makeBlankData", unused["functions"])
        self.assertTrue(any("firebaseWrite" == statement.get("kind")
                            for statement in unused["functions"]["makeBlankData"]["body"]))

        called = self.compile_source(source, "Screen")
        self.assertEqual(called["diagnostics"], [])
        self.assertTrue(called["firebase"]["writeEnabled"])
        button = next(node for node in walk(called["root"]) if node.get("type") == "Button")
        self.assertEqual(button["actions"][0]["kind"], "functionCall")
        self.assertEqual(button["actions"][0]["name"], "makeBlankData")

    def test_async_global_firebase_helper_supports_try_await_if_let_and_discarded_response(self):
        model = self.compile_source('''let databaseURL = "https://demo-default-rtdb.europe-west1.firebasedatabase.app"
func saveProfile(_ name: String) async throws {
 if let oldName = try await readFirebase(path: "users/demo/name", as: String.self) {
  _ = try await firebaseRequest(path: "users/demo/name", method: "PATCH", body: JSONEncoder().encode(name))
 } else {
  try await updateFirebase(path: "users/demo/name", method: "PATCH", data: ["name": name])
 }
}
struct Screen: View {
 var body: some View { Button("Save") { saveProfile("Ada") } }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        self.assertTrue(model["firebase"]["writeEnabled"])
        function = model["functions"]["saveProfile"]
        conditional = next(statement for statement in function["body"] if statement["kind"] == "if")
        self.assertEqual(conditional["bindings"][0]["name"], "oldName")
        self.assertEqual(conditional["bindings"][0]["value"]["op"], "firebaseCall")
        write = conditional["yes"][0]["value"]
        self.assertEqual((write["op"], write["name"]), ("firebaseCall", "firebaseRequest"))
        button = next(node for node in walk(model["root"]) if node.get("type") == "Button")
        self.assertEqual(button["actions"][0]["kind"], "functionCall")

    def test_helper_calls_bind_defaulted_middle_arguments_and_forward_globals(self):
        model = self.compile_source('''import SwiftUI
@main struct Demo: App { var body: some Scene { WindowGroup { Screen() } } }
struct Screen: View {
 var body: some View { Button("Run") { run() } }
 private func addActivity(to updates: inout [String: Int], type: String, target: String? = nil, details: [String: Int] = [:]) { }
 private func run() {
  var updates: [String: Int] = [:]
  addActivity(to: &updates, type: "investment", details: ["amount": 4])
  _ = laterValue()
 }
}
func laterValue() -> Int { return 7 }
''')
        self.assertEqual(model["diagnostics"], [])
        run = next(method for key, method in model["componentMethods"].items() if key.endswith(":run"))
        call = run["body"][1]["value"]
        self.assertEqual(call["op"], "componentCall")
        self.assertEqual(call["args"]["target"]["value"], None)
        self.assertEqual(call["args"]["details"]["op"], "dictionary")
        later = run["body"][2]["value"]
        self.assertEqual((later["op"], later["name"]), ("globalCall", "laterValue"))

    def test_do_catch_where_is_emitted_for_runtime_error_filtering(self):
        model = self.compile_source('''import SwiftUI
func guardedWrite() async {
 do { try await updateFirebase(path: "main", method: "PATCH", data: [:]) }
 catch let error as FirebaseHTTPError where error.statusCode == 412 { return }
}
struct Screen: View { var body: some View { Button("Save") { guardedWrite() } } }
let databaseURL = "https://demo-default-rtdb.europe-west1.firebasedatabase.app"
''', "Screen")
        statement = model["functions"]["guardedWrite"]["body"][0]
        self.assertEqual(statement["kind"], "doCatch")
        self.assertEqual(statement["catchTest"]["kind"], "==")

    def test_model_closure_initializers_capture_callbacks_and_environment_dismiss(self):
        model = self.compile_source('''import SwiftUI
struct SubmitRequest { let title: String; let onSubmit: (Int) async throws -> Void }
struct Screen: View {
 @Environment(\\.dismiss) private var dismiss
 var body: some View { Button("Open") { open() } }
 private func open() {
  let id = 7
  let request = SubmitRequest(title: "Save") { amount in finish(id + amount) }
 }
 private func finish(_ value: Int) { dismiss() }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        opened = next(method for key, method in model["componentMethods"].items() if key.endswith(":open"))
        request = opened["body"][1]["value"]
        self.assertEqual(request["op"], "construct")
        callback = next(field["value"] for field in request["fields"] if field["name"] == "onSubmit")
        self.assertEqual(callback["op"], "closure")
        self.assertEqual(callback["params"], ["amount"])
        finish = next(method for key, method in model["componentMethods"].items() if key.endswith(":finish"))
        self.assertEqual(finish["body"][0]["value"]["op"], "dismissCall")

    def test_remove_all_predicate_and_min_by_helpers_compile(self):
        model = self.compile_source('''import SwiftUI
struct Score { let id: Int }
func removeScore(_ id: Int, from scores: inout [Score]) { scores.removeAll { $0.id == id } }
func earliest(_ scores: [Score]) -> Score? { return scores.min { $0.id < $1.id } }
struct Screen: View { var body: some View { Text("Scores") } }
''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        remove = model["functions"]["removeScore"]["body"][0]["value"]
        self.assertEqual((remove["op"], remove["name"]), ("call", "removeAll"))
        earliest = model["functions"]["earliest"]["body"][0]["value"]
        self.assertEqual(earliest["op"], "extremumBy")

    def test_text_editor_is_a_writable_multiline_state_control(self):
        model = self.compile_source('''import SwiftUI
struct Screen: View {
 @State var draft = ""
 var body: some View { TextEditor(text: $draft).frame(minHeight: 90) }
}''', "Screen")
        editor = next(node for node in walk(model["root"]) if node.get("type") == "TextEditor")
        self.assertEqual(editor["binding"]["key"], "1:draft")
        self.assertEqual(editor["modifiers"][0]["name"], "frame")

    def test_button_actions_can_branch_on_state_and_write_each_branch(self):
        model = self.compile_source('''import SwiftUI
struct Screen: View {
 @State var selected = false
 var body: some View {
  Button(action: { if selected { selected = false } else { selected = true } }, label: { Text("Toggle") })
 }
}''', "Screen")
        button = next(node for node in walk(model["root"]) if node.get("type") == "Button")
        self.assertEqual(button["actions"][0]["kind"], "program")
        branch = button["actions"][0]["statements"][0]
        self.assertEqual(branch["kind"], "if")
        self.assertEqual(branch["yes"][0]["kind"], "stateAssign")
        self.assertEqual(branch["no"][0]["kind"], "stateAssign")

    def test_nonprimitive_state_with_network_initializer_falls_back_and_is_reported(self):
        model = self.compile_source('''import SwiftUI
struct Payload { var items: [String] }
@main struct Demo: App { var body: some Scene { WindowGroup { Screen() } } }
struct Screen: View {
 @State var data: Payload = try? readFirebase(path: "main") as? Payload
 var body: some View { Text("Screen remains visible") }
}''')
        messages = "\n".join(d["message"] for d in model["diagnostics"])
        self.assertIn("readFirebase", messages)
        self.assertIn("persistence and external Swift code are not translated or executed", messages)
        self.assertNotIn("unsupported expression or unknown name: try", messages)
        self.assertTrue(any(n.get("type") == "Text" and n.get("text", {}).get("value") == "Screen remains visible" for n in walk(model["root"])))

    def test_binding_passes_parent_state_into_custom_view(self):
        model = self.compile_source('''import SwiftUI
@main struct Demo: App { var body: some Scene { WindowGroup { Parent() } } }
struct Parent: View {
 @State var name = "World"
 var body: some View { VStack { Text("Parent: \\(name)"); Editor(name: $name) } }
}
struct Editor: View {
 @Binding var name: String
 var body: some View { TextField("Name", text: $name) }
}''')
        self.assertEqual(model["diagnostics"], [])
        nodes = list(walk(model["root"]))
        state_keys = [state["key"] for node in nodes if node.get("type") == "component" for state in node.get("states", [])]
        field = next(node for node in nodes if node.get("type") == "TextField")
        self.assertEqual(state_keys, [field["key"]])

    def test_navigation_picker_and_nested_settings_binding(self):
        model = self.compile_source('''import SwiftUI
enum Tab: String, CaseIterable { case one = "One"; case two = "Two" }
enum Route { case detail(Int) }
struct Settings { var maximum = 3 }
struct Screen: View {
 @State var selected = Tab.one
 @State var settings = Settings()
 @State var path: [Route] = []
 var body: some View {
  NavigationStack(path: $path) {
   VStack {
    Picker("Tab", selection: $selected) {
     ForEach(Tab.allCases) { item in Text(item.rawValue).tag(item) }
    }.pickerStyle(.segmented)
    Stepper("Maximum: \\(settings.maximum)", value: $settings.maximum, in: 1...10)
    Button("Open") { path.append(.detail(settings.maximum)) }
   }
   .navigationDestination(for: Route.self) { route in
    switch route { case .detail(let number): Text("Detail \\(number)") }
   }
  }
 }
}''', "Screen")
        nodes = list(walk(model["root"]))
        types = {node.get("type") for node in nodes}
        self.assertTrue({"NavigationStack", "Picker", "Stepper", "Button", "switch"} <= types)
        self.assertTrue(any(node.get("type") == "component" and node.get("states") for node in nodes))
        self.assertFalse(any("navigationDestination" in diagnostic["message"] for diagnostic in model["diagnostics"]))
        self.assertFalse(any("Picker" in diagnostic["message"] or "Stepper" in diagnostic["message"] for diagnostic in model["diagnostics"]))

    def test_sheet_and_confirmation_dialog_bindings(self):
        model = self.compile_source('''import SwiftUI
struct Screen: View {
 @State var show = false
 @State var selected: Int? = nil
 var body: some View {
  VStack { Button("Open") { show = true } }
   .sheet(isPresented: $show) { Text("Sheet content") }
   .confirmationDialog("Delete item", isPresented: Binding(
    get: { selected != nil },
    set: { if !$0 { selected = nil } }
   )) {
    Button("Delete", role: .destructive) { selected = nil }
   } message: { Text("This cannot be undone.") }
 }
}''', "Screen")
        modifiers = [modifier for node in walk(model["root"]) for modifier in node.get("modifiers", [])]
        self.assertTrue({"sheet", "confirmationDialog"} <= {modifier["name"] for modifier in modifiers})
        self.assertFalse(any("Unsupported view: message" in diagnostic["message"] for diagnostic in model["diagnostics"]))
        self.assertFalse(any("requires a writable @State presentation binding" in diagnostic["message"] for diagnostic in model["diagnostics"]))

    def test_incomplete_binding_and_empty_interpolation_recover_with_placeholders(self):
        model = self.compile_source('''struct Screen: View {
 @Binding var
 var body: some View { VStack { Text("Before"); Text("Value: \\()"); Text("After") } }
}''', "Screen")
        messages = "\n".join(d["message"] for d in model["diagnostics"])
        self.assertIn("Malformed @Binding declaration", messages)
        self.assertTrue(any(node.get("type") == "unsupported" and "empty string interpolation" in node.get("message", "") for node in walk(model["root"])))
        text_values = [node.get("text", {}).get("value") for node in walk(model["root"]) if node.get("type") == "Text"]
        self.assertEqual(text_values, ["Before", "After"])

    def test_literal_models_and_pure_loop_methods_are_compiled_safely(self):
        model = self.compile_source('''struct ScoreMatch { var home: Int; var away: Int }
struct ScoreData {
 var matches: [ScoreMatch]
 func homePoints() -> Int {
  var total: Int = 0
  for match in matches {
   if match.home > match.away { total += 3 }
   else if match.home == match.away { total += 1 }
  }
  return total
 }
}
struct Screen: View {
 @State var data = ScoreData(matches: [ScoreMatch(home: 2, away: 1), ScoreMatch(home: 0, away: 0)])
 var body: some View { Text("Total: \\(data.homePoints())") }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        self.assertIn("ScoreData.homePoints", model["methods"])
        text = next(node["text"] for node in walk(model["root"]) if node.get("type") == "Text")
        self.assertEqual(text["op"], "concat")
        self.assertEqual(text["items"][1]["op"], "call")

    def test_capsule_background_and_fixed_size_modifiers(self):
        model = self.compile_source('''struct Screen: View {
 var body: some View { Text("23").padding(.horizontal, 12).background(.blue, in: Capsule()).fixedSize() }
}''', "Screen")
        self.assertEqual(model["diagnostics"], [])
        node = next(node for node in walk(model["root"]) if node.get("type") == "Text")
        background = next(modifier for modifier in node["modifiers"] if modifier["name"] == "background")
        fixed = next(modifier for modifier in node["modifiers"] if modifier["name"] == "fixedSize")
        self.assertEqual(background["shape"], "Capsule")
        self.assertTrue(fixed["horizontal"] and fixed["vertical"])

    def test_sorted_trailing_closure_and_first_force_unwrap(self):
        model = self.compile_source('''struct ScoreMatch { var date: Int }
struct ScoreData {
 var matches: [ScoreMatch]
 func orderedMatches() -> [ScoreMatch] { matches.sorted { $0.date < $1.date } }
 func earliestDate() -> Int {
  let first = orderedMatches().first!
  return first.date
 }
 func fallbackDate() -> Int {
  let first = orderedMatches().first ?? .init(date: 0)
  return first.date
 }
}
struct Screen: View {
 @State var data = ScoreData(matches: [ScoreMatch(date: 2024), ScoreMatch(date: 2021)])
 var body: some View { Text("Earliest: \\(data.earliestDate())") }
}''', "Screen")
        self.assertIn("ScoreData.orderedMatches", model["methods"])
        self.assertIn("ScoreData.earliestDate", model["methods"])
        self.assertIn("ScoreData.fallbackDate", model["methods"])
        self.assertTrue(any("Force unwrap" in d["message"] for d in model["diagnostics"]))
        self.assertFalse(any("Model method" in d["message"] and "unsupported" in d["message"] for d in model["diagnostics"]))

    def test_recursion_is_reported(self):
        model = self.compile_source('struct Loop: View { var body: some View { Loop() } }', "Loop")
        self.assertIn("Recursive view expansion", model["diagnostics"][0]["message"])

    def test_ambiguous_and_explicit_entries(self):
        source = '''struct A: App { var body: some Scene { WindowGroup { Text("A") } } }
struct B: App { var body: some Scene { WindowGroup { Text("B") } } }'''
        with self.assertRaisesRegex(ConversionError, "--entry"):
            self.compile_source(source)
        self.assertEqual(self.compile_source(source, "B")["diagnostics"], [])
        with self.assertRaisesRegex(ConversionError, "Unknown --entry"):
            self.compile_source(source, "Missing")

    def test_missing_entry_and_malformed_source(self):
        with self.assertRaisesRegex(ConversionError, "SwiftUI App entry"):
            self.compile_source("import Foundation\nlet x = 3")
        with self.assertRaisesRegex(ConversionError, "unclosed"):
            self.compile_source("struct Broken: View {")

    def test_conditional_views_and_inline_action_closure(self):
        model = self.compile_source('''struct Test: View {
@State var on = false
@State var count = 0
var body: some View {
 VStack {
  if on { Text("On") } else if count > 1 { Text("Many") } else { Text("Off") }
  Button(action: { on.toggle(); count += 1 }) { Text("Do") }
  Button("Unsupported") { doSomething() }
 }
}
}''', "Test")
        self.assertEqual(sum(n.get("type") == "if" for n in walk(model)), 2)
        self.assertTrue(any(n.get("kind") == "toggle" for n in walk(model)))
        self.assertTrue(any("Unsupported action" in d["message"] for d in model["diagnostics"]))

    def test_literal_foreach_and_custom_properties(self):
        model = self.compile_source('''struct Test: View {
 var body: some View { VStack {
 ForEach(["a", "b"], id: \\.self) { item in Row(title: item) }
 ForEach(0..<10) { item in Text("no") }
 } }
}
struct Row: View { let title: String
 var body: some View { Text(title) }
}''', "Test")
        self.assertTrue(any(n.get("op") == "local" for n in walk(model)))
        range_loop = next(node for node in walk(model["root"])
                          if node.get("type") == "ForEach" and node.get("items", {}).get("op") == "binary")
        self.assertEqual(range_loop["items"]["kind"], "..<")

    def test_html_escape_and_zip_determinism(self):
        model = self.compile_source('struct Test: View { var body: some View { Text("</script><img src=x onerror=alert(1)>") } }', "Test")
        page = document(model)
        self.assertNotIn("</script><img", page)
        self.assertIn("\\u003c/script\\u003e", page)
        payload = re.search(r'<script id="swiftweb-model" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(payload), model)
        a = write_zip(self.root / "a.zip", page)
        b = write_zip(self.root / "b.zip", page)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        with zipfile.ZipFile(a) as z:
            self.assertEqual(z.namelist(), ["index.html"])
            self.assertEqual(z.read("index.html").decode(), page)
        with self.assertRaisesRegex(ConversionError, "--overwrite"):
            write_zip(a, page)
        write_zip(a, page, True)
        with self.assertRaisesRegex(ConversionError, "directory"):
            write_zip(self.root / "absent" / "a.zip", page)
        with self.assertRaisesRegex(ConversionError, ".zip"):
            write_zip(self.root / "a.txt", page)

    def test_svg_namespace_allowed_external_references_rejected(self):
        (self.root / "good.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>')
        (self.root / "bad.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>')
        model = self.compile_source('struct Test: View { var body: some View { VStack { Image("good"); Image("bad") } } }', "Test")
        self.assertEqual(sum(n.get("type") == "Image" for n in walk(model)), 1)
        self.assertTrue(any("external references" in d["message"] for d in model["diagnostics"]))

    def test_project_discovery_errors_and_target_selection(self):
        with self.assertRaisesRegex(ConversionError, "does not exist"):
            discover(self.root / "absent")
        with self.assertRaisesRegex(ConversionError, "Expected"):
            discover(self.root)
        (self.root / "Package.swift").write_text('let p = Package(targets: [.target(name: "A"), .target(name: "B")])')
        for name in ("A", "B"):
            path = self.root / "Sources" / name
            path.mkdir(parents=True)
            (path / "App.swift").write_text('struct ' + name + ': View { var body: some View { Text("hello") } }')
        with self.assertRaisesRegex(ConversionError, "--target"):
            discover(self.root)
        self.assertEqual(discover(self.root, "B").name, "B")
        with self.assertRaisesRegex(ConversionError, "Available"):
            discover(self.root, "Nope")

    def test_swiftpm_sources_and_excludes(self):
        (self.root / "Package.swift").write_text('let p = Package(targets: [.target(name: "A", path: "Code", exclude: ["Skip"], sources: ["UI", "Skip"])])')
        for name in ("UI", "Skip", "Other"):
            path = self.root / "Code" / name
            path.mkdir(parents=True)
            (path / "Test.swift").write_text("import SwiftUI")
        project = discover(self.root)
        self.assertEqual([p.parent.name for p in project.sources], ["UI"])

    def test_xcode_synchronized_groups_and_exceptions(self):
        bundle = self.root / "Demo.xcodeproj"
        bundle.mkdir()
        app = self.root / "App"
        app.mkdir()
        (app / "Main.swift").write_text('struct Main: View { var body: some View { Text("Main") } }')
        (app / "Skip.swift").write_text('struct Main: View { var body: some View { Text("skip") } }')
        (bundle / "project.pbxproj").write_text('''{ objects = {
 T = { isa = PBXNativeTarget; name = Demo; fileSystemSynchronizedGroups = (G,); };
 G = { isa = PBXFileSystemSynchronizedRootGroup; path = App; sourceTree = SOURCE_ROOT; exceptions = (E,); };
 E = { isa = PBXFileSystemSynchronizedBuildFileExceptionSet; target = T; membershipExceptions = (Skip.swift,); };
 }; }''')
        project = discover(bundle)
        self.assertEqual([p.name for p in project.sources], ["Main.swift"])
        self.assertEqual(Compiler(project).compile("Main")["diagnostics"], [])

    def test_dependency_target_references_are_not_source_targets(self):
        (self.root / "Package.swift").write_text('let p = Package(targets: [.executableTarget(name: "App", dependencies: [.target(name: "Core")]), .target(name: "Core")])')
        for name in ("App", "Core"):
            path = self.root / "Sources" / name
            path.mkdir(parents=True)
            (path / "Main.swift").write_text('struct Main: View { var body: some View { Text("Hello") } }')
        project = discover(self.root, "App")
        self.assertEqual(len(project.sources), 1)
        self.assertEqual(len(project.notes), 1)
        self.assertEqual(discover(self.root, "Core").name, "Core")

    def test_negative_state_and_parent_state_inside_foreach(self):
        model = self.compile_source('''struct Test: View {
 @State var count = -2
 var body: some View {
  ForEach(["A", "B"], id: \\.self) { name in
   Text("Count: \\(count)")
   Button(name) { count += 1 }
  }
 }
}''', "Test")
        self.assertEqual(model["diagnostics"], [])
        self.assertEqual(model["root"]["states"][0]["value"]["value"], -2)
        state_key = model["root"]["states"][0]["key"]
        self.assertTrue(any(n.get("kind") == "+=" and n["key"] == state_key for n in walk(model)))

    def test_button_closures_implicit_foreach_system_image_and_sized_modifiers(self):
        model = self.compile_source('''struct Test: View {
 @State var count = 0
 @State var lastUsedApps = ["matches", "notes"]
 var body: some View { VStack {
  Button(action: { count += 1 }, label: { Text("Increment") })
  ForEach(lastUsedApps.prefix(1), id: \\.self) {
   if $0 == "matches" { Image(systemName: "gear") }
  }
  ForEach(lastUsedApps.prefix(4), id: \\.self) { app in Text(app) }
  Text("Large").font(.system(size: 40))
  Text("Padded").padding(.horizontal, 12).padding(.top, 5)
 } }
}''', "Test")
        self.assertEqual(model["diagnostics"], [])
        nodes = list(walk(model))
        button = next(n for n in nodes if n.get("type") == "Button")
        self.assertEqual(button["actions"][0]["kind"], "+=")
        self.assertEqual(button["children"][0]["text"]["value"], "Increment")
        loop = next(n for n in nodes if n.get("type") == "ForEach")
        self.assertEqual(loop["items"]["op"], "prefix")
        self.assertEqual(loop["items"]["count"]["value"], 1)
        condition = next(n for n in loop["children"] if n.get("type") == "if")
        self.assertEqual(condition["test"]["a"]["op"], "local")
        named_loop = [n for n in nodes if n.get("type") == "ForEach"][1]
        self.assertEqual(named_loop["items"]["count"]["value"], 4)
        self.assertTrue(any(n.get("op") == "local" and n.get("key", "").startswith("loop") for n in walk(named_loop)))
        image = next(n for n in nodes if n.get("type") == "Image" and n.get("alt", {}).get("value") == "gear")
        self.assertTrue(image["src"].startswith("data:image/svg+xml;base64,"))
        large = next(n for n in nodes if n.get("type") == "Text" and n.get("text", {}).get("value") == "Large")
        self.assertEqual(large["modifiers"][0]["value"]["value"], 40)
        padded = next(n for n in nodes if n.get("type") == "Text" and n.get("text", {}).get("value") == "Padded")
        self.assertEqual([(m["edge"], m["value"]["value"]) for m in padded["modifiers"]], [("horizontal", 12), ("top", 5)])

    def test_laligabrains_groupbox_toolbar_and_dynamic_asset_images(self):
        for name in ("matchesIcon", "goalsIcon"):
            imageset = self.root / "Assets.xcassets" / (name + ".imageset")
            imageset.mkdir(parents=True)
            (imageset / "Contents.json").write_text(json.dumps({"images": [{"filename": "image.svg", "idiom": "universal"}]}))
            (imageset / "image.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>')
        model = self.compile_source('''import SwiftUI
struct Test: View {
 @State var apps: [String] = ["matches", "goals", ""]
 var body: some View {
  NavigationStack {
   GroupBox {
    VStack {
     Circle().frame(width: 20, height: 20)
     ForEach(apps.prefix(3), id: \\.self) { app in
      NavigationLink(destination: { Text("Destination") }, label: {
       Image("\\(app)Icon").resizable().scaledToFit().frame(width: 45, height: 45)
      })
     }
    }.padding(10)
   }
  }.toolbar(content: {
   HStack { Image("matchesIcon").resizable().frame(width: 45, height: 45) }
  })
 }
}''', "Test")
        types = {node.get("type") for node in walk(model)}
        self.assertTrue({"GroupBox", "Circle", "ForEach", "NavigationLink", "Image"} <= types)
        self.assertTrue(any(m.get("name") == "toolbar" for node in walk(model) for m in node.get("modifiers", [])))
        links = [node for node in walk(model) if node.get("type") == "NavigationLink"]
        self.assertEqual(links[0]["destination"][0]["text"]["value"], "Destination")
        self.assertEqual(links[0]["children"][0]["type"], "Image")
        self.assertEqual(set(model["assets"]), {"matchesIcon", "goalsIcon"})
        self.assertTrue(any("Missing dynamic image asset(s): Icon" in d["message"] for d in model["diagnostics"]))

    def test_default_background_uses_inherited_background_style(self):
        model = self.compile_source('''struct Test: View {
 var body: some View { Text("LaLiga Brains").background().backgroundStyle(.blue) }
}''', "Test")
        self.assertEqual(model["diagnostics"], [])
        text = next(node for node in walk(model) if node.get("type") == "Text")
        self.assertEqual([m["name"] for m in text["modifiers"]], ["background", "backgroundStyle"])
        self.assertEqual(text["modifiers"][1]["value"], "#007aff")
        self.assertIn("var(--swiftui-background-style, #f2f2f7)", document(model))

    def test_resource_exclusions_and_asset_variants(self):
        (self.root / "Package.swift").write_text('let p = Package(targets: [.target(name: "A", exclude: ["Excluded"])])')
        target = self.root / "Sources" / "A"
        (target / "Excluded").mkdir(parents=True)
        (target / "Excluded" / "hidden.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (target / "Main.swift").write_text('struct Main: View { var body: some View { Image("hidden") } }')
        project = discover(self.root)
        self.assertNotIn("hidden", Compiler(project).assets)
        imageset = target / "Logo.imageset"
        imageset.mkdir()
        (imageset / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (imageset / "Contents.json").write_text(json.dumps({"images": [{"filename": "logo.svg", "idiom": "universal"}, {"filename": "logo.svg", "idiom": "ipad"}]}))
        compiler = Compiler(project)
        self.assertIn("Logo", compiler.assets)
        self.assertTrue(any("variants" in d["message"] for d in compiler.diagnostics))
        (imageset / "Contents.json").write_text("[]")
        compiler = Compiler(project)
        self.assertTrue(any("Invalid image catalog" in d["message"] for d in compiler.diagnostics))

    def test_openstep_comments_preserve_quoted_paths(self):
        data = Plist('''// header
 { path = "A//B.swift"; /* ignored */ url = "https://example.com"; items = (a, b,); }''').read()
        self.assertEqual(data["path"], "A//B.swift")
        self.assertEqual(data["url"], "https://example.com")
        self.assertEqual(data["items"], ["a", "b"])
        bundle = self.root / "Bad.xcodeproj"
        bundle.mkdir()
        (bundle / "project.pbxproj").write_text("(one, two)")
        with self.assertRaisesRegex(ConversionError, "objects dictionary"):
            discover(bundle)

    def test_cli_success_failure_and_overwrite(self):
        version = subprocess.run([sys.executable, str(ROOT / "swift_to_web.py"), "--version"],
                                 capture_output=True, text=True)
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout.strip(), "swift-to-web 2.0.0")
        out = self.root / "app.zip"
        command = [sys.executable, str(ROOT / "swift_to_web.py"), str(FIXTURES / "SwiftDemo"), "--output", str(out)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 warnings", result.stdout)
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--overwrite", result.stderr)
        result = subprocess.run(command + ["--overwrite"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
