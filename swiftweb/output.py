"""Self-contained HTML and deterministic, single-member ZIP output."""
from pathlib import Path
import html
import json
import os
import tempfile
import zipfile
from urllib.parse import urlsplit
from .syntax import ConversionError


def document(model):
    assets = Path(__file__).parent
    # Escape script terminators, HTML metacharacters, and JS line separators in JSON.
    payload = json.dumps(model, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    css = (assets / "style.css").read_text(encoding="utf-8")
    js = (assets / "runtime.js").read_text(encoding="utf-8")
    connect_source = "'none'"
    script_source = "'unsafe-inline'"
    if model.get("firebase", {}).get("baseUrl"):
        parts = urlsplit(model["firebase"]["baseUrl"])
        if parts.scheme == "https" and parts.hostname:
            connect_source = html.escape(f"{parts.scheme}://{parts.netloc}", quote=True)
    firebase_notice = ""
    if model.get("firebase"):
        config = model["firebase"]
        path = html.escape("/" + str(config.get("path", "")), quote=False)
        behavior = (f"Reads Firebase {path} when opened and every 10 seconds. " if config.get("readEnabled") else "")
        behavior += "Browser state is saved locally."
        if config.get("writeEnabled"):
            behavior += " Converted write actions can update Firebase."
        firebase_notice = (
            f'<p class="firebase-notice">{behavior} '
            'Use <code>?firebase=off</code> to disable network access or '
            '<code>?cache=off</code> to disable local saving.</p>'
        )
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src {script_source}; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src {connect_source}; base-uri 'none'; form-action 'none'">
<title>{html.escape(model['name'])}</title>
<style>{css}</style>
</head>
<body>
<main id="app" aria-label="Converted SwiftUI app"></main>
<noscript>This converted app requires JavaScript.</noscript>
<details id="conversion-report"><summary id="report-summary">Conversion report</summary>
<p>Generated from the supported SwiftUI subset. Native framework behavior is not reproduced.</p>{firebase_notice}<ul id="report-items"></ul></details>
<script id="swiftweb-model" type="application/json">{payload}</script>
<script>{js}</script>
</body>
</html>
'''


def write_zip(destination, content, overwrite=False):
    destination = Path(destination).expanduser().absolute()
    if destination.suffix.lower() != ".zip":
        raise ConversionError("Output path must end in .zip")
    if destination.exists() and not overwrite:
        raise ConversionError(f"Output exists: {destination}; pass --overwrite to replace it")
    if not destination.parent.is_dir():
        raise ConversionError(f"Output directory does not exist: {destination.parent}")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".swiftweb-", suffix=".zip", dir=destination.parent, delete=False) as f:
            temp = Path(f.name)
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("index.html", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            archive.writestr(info, content.encode("utf-8"))
        if overwrite:
            os.replace(temp, destination)
        else:
            # Atomic no-clobber publication even if another process created the output.
            os.link(temp, destination)
        return destination
    except OSError as e:
        raise ConversionError(f"Cannot write ZIP: {e}") from e
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
