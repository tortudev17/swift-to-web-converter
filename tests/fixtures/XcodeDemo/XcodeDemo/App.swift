import SwiftUI

@main
struct DemoApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}

struct ContentView: View {
    @State private var count = 0
    @State private var name = "World"
    @State private var enabled = true
    @State private var lastUsedApps = ["Apple", "Pear", "matches"]
    @State private var scoreData = ScoreData(matches: [ScoreMatch(home: 2, away: 1), ScoreMatch(home: 0, away: 0)])
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Hello, \(name)!").font(.largeTitle).bold()
                    Text("Local points: \(scoreData.homePoints())")
                    Image("Badge").resizable().scaledToFit().frame(width: 72, height: 72)
                    NameEditor(name: $name)
                    Toggle("Show count", isOn: $enabled)
                    if enabled {
                        Text("Count: \(count)").foregroundStyle(.blue)
                    } else {
                        Text("Count hidden")
                    }
                    HStack {
                        Button(action: {
                            count += 1
                        }, label: {
                            Text("Increment")
                        })
                        Button { count = 0 } label: { Text("Reset") }
                    }
                    Divider()
                    ForEach(lastUsedApps.prefix(4), id: \.self) { app in
                        FruitRow(title: app)
                        if app == "matches" {
                            Image(systemName: "gear")
                        }
                    }
                    NavigationLink("Details") {
                        DetailView()
                    }
                    Text("Footer").opacity(0.8)
                }
                .padding()
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .navigationTitle("Swift Demo")
        }
    }
}

struct NameEditor: View {
    @Binding var name: String
    var body: some View {
        TextField("Your name", text: $name)
    }
}

struct FruitRow: View {
    let title: String
    @State private var selected = false
    var body: some View {
        HStack {
            Text(title)
            Button("Select \(title)") { selected.toggle() }
            if selected { Text("Selected") }
        }
    }
}

struct DetailView: View {
    var body: some View {
        VStack {
            Text("Details screen").font(.title)
            Text("40 pt sample").font(.system(size: 40))
            Text("Padding sample").padding(.horizontal, 12).padding(.top, 5)
            Text("Works offline")
        }.padding().background(.yellow).cornerRadius(12)
    }
}

struct ScoreMatch {
    var home: Int
    var away: Int
}

struct ScoreData {
    var matches: [ScoreMatch]

    func homePoints() -> Int {
        var total: Int = 0
        for match in matches {
            if match.home > match.away {
                total += 3
            } else if match.home == match.away {
                total += 1
            }
        }
        return total
    }
}
