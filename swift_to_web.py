#!/usr/bin/env python3
"""Convert a local SwiftUI Xcode/SwiftPM target to a self-contained HTML ZIP."""
import argparse
from pathlib import Path
import re
import sys
from swiftweb import Compiler, ConversionError, discover, document, write_zip

__version__ = "2.0.0"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"swift-to-web {__version__}")
    parser.add_argument("project_path", help="Project directory, .xcodeproj, or Package.swift")
    parser.add_argument("--output", "-o", help="Destination .zip (default: <project-name>-web.zip)")
    parser.add_argument("--target", help="Exact Xcode or SwiftPM source target name")
    parser.add_argument("--entry", help="SwiftUI App or View struct name to use as the root")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output ZIP")
    args = parser.parse_args(argv)
    try:
        project = discover(args.project_path, args.target)
        model = Compiler(project).compile(args.entry)
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip(".-") or "app"
        output = write_zip(args.output or name + "-web.zip", document(model), args.overwrite)
    except (ConversionError, OSError, UnicodeError, RecursionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Created {output} (index.html; {len(project.sources)} Swift files; {len(model['diagnostics'])} warnings)")
    for item in model["diagnostics"]:
        print(f"warning: {item['file']}:{item['line']}: {item['message']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
