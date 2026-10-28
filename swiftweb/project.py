"""Read-only discovery for local SwiftPM and Xcode source targets."""
from dataclasses import dataclass, field
from pathlib import Path
import json
import re
from .syntax import ConversionError, lex, matching, arguments, values

EXCLUDED = {".git", ".build", "build", "DerivedData", "Pods", "Carthage", ".swiftpm", "node_modules"}


def local_files(root, suffix):
    return sorted(p for p in root.rglob("*" + suffix)
                  if not any(part in EXCLUDED for part in p.relative_to(root).parts)
                  and p.is_file() and p.resolve().is_relative_to(root.resolve()))


@dataclass
class Project:
    name: str
    root: Path
    sources: list
    resource_roots: list
    notes: list
    resource_excludes: list = field(default_factory=list)

    def __post_init__(self):
        self.root = self.root.resolve()
        self.sources = [p.resolve() for p in self.sources]
        self.resource_roots = [p.resolve() for p in self.resource_roots]
        self.resource_excludes = [p.resolve() for p in self.resource_excludes]


def choose(candidates, target):
    if target:
        found = [c for c in candidates if c[0] == target]
        if len(found) != 1:
            raise ConversionError(f"Target {target!r} is not unique or does not exist. Available: " + ", ".join(c[0] for c in candidates))
        return found[0]
    if len(candidates) != 1:
        raise ConversionError("Select --target from: " + ", ".join(c[0] for c in candidates))
    return candidates[0]


def string_value(tokens):
    if len(tokens) != 1 or not tokens[0].value.startswith('"'):
        raise ConversionError("Target names and paths must be literal strings")
    try:
        return json.loads(tokens[0].value)
    except ValueError as e:
        raise ConversionError(str(e)) from e


def swiftpm(manifest, target):
    root = manifest.parent
    tokens = lex(manifest.read_text(encoding="utf-8"), str(manifest))
    # Only Package.targets contains source target declarations. A .target() inside
    # dependencies is a reference, not a second source target.
    target_tokens = None
    for i in range(len(tokens) - 1):
        if values(tokens[i:i + 2]) == ["Package", "("]:
            end = matching(tokens, i + 1)
            package_args = dict(arguments(tokens[i + 2:end]))
            target_tokens = package_args.get("targets")
            break
    if not target_tokens or target_tokens[0].value != "[" or matching(target_tokens, 0) != len(target_tokens) - 1:
        raise ConversionError("Package.targets must be a literal target array")
    from .syntax import split_top
    candidates = []
    for declaration in split_top(target_tokens[1:-1]):
        if values(declaration[:3]) not in ([".", "target", "("], [".", "executableTarget", "("]):
            if values(declaration[:2]) in ([".", "testTarget"], [".", "binaryTarget"], [".", "plugin"], [".", "systemLibrary"]):
                continue
            raise ConversionError("Unsupported target declaration in Package.targets")
        end = matching(declaration, 2)
        if end != len(declaration) - 1:
            raise ConversionError("Target declarations must be literal .target(...) or .executableTarget(...)")
        args = dict(arguments(declaration[3:end]))
        if "name" not in args:
            continue
        name = string_value(args["name"])
        path = root / (string_value(args["path"]) if "path" in args else "Sources/" + name)
        if not path.resolve().is_relative_to(root.resolve()):
            raise ConversionError(f"Target {name} is outside the package directory")
        if "sources" in args:
            ts = args["sources"]
            if not ts or ts[0].value != "[" or matching(ts, 0) != len(ts) - 1:
                raise ConversionError("SwiftPM sources must be a literal string array")
            sources = []
            from .syntax import split_top
            for part in split_top(ts[1:-1]):
                p = path / string_value(part)
                if not p.resolve().is_relative_to(path.resolve()):
                    raise ConversionError("Source paths must remain inside the target")
                sources.extend(local_files(p, ".swift") if p.is_dir() else [p])
        else:
            sources = local_files(path, ".swift") if path.is_dir() else []
        excluded = []
        if "exclude" in args:
            from .syntax import split_top
            ts = args["exclude"]
            if not ts or ts[0].value != "[" or matching(ts, 0) != len(ts) - 1:
                raise ConversionError("SwiftPM exclude must be a literal string array")
            excluded = [(path / string_value(p)).resolve() for p in split_top(ts[1:-1])]
            sources = [p for p in sources if not any(p.resolve().is_relative_to(e) for e in excluded)]
        candidates.append((name, sources, [path], args, excluded))
    if not candidates:
        raise ConversionError("No local .target or .executableTarget found in Package.swift")
    name, sources, roots, args, excluded = choose(candidates, target)
    notes = []
    if "dependencies" in args and values(args["dependencies"]) != ["[", "]"]:
        notes.append("SwiftPM target dependencies are not resolved; only this target's local sources are converted.")
    return Project(name, root, sorted(set(sources)), roots, notes, excluded)


class Plist:
    """Minimal OpenStep plist reader for project.pbxproj; never invokes Xcode."""
    def __init__(self, text):
        text = re.sub(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*',
                      lambda m: m.group() if m.group().startswith('"') else "", text, flags=re.S)
        self.ts = re.findall(r'"(?:\\.|[^"\\])*"|[{}()=;,]|[^\s{}()=;,]+', text)
        self.i = 0

    def take(self):
        if self.i >= len(self.ts):
            raise ConversionError("Truncated Xcode project file")
        t = self.ts[self.i]
        self.i += 1
        return t

    def expect(self, value):
        if self.take() != value:
            raise ConversionError("Invalid Xcode project file: expected " + value)

    def read(self):
        t = self.take()
        if t == "{":
            result = {}
            while self.i < len(self.ts) and self.ts[self.i] != "}":
                key = self.take().strip('"')
                self.expect("=")
                result[key] = self.read()
                self.expect(";")
            self.expect("}")
            return result
        if t == "(":
            result = []
            while self.i < len(self.ts) and self.ts[self.i] != ")":
                result.append(self.read())
                if self.i < len(self.ts) and self.ts[self.i] == ",":
                    self.i += 1
            self.expect(")")
            return result
        if t.startswith('"'):
            try:
                return json.loads(t)
            except ValueError:
                return t[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        return t


def xcode(bundle, target):
    root = bundle.parent.resolve()
    project_file = bundle / "project.pbxproj"
    data = Plist(project_file.read_text(encoding="utf-8")).read()
    if not isinstance(data, dict) or not isinstance(data.get("objects"), dict):
        raise ConversionError("Invalid Xcode project: expected an objects dictionary")
    objects = data["objects"]
    if not all(isinstance(v, dict) for v in objects.values()):
        raise ConversionError("Invalid Xcode project object")
    candidates = [(v.get("name", k), k, v) for k, v in objects.items()
                  if isinstance(v, dict) and v.get("isa") == "PBXNativeTarget"]
    if not candidates:
        raise ConversionError("No native targets in Xcode project")
    name, target_id, selected = choose(candidates, target)
    parents = {}
    for key, value in objects.items():
        for child in value.get("children", []):
            parents[child] = key

    def resolve(ref, seen=()):
        if ref in seen:
            raise ConversionError("Cyclic Xcode file groups")
        obj = objects.get(ref, {})
        tree = obj.get("sourceTree", "<group>")
        path = obj.get("path", "")
        if "$" in path or tree not in ("<group>", "SOURCE_ROOT"):
            return None
        parent = resolve(parents[ref], seen + (ref,)) if ref in parents and tree == "<group>" else root
        if parent is None:
            return None
        result = (parent / path).resolve()
        return result if result.is_relative_to(root) else None

    sources, resource_roots, notes = [], [], []
    for phase_id in selected.get("buildPhases", []):
        phase = objects.get(phase_id, {})
        if phase.get("isa") not in ("PBXSourcesBuildPhase", "PBXResourcesBuildPhase"):
            continue
        for build_id in phase.get("files", []):
            ref = objects.get(build_id, {}).get("fileRef")
            path = resolve(ref) if ref else None
            if path is None:
                notes.append(f"Unresolved Xcode build file: {build_id}")
            elif phase.get("isa") == "PBXSourcesBuildPhase" and path.suffix == ".swift":
                sources.append(path)
            elif phase.get("isa") == "PBXResourcesBuildPhase":
                resource_roots.append(path)
    # Xcode 16 folder-synchronized targets have no explicit build-file lists.
    for group in selected.get("fileSystemSynchronizedGroups", []):
        path = resolve(group)
        obj = objects.get(group, {})
        if path and path.is_dir():
            excluded = []
            for exception_id in obj.get("exceptions", []):
                exception = objects.get(exception_id, {})
                if exception.get("target") == target_id:
                    excluded.extend((path / p).resolve() for p in exception.get("membershipExceptions", []))
            sources.extend(p for p in local_files(path, ".swift") if not any(p.is_relative_to(e) for e in excluded))
            # Asset discovery honors the same target membership exceptions.
            resource_roots.extend(p for p in local_files(path, ".json")
                                  if p.name == "Contents.json" and not any(p.is_relative_to(e) for e in excluded))
            resource_roots.extend(p for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg")
                                  for p in local_files(path, ext) if not any(p.is_relative_to(e) for e in excluded))
    if selected.get("dependencies"):
        notes.append("Xcode target dependencies are not resolved; only the selected target is converted.")
    return Project(name, root, sorted(set(sources)), sorted(set(resource_roots)), notes)


def discover(path, target=None):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConversionError(f"Input does not exist: {path}")
    if path.name == "Package.swift" and path.is_file():
        result = swiftpm(path, target)
    elif path.suffix == ".xcodeproj" and path.is_dir():
        result = xcode(path, target)
    elif path.is_dir():
        bundles = sorted(p for p in path.glob("*.xcodeproj") if p.is_dir())
        if (path / "Package.swift").is_file() and bundles:
            raise ConversionError("Both Xcode and SwiftPM projects found; pass a .xcodeproj or Package.swift explicitly")
        if (path / "Package.swift").is_file():
            result = swiftpm(path / "Package.swift", target)
        elif len(bundles) == 1:
            result = xcode(bundles[0], target)
        elif bundles:
            raise ConversionError("Multiple Xcode projects; pass a specific .xcodeproj path")
        else:
            raise ConversionError("Expected a Package.swift or .xcodeproj in the project directory")
    else:
        raise ConversionError("Expected a directory, .xcodeproj, or Package.swift")
    if not result.sources:
        raise ConversionError(f"Target {result.name} contains no local Swift sources")
    for source in result.sources:
        if not source.is_file() or not source.resolve().is_relative_to(result.root):
            raise ConversionError(f"Missing or external source: {source}")
    return result
