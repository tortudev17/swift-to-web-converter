"""SwiftUI subset -> serializable view IR, with source-located diagnostics."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import base64
import json
import mimetypes
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from .syntax import ConversionError, Expr, Token, arguments, lex, matching, split_top, values

SYSTEM_SYMBOLS = {
    "gear": '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="3.3"/><path d="M12 2.4v1.4M12 20.2v1.4M2.4 12h1.4M20.2 12h1.4M5.22 5.22l1 1M17.78 17.78l1 1M18.78 5.22l-1 1M6.22 17.78l-1 1"/></svg>',
    "gearshape": '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="3.3"/><path d="M12 2.4v1.4M12 20.2v1.4M2.4 12h1.4M20.2 12h1.4M5.22 5.22l1 1M17.78 17.78l1 1M18.78 5.22l-1 1M6.22 17.78l-1 1"/></svg>',
}

FIREBASE_BUILTINS = {
    "firebaseRequest": ("path", "method", "body", "headers"),
    "readFirebase": ("path", "as"),
    "readFirebaseSnapshot": ("path", "as"),
    "updateFirebase": ("path", "method", "data"),
    "createFirebase": ("path", "data"),
    "patchFirebaseRoot": ("updates",),
    "writeFirebase": ("path", "data"),
}


def top_level_token(tokens, wanted):
    index = 0
    while index < len(tokens):
        if tokens[index].value in ("(", "[", "{"):
            index = matching(tokens, index) + 1
        elif tokens[index].value == wanted:
            return index
        else:
            index += 1
    return None


@dataclass
class Declaration:
    name: str
    kind: str
    token: Token
    content: list
    body: list
    properties: list
    computed_views: dict
    computed_values: dict
    view_methods: dict
    helper_methods: dict
    closure_properties: list


class MethodParser:
    """Compile a small, side-effect-free Swift method subset into an IR."""
    def __init__(self, tokens, fields=(), names=None, method_names=()):
        self.ts = tokens
        self.i = 0
        if names is not None:
            self.names = names
        else:
            self.names = {name: {"op": "member", "a": {"op": "self"}, "name": name} for name in fields}
            self.names.update({name: {"op": "methodRef", "name": name} for name in method_names})

    def block(self):
        if self.i >= len(self.ts) or self.ts[self.i].value != "{":
            raise ValueError("expected a method block")
        end = matching(self.ts, self.i)
        body = self.ts[self.i + 1:end]
        self.i = end + 1
        return body

    def expression_until_line(self):
        start = self.i
        while self.i < len(self.ts):
            if self.ts[self.i].value == ";":
                break
            if (self.i > start and self.ts[self.i].line > self.ts[self.i - 1].line
                    and self.ts[self.i].value not in (".", "?", "+", "??")):
                break
            if self.ts[self.i].value in ("(", "[", "{"):
                self.i = matching(self.ts, self.i) + 1
            else:
                self.i += 1
        result = self.ts[start:self.i]
        while result and result[0].value in ("try", "await"):
            result = result[1:]
            if result and result[0].value in ("?", "!"):
                result = result[1:]
        if self.i < len(self.ts) and self.ts[self.i].value == ";":
            self.i += 1
        if not result:
            raise ValueError("expected a statement expression")
        return Expr.parse(result, self.names)

    def parse_block(self, body, names=None):
        parser = MethodParser(body, names=self.names if names is None else names)
        return parser.parse()

    def parse_if(self):
        self.i += 1
        start = self.i
        while self.i < len(self.ts) and self.ts[self.i].value != "{":
            if self.ts[self.i].value in ("(", "["):
                self.i = matching(self.ts, self.i) + 1
            else:
                self.i += 1
        if self.i >= len(self.ts):
            raise ValueError("if statement has no body")
        header = self.ts[start:self.i]
        outer_names = self.names
        yes_names = dict(outer_names)
        bindings, conditions = [], []
        for part in split_top(header):
            if part and part[0].value in ("let", "var"):
                name = part[1].value if len(part) > 1 else ""
                eq = next((index for index, item in enumerate(part) if item.value == "="), None)
                expression = Expr.parse(part[eq + 1:], yes_names) if eq is not None else Expr.parse([part[1]], yes_names)
                bindings.append({"name": name, "value": expression})
                yes_names[name] = {"op": "local", "key": name}
            elif part:
                conditions.append(Expr.parse(part, yes_names))
        test = {"op": "literal", "value": True}
        for expression in conditions:
            test = expression if test.get("value") is True else {"op": "binary", "kind": "&&", "a": test, "b": expression}
        yes_tokens = self.block()
        self.names = yes_names
        yes = self.parse_block(yes_tokens)
        self.names = outer_names
        no = []
        if self.i < len(self.ts) and self.ts[self.i].value == "else":
            self.i += 1
            if self.i < len(self.ts) and self.ts[self.i].value == "if":
                no = [self.parse_if()]
            else:
                no = self.parse_block(self.block())
        return {"kind": "if", "test": test, "bindings": bindings, "yes": yes, "no": no}

    def parse_switch(self):
        self.i += 1
        start = self.i
        while self.i < len(self.ts) and self.ts[self.i].value != "{":
            if self.ts[self.i].value in ("(", "["):
                self.i = matching(self.ts, self.i) + 1
            else:
                self.i += 1
        if self.i >= len(self.ts):
            raise ValueError("switch statement has no body")
        expression = Expr.parse(self.ts[start:self.i], self.names)
        ending = matching(self.ts, self.i)
        body = self.ts[self.i + 1:ending]
        case_starts, index = [], 0
        while index < len(body):
            if body[index].value in ("case", "default"):
                case_starts.append(index)
            if body[index].value in ("(", "[", "{"):
                index = matching(body, index) + 1
            else:
                index += 1
        cases = []
        for offset, case_start in enumerate(case_starts):
            limit = case_starts[offset + 1] if offset + 1 < len(case_starts) else len(body)
            colon = next((cursor for cursor in range(case_start + 1, limit) if body[cursor].value == ":"), None)
            if colon is None:
                raise ValueError("switch case needs a colon")
            is_default = body[case_start].value == "default"
            patterns = [] if is_default else split_top(body[case_start + 1:colon])
            if any(not pattern or pattern[0].value in ("let", "var") for pattern in patterns):
                raise ValueError("switch case binding patterns are unsupported in methods")
            values_for_case = [Expr.parse(pattern, self.names) for pattern in patterns]
            branch = self.parse_block(body[colon + 1:limit])
            cases.append({"default": is_default, "values": values_for_case, "body": branch})
        self.i = ending + 1
        return {"kind": "switch", "expression": expression, "cases": cases}

    def statement(self):
        token = self.ts[self.i]
        if token.value in ("try", "await"):
            self.i += 1
            if self.i < len(self.ts) and self.ts[self.i].value in ("?", "!"):
                self.i += 1
            if (self.i + 1 < len(self.ts) and self.ts[self.i].value == "writeFirebase"
                    and self.ts[self.i + 1].value == "("):
                start = self.i + 2
                end = matching(self.ts, self.i + 1)
                args = dict(arguments(self.ts[start:end]))
                if "path" not in args or "data" not in args:
                    raise ValueError("writeFirebase requires path: and data:")
                self.i = end + 1
                if self.i < len(self.ts) and self.ts[self.i].value == ";": self.i += 1
                return {"kind": "firebaseWrite", "path": Expr.parse(args["path"], self.names),
                        "value": Expr.parse(args["data"], self.names)}
            return self.statement()
        if token.value == "throw":
            self.i += 1
            return {"kind": "throw", "value": self.expression_until_line()}
        if token.value == "do":
            self.i += 1
            body = self.parse_block(self.block())
            catcher = []
            catch_name = "error"
            catch_test = None
            if self.i < len(self.ts) and self.ts[self.i].value == "catch":
                self.i += 1
                header_start = self.i
                if self.i < len(self.ts) and self.ts[self.i].value != "{":
                    while self.i < len(self.ts) and self.ts[self.i].value != "{":
                        self.i += 1
                header = self.ts[header_start:self.i]
                if header and header[0].value in ("let", "var") and len(header) > 1:
                    catch_name = header[1].value
                outer = self.names
                catch_names = dict(outer)
                catch_names[catch_name] = {"op": "local", "key": catch_name}
                where = next((index for index, item in enumerate(header) if item.value == "where"), None)
                if where is not None:
                    catch_test = Expr.parse(header[where + 1:], catch_names)
                catcher = self.parse_block(self.block(), catch_names)
                self.names = outer
            return {"kind": "doCatch", "body": body, "catchName": catch_name,
                    "catchTest": catch_test, "catch": catcher}
        if token.value == "Task" and self.i + 1 < len(self.ts) and self.ts[self.i + 1].value == "{":
            self.i += 1
            return {"kind": "task", "body": self.parse_block(self.block())}
        if token.value == "defer" and self.i + 1 < len(self.ts) and self.ts[self.i + 1].value == "{":
            self.i += 1
            return {"kind": "defer", "body": self.parse_block(self.block())}
        if token.value == "guard":
            self.i += 1
            start = self.i
            while self.i < len(self.ts) and self.ts[self.i].value != "else":
                if self.ts[self.i].value in ("(", "[", "{"):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("guard let must include else")
            parts = split_top(self.ts[start:self.i])
            outer_names = self.names
            success_names = dict(outer_names)
            bindings, tests = [], []
            for part in parts:
                if part and part[0].value in ("let", "var"):
                    name = part[1].value if len(part) > 1 else ""
                    eq = next((index for index, value in enumerate(part) if value.value == "="), None)
                    if eq is None:
                        expression = Expr.parse([part[1]], success_names)
                    else:
                        expression = Expr.parse(part[eq + 1:], success_names)
                    bindings.append({"name": name, "value": expression})
                    success_names[name] = {"op": "local", "key": name}
                elif part:
                    tests.append(Expr.parse(part, success_names))
            test = {"op": "literal", "value": True}
            for expression in tests:
                test = expression if test.get("value") is True else {"op": "binary", "kind": "&&", "a": test, "b": expression}
            self.i += 1
            self.names = outer_names
            otherwise = self.parse_block(self.block())
            self.names = success_names
            return {"kind": "guard", "bindings": bindings, "test": test, "else": otherwise}
        if token.value == "break":
            self.i += 1
            return {"kind": "break"}
        if token.value == "continue":
            self.i += 1
            return {"kind": "continue"}
        if token.value in ("var", "let"):
            self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("variable declaration has no name")
            name = self.ts[self.i].value
            self.i += 1
            while self.i < len(self.ts) and self.ts[self.i].value not in ("=", ";"):
                if self.i > 0 and self.ts[self.i].line > self.ts[self.i - 1].line:
                    break
                self.i += 1
            if self.i >= len(self.ts) or self.ts[self.i].value != "=":
                value = {"op": "literal", "value": None}
            else:
                self.i += 1
                value = self.expression_until_line()
            self.names[name] = {"op": "local", "key": name}
            return {"kind": "declare", "name": name, "value": value}
        if token.value == "for":
            self.i += 1
            header_start = self.i
            while self.i < len(self.ts) and self.ts[self.i].value != "in":
                if self.ts[self.i].value in ("(", "[", "{"):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            if self.i >= len(self.ts): raise ValueError("for loop must use `for name in collection`")
            pattern = self.ts[header_start:self.i]
            if pattern and pattern[0].value == "(" and matching(pattern, 0) == len(pattern) - 1:
                loop_names = [item[0].value for item in split_top(pattern[1:-1]) if item]
            else:
                loop_names = [pattern[0].value] if pattern else []
            if not loop_names: raise ValueError("for loop has no binding name")
            name = loop_names[0]
            self.i += 1
            start = self.i
            while self.i < len(self.ts) and self.ts[self.i].value != "{":
                if self.ts[self.i].value in ("(", "["):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("for loop has no body")
            collection = Expr.parse(self.ts[start:self.i], self.names)
            body_names = dict(self.names)
            # Keep tuple loop bindings available to the body.
            pattern_names = [item[0].value for item in split_top(pattern[1:-1]) if item] if pattern and pattern[0].value == "(" else [name]
            for loop_name in pattern_names: body_names[loop_name] = {"op": "local", "key": loop_name}
            body = self.parse_block(self.block(), body_names)
            return {"kind": "for", "names": pattern_names, "name": name, "items": collection, "body": body}
        if token.value == "if":
            return self.parse_if()
        if token.value == "switch":
            return self.parse_switch()
        if token.value == "return":
            self.i += 1
            if self.i >= len(self.ts) or self.ts[self.i].value in ("}", ";") or self.ts[self.i].line > token.line:
                if self.i < len(self.ts) and self.ts[self.i].value == ";": self.i += 1
                return {"kind": "return", "value": {"op": "literal", "value": None}}
            return {"kind": "return", "value": self.expression_until_line()}
        assignment = None
        cursor = self.i
        while cursor < len(self.ts):
            if self.ts[cursor].value in ("(", "[", "{"):
                cursor = matching(self.ts, cursor) + 1
            elif self.ts[cursor].value in ("=", "+=", "-="):
                assignment = cursor
                break
            elif self.ts[cursor].value == ";" or (cursor > self.i and self.ts[cursor].line > self.ts[cursor - 1].line):
                break
            else:
                cursor += 1
        if assignment is not None:
            lhs = Expr.parse(self.ts[self.i:assignment], self.names)
            operator = self.ts[assignment].value
            self.i = assignment + 1
            next_value = self.expression_until_line()
            target = Compiler.binding_reference(lhs)
            if target:
                return {"kind": "stateAssign", "binding": {"op": "binding", **target},
                        "operator": operator, "value": next_value}
            if lhs.get("op") == "local":
                return {"kind": "assign", "name": lhs["key"], "operator": operator, "value": next_value}
            return {"kind": "pathAssign", "target": lhs, "operator": operator, "value": next_value}
        return {"kind": "expression", "value": self.expression_until_line()}

    def parse(self):
        statements = []
        while self.i < len(self.ts):
            if self.ts[self.i].value == ";":
                self.i += 1
                continue
            start = self.i
            statements.append(self.statement())
            if self.i <= start:
                raise ValueError("method parser did not advance")
        if len(statements) == 1 and statements[0]["kind"] == "expression":
            statements[0]["kind"] = "return"
        return statements


class Compiler:
    def __init__(self, project):
        self.project = project
        self.diagnostics = []
        self._reported = set()
        self.declarations = {}
        self.counter = 0
        self.assets = {}
        self.state_initials = {}
        self.model_types = set()
        self.model_methods = {}
        self.model_field_types = {}
        self.model_defaults = {}
        self.global_functions = {}
        self.computed_properties = {}
        self.component_methods = {}
        self.enums = {}
        self.enum_case_raw_values = {}
        self.firebase_base_url = None
        self.firebase_read_calls = []
        self.firebase_write_helper = False
        self.firebase_write_action = False
        self.firebase_requested = False
        self.needs_asset_lookup = False
        for note in project.notes:
            self.report(Token("project", str(project.root), 1), note)
        for path in project.sources:
            self.read(path)
        self.load_assets()

    def report(self, token, message):
        try:
            file = str(Path(token.file).relative_to(self.project.root))
        except ValueError:
            file = token.file
        item = (file, token.line, message)
        if item not in self._reported:
            self._reported.add(item)
            self.diagnostics.append(dict(file=file, line=token.line, message=message))

    def read(self, path):
        tokens = lex(path.read_text(encoding="utf-8"), str(path))
        if any(token.value == "func" and index + 1 < len(tokens)
               and tokens[index + 1].value == "writeFirebase"
               for index, token in enumerate(tokens)):
            self.firebase_write_helper = True
        # Discover literal RTDB configuration and call sites before skipping
        # method bodies. No Swift code or network operation is executed here.
        for index, token in enumerate(tokens[:-1]):
            if (token.value in ("let", "var") and index + 3 < len(tokens)
                    and tokens[index + 1].value == "databaseURL"
                    and tokens[index + 2].value == "=" and tokens[index + 3].value.startswith('"')):
                try:
                    self.firebase_base_url = json.loads(tokens[index + 3].value)
                except (ValueError, json.JSONDecodeError):
                    self.report(token, "Firebase databaseURL must be a literal URL")
            if token.value not in ("readFirebase", "writeFirebase") or tokens[index + 1].value != "(":
                continue
            try:
                end = matching(tokens, index + 1)
                args = dict(arguments(tokens[index + 2:end]))
                path_tokens = args.get("path", [])
                if len(path_tokens) != 1 or not path_tokens[0].value.startswith('"'):
                    continue
                database_path = json.loads(path_tokens[0].value)
                if token.value == "readFirebase":
                    type_tokens = args.get("as", [])
                    type_name = type_tokens[0].value if len(type_tokens) >= 3 and values(type_tokens[1:3]) == [".", "self"] else None
                    self.firebase_read_calls.append({"path": database_path, "type": type_name, "token": token})
            except (ValueError, IndexError, json.JSONDecodeError):
                continue
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.value == "import" and i + 1 < len(tokens):
                module = tokens[i + 1].value
                if module not in ("SwiftUI", "Foundation", "Playgrounds"):
                    self.report(tokens[i + 1], f"Framework {module} is not translated")
                i += 2
            elif t.value == "func" and i + 2 < len(tokens) and tokens[i + 1].value == "writeFirebase":
                self.firebase_write_helper = True
                name = tokens[i + 1].value
                end = self.function_end(tokens, i)
                if not self.firebase_base_url:
                    self.report(t, f"Global Swift function {name} is not translated or executed")
                i = end + 1 if end is not None else i + 2
            elif t.value == "func" and i + 1 < len(tokens):
                name = tokens[i + 1].value
                end = self.function_end(tokens, i)
                try:
                    if name in FIREBASE_BUILTINS and self.firebase_base_url:
                        i = end + 1 if end is not None else i + 2
                        continue
                    if i + 2 < len(tokens) and tokens[i + 2].value == "<":
                        if name == "readFirebase" and self.firebase_base_url and self.firebase_read_calls:
                            i = end + 1 if end is not None else i + 2
                            continue
                        raise ValueError("generic global functions are unsupported")
                    function = self.parse_global_function(tokens, i, end)
                    if name in self.global_functions:
                        raise ValueError(f"duplicate global function {name} is ambiguous")
                    self.global_functions[name] = function
                except (ValueError, IndexError) as e:
                    body_tokens = tokens[i:end + 1] if end is not None else []
                    body_values = values(body_tokens)
                    if (name == "schoolDateLabel" and "DateFormatter" in body_values
                            and "dateFormat" in body_values and "capitalized" in body_values):
                        try:
                            signature = self.function_signature(tokens, i, end)
                            self.global_functions[name] = {"params": signature["params"],
                                                           "builtin": "spanishSchoolDateLabel"}
                        except (ValueError, IndexError):
                            self.report(t, f"Global Swift function {name} is not translated or executed: {e}")
                    else:
                        self.report(t, f"Global Swift function {name} is not translated or executed: {e}")
                i = end + 1 if end is not None else i + 2
            elif t.value in ("struct", "class", "enum", "extension") and i + 1 < len(tokens):
                if tokens[i + 1].value in ("struct", "class", "enum", "extension", "func", "init", "var", "let", "import"):
                    self.report(t, f"Malformed {t.value} declaration: expected a name")
                    i += 1
                    continue
                start = i
                j = i + 2
                while j < len(tokens) and tokens[j].value != "{":
                    j += 1
                if j == len(tokens):
                    break
                end = matching(tokens, j)
                header = values(tokens[i + 2:j])
                if t.value == "struct" and ":" in header and any(v in header for v in ("App", "View")):
                    kind = "App" if "App" in header else "View"
                    content = tokens[j + 1:end]
                    body, props, computed_views, computed_values, view_methods, helper_methods, closure_properties = self.members(content)
                    name = tokens[i + 1].value
                    if name in self.declarations:
                        raise ConversionError(f"Duplicate SwiftUI declaration {name}; conditional compilation and namespaced duplicates need separate targets")
                    self.declarations[name] = Declaration(name, kind, tokens[start], content, body, props,
                                                          computed_views, computed_values, view_methods, helper_methods, closure_properties)
                elif t.value == "enum":
                    self.collect_enum(tokens[i + 1].value, tokens[j + 1:end])
                elif t.value == "struct":
                    self.collect_model(tokens[i + 1].value, tokens[j + 1:end])
                elif (t.value == "extension" and tokens[i + 1].value == "Array"
                      and "Element" in header and "Hashable" in header
                      and any(values(tokens[k:k + 2]) == ["func", "uniqued"] for k in range(j + 1, end - 1))):
                    pass  # Array.uniqued() has a safe equivalent in the browser runtime.
                else:
                    self.report(t, f"{t.value} {tokens[i + 1].value} is outside the supported SwiftUI subset")
                i = end + 1
            elif t.value == "#":
                macro = tokens[i + 1].value if i + 1 < len(tokens) else ""
                if macro in ("Preview", "Playground"):
                    start = i + 2
                    if start < len(tokens) and tokens[start].value == "(":
                        start = matching(tokens, start) + 1
                    if start < len(tokens) and tokens[start].value == "{":
                        end = matching(tokens, start)
                        if macro == "Playground":
                            self.report(t, "#Playground code is design-time only and is not executed")
                        i = end + 1
                        continue
                self.report(t, "Conditional compilation and unsupported macros are not evaluated")
                i += 1
            else:
                i += 1

    @staticmethod
    def function_end(tokens, start):
        j = start + 2
        while j < len(tokens) and tokens[j].value != "{":
            if tokens[j].value in ("(", "["):
                j = matching(tokens, j) + 1
            else:
                j += 1
        return matching(tokens, j) if j < len(tokens) else None

    def parse_global_function(self, tokens, start, body_end):
        name = tokens[start + 1].value
        paren = start + 2
        while paren < len(tokens) and tokens[paren].value != "(":
            if tokens[paren].value in ("{", ";"):
                raise ValueError("expected a simple parameter list")
            paren += 1
        if paren >= len(tokens):
            raise ValueError("missing parameter list")
        params_end = matching(tokens, paren)
        params = []
        names = {}
        for part in split_top(tokens[paren + 1:params_end]):
            colon = next((index for index, token in enumerate(part) if token.value == ":"), None)
            if colon is None:
                raise ValueError("parameters need explicit types")
            declaration = [token.value for token in part[:colon]]
            if len(declaration) == 1:
                label = local_name = declaration[0]
            elif len(declaration) == 2:
                label, local_name = declaration
            else:
                raise ValueError("unsupported parameter declaration")
            if local_name in names:
                raise ValueError(f"duplicate parameter name {local_name}")
            equal = next((index for index in range(colon + 1, len(part)) if part[index].value == "="), None)
            parameter = {"label": label, "name": local_name}
            if equal is not None:
                try:
                    parameter["default"] = Expr.parse(part[equal + 1:], {})
                except (ValueError, IndexError) as e:
                    raise ValueError(f"default value for {local_name} is unsupported: {e}") from None
            params.append(parameter)
            names[local_name] = {"op": "local", "key": local_name}
        body_start = params_end + 1
        while body_start < len(tokens) and tokens[body_start].value != "{":
            body_start += 1
        if body_start >= len(tokens) or matching(tokens, body_start) != body_end:
            raise ValueError("missing function body")
        body = MethodParser(tokens[body_start + 1:body_end], names=names).parse()
        return {"params": params, "body": body}

    def collect_model(self, name, tokens):
        if name in self.model_types:
            return
        self.model_types.add(name)
        fields, methods, i = [], [], 0
        method_names = {tokens[index + 1].value for index in range(len(tokens) - 1) if tokens[index].value == "func"}
        while i < len(tokens):
            t = tokens[i]
            if t.value == "struct" and i + 1 < len(tokens):
                j = i + 2
                while j < len(tokens) and tokens[j].value != "{":
                    j += 1
                if j < len(tokens):
                    end = matching(tokens, j)
                    self.collect_model(tokens[i + 1].value, tokens[j + 1:end])
                    i = end + 1
                else:
                    i += 1
            elif t.value in ("var", "let") and i + 1 < len(tokens):
                field = tokens[i + 1].value
                fields.append(field)
                initializer = next((index for index in range(i + 2, len(tokens))
                                    if tokens[index].value == "=" or tokens[index].line > tokens[index - 1].line), None)
                if initializer is not None and tokens[initializer].value == "=":
                    value_start = initializer + 1
                    value_end = value_start
                    while value_end < len(tokens):
                        if value_end > value_start and tokens[value_end].line > tokens[value_end - 1].line:
                            break
                        if tokens[value_end].value in ("(", "[", "{"):
                            value_end = matching(tokens, value_end) + 1
                        else:
                            value_end += 1
                    try:
                        default_value = Expr.parse(tokens[value_start:value_end], {})
                        if self.literal_state_value(default_value):
                            self.model_defaults.setdefault(name, {})[field] = default_value
                    except (ValueError, IndexError):
                        pass
                if i + 2 < len(tokens) and tokens[i + 2].value == ":":
                    type_start = i + 3
                    type_end = type_start
                    while type_end < len(tokens) and tokens[type_end].value not in ("=", ";"):
                        if type_end > type_start and tokens[type_end].line > tokens[type_end - 1].line:
                            break
                        type_end += 1
                    type_tokens = values(tokens[type_start:type_end])
                    if len(type_tokens) == 3 and type_tokens[0] == "[" and type_tokens[2] == "]":
                        self.model_field_types.setdefault(name, {})[field] = {"array": type_tokens[1], "optional": "?" in type_tokens}
                    elif type_tokens and type_tokens[0] not in ("(", "{"):
                        self.model_field_types.setdefault(name, {})[field] = {"type": type_tokens[0], "optional": "?" in type_tokens}
                i += 2
            elif t.value == "func" and i + 1 < len(tokens):
                function = tokens[i + 1].value
                paren = i + 2
                while paren < len(tokens) and tokens[paren].value != "(":
                    paren += 1
                if paren == len(tokens):
                    self.report(t, f"Model method {name}.{function} has an invalid signature")
                    i += 2
                    continue
                params_end = matching(tokens, paren)
                body_start = params_end + 1
                while body_start < len(tokens) and tokens[body_start].value != "{":
                    body_start += 1
                if body_start == len(tokens):
                    self.report(t, f"Model method {name}.{function} has no body")
                    i = params_end + 1
                    continue
                body_end = matching(tokens, body_start)
                try:
                    signature = self.function_signature(tokens, i, body_end)
                    method_names_map = {
                        field: {"op": "member", "a": {"op": "self"}, "name": field} for field in fields
                    }
                    method_names_map.update({method: {"op": "methodRef", "name": method} for method in method_names})
                    method_names_map.update({parameter["name"]: {"op": "local", "key": parameter["name"]}
                                             for parameter in signature["params"]})
                    method_body = MethodParser(tokens[body_start + 1:body_end], names=method_names_map).parse()
                    methods.append((function, {"params": signature["params"], "body": method_body}))
                    body_values = values(tokens[body_start + 1:body_end])
                    if any(body_values[index:index + 3] in ([".", "first", "!"], [".", "last", "!"]) for index in range(len(body_values) - 2)):
                        self.report(t, f"Force unwrap in {name}.{function} may trap on empty data; the browser treats a missing element as nil")
                except (ValueError, IndexError) as e:
                    self.report(t, f"Model method {name}.{function} is unsupported: {e}")
                i = body_end + 1
            elif t.value == "{":
                i = matching(tokens, i) + 1
            else:
                i += 1
        for function, body in methods:
            key = f"{name}.{function}"
            if key in self.model_methods:
                self.report(Token(function), f"Duplicate model method {key} is ambiguous")
            else:
                self.model_methods[key] = body

    def members(self, tokens):
        body, props, computed_views, computed_values, view_methods, helper_methods, closure_properties, i, attribute = [], [], {}, {}, {}, {}, [], 0, None
        while i < len(tokens):
            t = tokens[i]
            if t.value == "@" and i + 1 < len(tokens):
                attribute = tokens[i + 1].value
                i += 2
                if i < len(tokens) and tokens[i].value == "(":
                    i = matching(tokens, i) + 1
            elif t.value in ("private", "public", "internal", "fileprivate", "static"):
                i += 1
            elif t.value in ("var", "let") and i + 1 < len(tokens):
                if attribute and tokens[i + 1].value in ("var", "let", "func", "init", "body"):
                    self.report(t, f"Malformed @{attribute} declaration: expected a property name")
                    attribute = None
                    i += 1
                    continue
                name, start = tokens[i + 1].value, i
                i += 2
                while i < len(tokens) and tokens[i].value not in ("=", "{", ";"):
                    if tokens[i].line > tokens[i - 1].line and tokens[i].value in ("@", "var", "let", "func", "private"):
                        break
                    i += 1
                if i < len(tokens) and tokens[i].value == "{":
                    end = matching(tokens, i)
                    if name == "body":
                        body = tokens[i + 1:end]
                    elif "View" in values(tokens[start + 2:i]) or attribute == "ViewBuilder":
                        computed_views[name] = tokens[i + 1:end]
                    else:
                        computed_values[name] = tokens[i + 1:end]
                    i = end + 1
                else:
                    default = []
                    if "->" in values(tokens[start + 2:i]):
                        closure_properties.append(name)
                    if i < len(tokens) and tokens[i].value == "=":
                        i += 1
                        begin = i
                        while i < len(tokens):
                            if tokens[i].value == ";":
                                break
                            if i > begin and tokens[i].line > tokens[i - 1].line and tokens[i].value != ".":
                                break
                            if tokens[i].value in ("(", "[", "{"):
                                i = matching(tokens, i) + 1
                            else:
                                i += 1
                        default = tokens[begin:i]
                    elif attribute == "State" and "?" in values(tokens[start + 2:i]):
                        default = [Token("nil", t.file, t.line)]
                    props.append((name, attribute, default, tokens[start]))
                attribute = None
            elif t.value == "{":
                i = matching(tokens, i) + 1
                attribute = None
            elif t.value in ("func", "init"):
                name = tokens[i + 1].value if i + 1 < len(tokens) else ""
                end = self.function_end(tokens, i)
                if end is None:
                    if not (name == "refreshFirebase" and self.firebase_base_url and self.firebase_read_calls):
                        self.report(t, "Swift methods and initializers are not translated")
                    i += 1
                    attribute = None
                    continue
                body_start = i + 2
                while body_start < end and tokens[body_start].value != "{":
                    body_start += 1
                return_tokens = tokens[matching(tokens, i + 2) + 1:body_start] if i + 2 < body_start and tokens[i + 2].value == "(" else []
                is_view = "View" in values(return_tokens) or attribute == "ViewBuilder"
                if name != "init" and is_view:
                    try:
                        signature = self.function_signature(tokens, i, end)
                        view_methods[name] = {"params": signature["params"], "body": tokens[body_start + 1:end], "token": t}
                    except (ValueError, IndexError) as e:
                        self.report(t, f"View method {name} is unsupported: {e}")
                elif (name == "refreshFirebase" and self.firebase_base_url and self.firebase_read_calls):
                    pass
                elif name != "init":
                    try:
                        signature = self.function_signature(tokens, i, end)
                        helper_methods[name] = {"params": signature["params"],
                                                "body": tokens[body_start + 1:end], "token": t}
                    except (ValueError, IndexError) as e:
                        self.report(t, f"Swift helper method {name} is unsupported: {e}")
                i = end + 1
                attribute = None
            else:
                i += 1
        return body, props, computed_views, computed_values, view_methods, helper_methods, closure_properties

    @staticmethod
    def function_signature(tokens, start, body_end):
        paren = start + 2
        while paren < len(tokens) and tokens[paren].value != "(":
            if tokens[paren].value in ("{", ";"):
                raise ValueError("expected a parameter list")
            paren += 1
        if paren >= len(tokens):
            raise ValueError("missing parameter list")
        params_end = matching(tokens, paren)
        params = []
        for part in split_top(tokens[paren + 1:params_end]):
            colon = next((index for index, token in enumerate(part) if token.value == ":"), None)
            if colon is None:
                raise ValueError("parameters need explicit types")
            declaration = [token.value for token in part[:colon] if token.value != "@escaping"]
            declaration = [value for value in declaration if value != "_" or len(declaration) > 1]
            if len(declaration) == 1:
                label = local_name = declaration[0]
            elif len(declaration) == 2:
                label, local_name = declaration
            else:
                raise ValueError("unsupported parameter declaration")
            params.append({"label": label, "name": local_name})
        body_start = params_end + 1
        while body_start < len(tokens) and tokens[body_start].value != "{":
            body_start += 1
        if body_start >= len(tokens) or matching(tokens, body_start) != body_end:
            raise ValueError("missing function body")
        return {"params": params, "body_start": body_start}

    def load_assets(self):
        files = set()
        for root in self.project.resource_roots:
            if root.is_file():
                files.add(root)
            elif root.is_dir():
                files.update(p for p in root.rglob("*") if p.is_file())
            else:
                self.report(Token("resource", str(root), 1), "Resource does not exist")
        for path in sorted(files):
            if any(path.resolve().is_relative_to(p) for p in self.project.resource_excludes):
                continue
            if not path.resolve().is_relative_to(self.project.root):
                continue
            if path.name == "Contents.json" and path.parent.suffix == ".imageset":
                token = Token("asset", str(path), 1)
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict) or not isinstance(data.get("images", []), list) or not all(isinstance(x, dict) for x in data.get("images", [])):
                        raise ValueError("expected an images array of objects")
                    variants = [x for x in data.get("images", []) if x.get("filename")]
                    preferred = [x for x in variants if x.get("idiom", "universal") == "universal" and not x.get("appearances")]
                    if variants and (len(variants) > 1 or len(preferred) != len(variants)):
                        self.report(token, f"Image {path.parent.stem}: asset variants are flattened to one image")
                    chosen = (preferred or variants)
                    if chosen:
                        image_path = path.parent / chosen[0]["filename"]
                        self.add_asset(path.parent.stem, image_path, token)
                except (ValueError, OSError, KeyError, TypeError) as e:
                    self.report(token, f"Invalid image catalog: {e}")
            elif path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg") and not any(p.suffix == ".imageset" for p in path.parents):
                self.add_asset(path.stem, path, Token("asset", str(path), 1))
                if path.stem in self.assets:
                    self.assets[path.name] = self.assets[path.stem]

    def add_asset(self, name, path, token):
        if not path.resolve().is_relative_to(self.project.root):
            self.report(token, "Asset path escapes the project directory")
            return
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
            self.report(token, f"Unsupported asset format: {path.suffix}")
            return
        try:
            raw = path.read_bytes()
            if path.suffix.lower() == ".svg":
                try:
                    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                        raise ValueError("SVG document types and entities are unsupported")
                    root = ET.fromstring(raw)
                    for element in root.iter():
                        if element.tag.split("}")[-1].lower() in ("script", "foreignobject"):
                            raise ValueError("SVG scripts and foreign objects are unsupported")
                        for attr, val in element.attrib.items():
                            attr = attr.split("}")[-1].lower()
                            if attr.startswith("on") or (attr == "href" and not val.startswith("#")):
                                raise ValueError("SVG event handlers and external references are unsupported")
                        css = " ".join(element.attrib.values()) + " " + (element.text or "")
                        if re.search(r"@import|url\(\s*['\"]?(?!#)", css, re.I):
                            raise ValueError("SVG external CSS references are unsupported")
                except (ET.ParseError, ValueError) as e:
                    self.report(token, str(e))
                    return
            uri = "data:" + (mimetypes.guess_type(path.name)[0] or "application/octet-stream") + ";base64," + base64.b64encode(raw).decode("ascii")
            if name in self.assets and self.assets[name] != uri:
                self.report(token, f"Ambiguous image name: {name}; image omitted")
                self.assets[name] = None
            else:
                self.assets[name] = uri
        except OSError as e:
            self.report(token, f"Cannot read asset: {e}")

    def expression(self, tokens, names, fallback=None):
        try:
            return Expr.parse(tokens, names)
        except (ValueError, IndexError) as e:
            self.report(tokens[0] if tokens else Token("expression"), str(e))
            return {"op": "literal", "value": fallback}

    @staticmethod
    def binding_reference(expression):
        """Return a writable @State root and safe field path for a binding expression."""
        if not isinstance(expression, dict):
            return None
        if expression.get("op") == "state":
            return {"key": expression["key"], "path": []}
        if expression.get("op") == "member":
            parent = Compiler.binding_reference(expression.get("a"))
            if parent:
                return {"key": parent["key"], "path": parent["path"] + [expression["name"]]}
        if expression.get("op") == "subscript":
            parent = Compiler.binding_reference(expression.get("a"))
            if parent:
                subscript = expression.get("key", {})
                part = subscript.get("value") if subscript.get("op") == "literal" else subscript
                return {"key": parent["key"], "path": parent["path"] + [part]}
        if expression.get("op") == "binding":
            return {"key": expression["key"], "path": list(expression.get("path", []))}
        return None

    def compile(self, entry=None):
        if entry:
            if entry not in self.declarations:
                raise ConversionError(f"Unknown --entry {entry!r}. Available: " + ", ".join(self.declarations))
            selected = self.declarations[entry]
        else:
            apps = [d for d in self.declarations.values() if d.kind == "App"]
            if len(apps) != 1:
                raise ConversionError("Expected one SwiftUI App entry; use --entry NAME. Found: " + (", ".join(d.name for d in apps) or "none"))
            selected = apps[0]
        if not selected.body:
            raise ConversionError(f"{selected.name} has no SwiftUI body")
        result = self.component(selected, [], {}, ())
        self.firebase_write_action = self.firebase_write_action or self.has_firebase_write(self.component_methods)
        model = {"name": self.project.name, "root": result, "diagnostics": self.diagnostics}
        source_hash = hashlib.sha256()
        for source in sorted(self.project.sources):
            source_hash.update(str(source.relative_to(self.project.root)).encode("utf-8"))
            source_hash.update(b"\0")
            source_hash.update(source.read_bytes())
            source_hash.update(b"\0")
        model["sourceId"] = source_hash.hexdigest()[:16]
        if self.model_methods:
            model["methods"] = self.model_methods
        if self.model_field_types:
            model["modelFields"] = self.model_field_types
        if self.model_defaults:
            model["modelDefaults"] = self.model_defaults
        if self.computed_properties:
            model["computedProperties"] = self.computed_properties
        if self.component_methods:
            model["componentMethods"] = self.component_methods
        if self.global_functions:
            model["functions"] = self.global_functions
        if self.enums:
            model["enums"] = self.enums
            model["enumCaseRawValues"] = self.enum_case_raw_values
        firebase = self.firebase_configuration(result)
        if firebase:
            model["firebase"] = firebase
        if self.firebase_requested and (not firebase or not firebase.get("readEnabled")):
            self.report(Token("Firebase", str(self.project.root), 1), "Firebase .task could not be connected to one literal RTDB URL, read path, and matching @State model")
        if self.needs_asset_lookup:
            model["assets"] = {name: uri for name, uri in sorted(self.assets.items()) if uri}
        return model

    @staticmethod
    def has_firebase_write(value):
        if isinstance(value, dict):
            if value.get("kind") == "firebaseWrite":
                return True
            if value.get("op") == "firebaseCall" and value.get("name") in {
                "firebaseRequest", "updateFirebase", "createFirebase", "patchFirebaseRoot", "writeFirebase"
            }:
                method = value.get("args", {}).get("method", {})
                method_name = method.get("value") if isinstance(method, dict) else None
                if value.get("name") == "firebaseRequest" and method_name == "GET":
                    return False
                return True
            return any(Compiler.has_firebase_write(child) for child in value.values())
        if isinstance(value, list):
            return any(Compiler.has_firebase_write(child) for child in value)
        return False

    def collect_enum(self, name, tokens):
        cases, index = [], 0
        while index < len(tokens):
            if tokens[index].value != "case":
                index += 1
                continue
            start = index + 1
            end = start
            while end < len(tokens) and tokens[end].value not in ("case", "var", "func", "init"):
                if tokens[end].value in ("(", "[", "{"):
                    end = matching(tokens, end) + 1
                else:
                    end += 1
            for part in split_top(tokens[start:end]):
                if not part:
                    continue
                case_name = part[0].value
                raw_value = case_name
                eq = next((i for i, token in enumerate(part) if token.value == "="), None)
                if eq is not None and eq + 1 < len(part):
                    raw_token = part[eq + 1]
                    if raw_token.value.startswith('"'):
                        try:
                            raw_value = json.loads(raw_token.value)
                        except (ValueError, json.JSONDecodeError):
                            pass
                    elif re.fullmatch(r"-?\d+", raw_token.value):
                        raw_value = int(raw_token.value)
                cases.append({"name": case_name, "rawValue": raw_value})
                self.enum_case_raw_values.setdefault(case_name, raw_value)
            index = end
        self.enums[name] = cases

    def firebase_configuration(self, root):
        if not self.firebase_base_url:
            return None
        parts = urlsplit(self.firebase_base_url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in ("", "/")):
            return None
        config = {
            "baseUrl": self.firebase_base_url.rstrip("/"),
            "readEnabled": False,
            "writeEnabled": self.firebase_write_action,
        }
        if self.firebase_requested:
            for call in self.firebase_read_calls:
                if not call["type"]:
                    continue
                state_key = self.state_for_model(root, call["type"])
                if state_key:
                    config.update({
                        "readEnabled": True,
                        "path": call["path"],
                        "type": call["type"],
                        "stateKey": state_key,
                        "refreshMs": 10000,
                    })
                    break
        return config if config["readEnabled"] or config["writeEnabled"] else None

    @staticmethod
    def state_for_model(node, type_name):
        if isinstance(node, dict):
            if node.get("type") == "component":
                for item in node.get("states", []):
                    value = item.get("value", {})
                    if value.get("op") == "construct" and value.get("type") == type_name:
                        return item.get("key")
            for value in node.values():
                found = Compiler.state_for_model(value, type_name)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = Compiler.state_for_model(value, type_name)
                if found:
                    return found
        return None

    def static_array_values(self, expression):
        if expression.get("op") == "array":
            items = expression.get("items", [])
            if all(item.get("op") == "literal" and isinstance(item.get("value"), (str, int, float, bool)) for item in items):
                return [item["value"] for item in items]
            return None
        if expression.get("op") == "state":
            initial = self.state_initials.get(expression.get("key"), {})
            return self.static_array_values(initial) if initial else None
        if expression.get("op") == "prefix":
            items = self.static_array_values(expression.get("a", {}))
            count = expression.get("count", {})
            if items is None:
                return None
            if count.get("op") == "literal" and isinstance(count.get("value"), (int, float)):
                return items[:max(0, int(count["value"]))]
            return items
        return None

    def literal_state_value(self, expression):
        op = expression.get("op")
        if op == "literal":
            return expression.get("value") is None or type(expression.get("value")) in (str, int, float, bool)
        if op == "array":
            return all(self.literal_state_value(item) for item in expression.get("items", []))
        if op == "dictionary":
            return all(self.literal_state_value(value) for _, value in expression.get("entries", []))
        if op == "enumCase":
            return all(self.literal_state_value(item) for item in expression.get("args", []))
        if op == "construct" and expression.get("type") in self.model_types:
            return all(self.literal_state_value(field["value"]) for field in expression.get("fields", []))
        return False

    def static_string_values(self, expression):
        if expression.get("op") == "literal" and isinstance(expression.get("value"), str):
            return [expression["value"]]
        if expression.get("op") == "local":
            candidates = expression.get("candidates")
            return [str(value) for value in candidates] if candidates is not None else None
        if expression.get("op") == "concat":
            result = [""]
            for part in expression.get("items", []):
                values_for_part = self.static_string_values(part)
                if values_for_part is None:
                    return None
                result = [prefix + suffix for prefix in result for suffix in values_for_part]
                if len(result) > 512:
                    return None
            return result
        return None

    @staticmethod
    def closure_body(tokens):
        if tokens and tokens[0].value == "{" and matching(tokens, 0) == len(tokens) - 1:
            return tokens[1:-1]
        return None

    def placeholder(self, t, message):
        self.report(t, message)
        return {"type": "unsupported", "message": message, "location": f"{Path(t.file).name}:{t.line}"}

    def image_placeholder(self, t, message):
        self.report(t, message)
        return {"type": "missing-image", "message": message, "location": f"{Path(t.file).name}:{t.line}"}

    def component(self, declaration, args, parent_names, stack):
        if declaration.name in stack:
            return self.placeholder(declaration.token, "Recursive view expansion: " + " → ".join(stack + (declaration.name,)))
        self.counter += 1
        prefix = str(self.counter)
        names, states = {
            name: {"op": "functionRef", "name": name, "params": function["params"]}
            for name, function in self.global_functions.items()
        }, []
        names.update({name: {"op": "firebaseRef", "name": name, "params": params}
                      for name, params in FIREBASE_BUILTINS.items()})
        supplied = dict(args)
        expected = {p[0] for p in declaration.properties if p[1] != "State"}
        for name, _ in args:
            if name not in expected:
                self.report(declaration.token, f"Unsupported initializer argument {name!r} for {declaration.name}")
        for name, attribute, default, token in declaration.properties:
            if attribute == "State":
                key = prefix + ":" + name
                # State belongs to the browser only when its initial value is
                # a supported literal. Never evaluate Swift initializer code:
                # it may perform persistence, network, or other side effects.
                try:
                    value = Expr.parse(default, {})
                except (ValueError, IndexError):
                    value = None
                if value is not None and value.get("op") == "unary" and value.get("kind") == "-" and value["a"].get("op") == "literal" and type(value["a"].get("value")) in (int, float):
                    value = {"op": "literal", "value": -value["a"]["value"]}
                if value is None or not self.literal_state_value(value):
                    initializer = values(default)
                    if "readFirebase" in initializer:
                        message = f"@State {name} calls readFirebase; persistence and external Swift code are not translated or executed, so browser state starts empty"
                    else:
                        message = f"@State {name} has a complex initializer that is not translated or executed, so browser state starts empty"
                    self.report(token, message)
                    value = {"op": "literal", "value": None}
                states.append({"key": key, "value": value})
                self.state_initials[key] = value
                names[name] = {"op": "state", "key": key}
                names["$" + name] = names[name]
            elif attribute == "Binding":
                if name not in supplied:
                    self.report(token, f"Missing value for @Binding {name}")
                    names[name] = {"op": "literal", "value": None}
                else:
                    binding = self.expression(supplied[name], parent_names)
                    reference = self.binding_reference(binding)
                    if not reference:
                        self.report(token, f"@Binding {name} requires a writable @State field binding")
                        binding = {"op": "literal", "value": None}
                    else:
                        binding = ({"op": "state", "key": reference["key"]}
                                   if not reference["path"] else {"op": "binding", **reference})
                    names[name] = binding
                names["$" + name] = names[name]
            elif attribute:
                self.report(token, f"@{attribute} {name} is unsupported")
                names[name] = {"op": "literal", "value": None}
            elif name in supplied:
                names[name] = self.expression(supplied[name], parent_names)
            elif default:
                names[name] = self.expression(default, names)
            else:
                self.report(token, f"Missing value for view property {name}")
                names[name] = {"op": "literal", "value": None}
        for name in declaration.computed_values:
            names[name] = {"op": "computed", "key": prefix + ":" + name}
        for name, method in declaration.helper_methods.items():
            names[name] = {"op": "componentMethodRef", "key": prefix + ":" + name,
                           "params": method["params"]}
        for name, method in declaration.helper_methods.items():
            key = prefix + ":" + name
            method_names = dict(names)
            method_names.update({parameter["name"]: {"op": "local", "key": parameter["name"]}
                                 for parameter in method["params"]})
            try:
                self.component_methods[key] = {"params": method["params"],
                                               "body": MethodParser(method["body"], names=method_names).parse()}
            except (ValueError, IndexError) as e:
                self.report(method["token"], f"Helper method {declaration.name}.{name} is unsupported: {e}")
        for name, tokens in declaration.computed_values.items():
            key = prefix + ":" + name
            try:
                self.computed_properties[key] = MethodParser(tokens, names=dict(names)).parse()
            except (ValueError, IndexError) as e:
                self.report(declaration.token, f"Computed property {declaration.name}.{name} is unsupported: {e}")
        parser = ViewParser(self, declaration.body, names, stack + (declaration.name,),
                            declaration.computed_views, declaration.view_methods)
        return {"type": "component", "states": states, "children": parser.parse()}

    def actions(self, tokens, names):
        result, i = [], 0
        while i < len(tokens):
            if tokens[i].value == ";":
                i += 1
                continue
            start, i = i, i + 1
            while i < len(tokens):
                if tokens[i].value == ";" or tokens[i].line > tokens[i - 1].line:
                    break
                if tokens[i].value in ("(", "[", "{"):
                    i = matching(tokens, i) + 1
                else:
                    i += 1
            part = tokens[start:i]
            firebase_index = next((index for index, token in enumerate(part[:-1])
                                   if token.value == "writeFirebase" and part[index + 1].value == "("), None)
            if firebase_index is not None:
                if not (self.firebase_base_url and self.firebase_write_helper):
                    self.report(part[firebase_index], "writeFirebase action needs a literal Firebase databaseURL and the local REST helper")
                    continue
                try:
                    end = matching(part, firebase_index + 1)
                    arguments_map = dict(arguments(part[firebase_index + 2:end]))
                    if "path" not in arguments_map or "data" not in arguments_map:
                        raise ValueError("writeFirebase requires path: and data:")
                    result.append({
                        "kind": "firebaseWrite",
                        "path": Expr.parse(arguments_map["path"], names),
                        "value": Expr.parse(arguments_map["data"], names),
                    })
                    self.firebase_write_action = True
                except (ValueError, IndexError) as e:
                    self.report(part[firebase_index], "Unsupported writeFirebase action: " + str(e))
                continue
            if names.get(part[0].value, {}).get("op") == "functionRef":
                try:
                    expression = Expr.parse(part, names)
                    if expression.get("op") != "globalCall":
                        raise ValueError("expected a direct global function call")
                    function_name = expression["name"]
                    if function_name not in self.global_functions:
                        raise ValueError(f"global function {function_name} is not available")
                    calls_firebase = self.function_calls_firebase_write(function_name)
                    if calls_firebase and not (self.firebase_base_url and self.firebase_write_helper):
                        raise ValueError("Firebase-writing helper needs a literal databaseURL and local writeFirebase helper")
                    result.append({"kind": "functionCall", "name": function_name, "args": expression["args"]})
                    self.firebase_write_action = self.firebase_write_action or calls_firebase
                    continue
                except (ValueError, IndexError) as e:
                    self.report(part[0], f"Unsupported action: {e}")
                    continue
            if names.get(part[0].value, {}).get("op") == "componentMethodRef":
                try:
                    reference = names[part[0].value]
                    call_args = []
                    if len(part) > 1:
                        if part[1].value != "(" or matching(part, 1) != len(part) - 1:
                            raise ValueError("method callback needs a direct argument list")
                        call_args = [Expr.parse(argument, names) for label, argument in arguments(part[2:-1]) if label is None]
                    params = reference.get("params", [])
                    if len(call_args) > len(params) or any(index >= len(call_args) and "default" not in param for index, param in enumerate(params)):
                        raise ValueError(f"callback expects {len(params)} argument(s)")
                    provided = len(call_args)
                    call_args.extend(param["default"] for index, param in enumerate(params) if index >= provided)
                    result.append({"kind": "componentMethodCall", "key": reference["key"], "args": call_args})
                except (ValueError, IndexError) as e:
                    self.report(part[0], f"Unsupported view method action: {e}")
                continue
            if names.get(part[0].value, {}).get("op") == "closure":
                try:
                    if len(part) < 3 or part[1].value != "(" or matching(part, 1) != len(part) - 1:
                        raise ValueError("callback needs a direct argument list")
                    callback_args = [Expr.parse(arg, names) for label, arg in arguments(part[2:-1]) if label is None]
                    result.append({"kind": "closureCall", "name": part[0].value, "args": callback_args})
                except (ValueError, IndexError) as e:
                    self.report(part[0], f"Unsupported callback action: {e}")
                continue
            name = part[0].value
            state = names.get(name, {})
            try:
                call = Expr.parse(part, names)
                if call.get("op") == "call" and call.get("name") in ("append", "removeAll"):
                    reference = self.binding_reference(call.get("a"))
                    if reference and (call["name"] == "removeAll" or len(call.get("args", [])) == 1):
                        result.append({"kind": "collection", "binding": {"op": "binding", **reference},
                                       "method": call["name"], "args": call.get("args", [])})
                        continue
            except (ValueError, IndexError):
                pass
            if state.get("op") != "state":
                self.report(part[0], "Unsupported action; only assignments and toggle() on @State are translated")
            elif values(part[1:]) == [".", "toggle", "(", ")"]:
                result.append({"key": state["key"], "kind": "toggle"})
            elif len(part) >= 3 and part[1].value in ("=", "+=", "-="):
                try:
                    expression = Expr.parse(part[2:], names)
                    result.append({"key": state["key"], "kind": part[1].value, "value": expression})
                except (ValueError, IndexError) as e:
                    self.report(part[0], "Unsupported action: " + str(e))
            else:
                self.report(part[0], "Unsupported state action")
        return result

    def function_calls_firebase_write(self, name, visited=None):
        visited = set() if visited is None else visited
        if name in visited:
            return False
        visited.add(name)
        function = self.global_functions.get(name)
        if not function:
            return False

        def contains(value):
            if isinstance(value, dict):
                if value.get("kind") == "firebaseWrite":
                    return True
                if value.get("op") == "globalCall" and self.function_calls_firebase_write(value.get("name", ""), visited):
                    return True
                return any(contains(child) for child in value.values())
            if isinstance(value, list):
                return any(contains(child) for child in value)
            return False

        return contains(function.get("body", []))


class ViewParser:
    def __init__(self, compiler, tokens, names, stack, computed_views=None, view_methods=None):
        self.c, self.ts, self.names, self.stack, self.i = compiler, tokens, dict(names), stack, 0
        self.computed_views = computed_views or {}
        self.view_methods = view_methods or {}

    def parse(self):
        nodes = []
        while self.i < len(self.ts):
            if self.ts[self.i].value in (";", "return"):
                self.i += 1
                continue
            before = self.i
            try:
                nodes.append(self.node())
            except (ValueError, IndexError) as e:
                nodes.append(self.c.placeholder(self.ts[before], "Unsupported view syntax: " + str(e)))
                # Consume the malformed statement without interpreting its children as siblings.
                while self.i < len(self.ts):
                    if self.i > before and self.ts[self.i].line > self.ts[self.i - 1].line:
                        break
                    if self.ts[self.i].value in ("{", "(", "["):
                        self.i = matching(self.ts, self.i) + 1
                    else:
                        self.i += 1
            if self.i <= before:
                self.i = before + 1
        return nodes

    def block(self):
        if self.i >= len(self.ts) or self.ts[self.i].value != "{":
            raise ValueError("expected view closure")
        end = matching(self.ts, self.i)
        ts = self.ts[self.i + 1:end]
        self.i = end + 1
        return ts

    def subviews(self, tokens, names=None):
        return ViewParser(self.c, tokens, self.names if names is None else names, self.stack,
                          self.computed_views, self.view_methods).parse()

    def expr(self, tokens, fallback=None):
        return self.c.expression(tokens, self.names, fallback)

    def condition(self, tokens):
        names = dict(self.names)
        bindings, tests = [], []
        for part in split_top(tokens):
            if part and part[0].value in ("let", "var"):
                if len(part) == 2:
                    name = part[1].value
                    binding = self.c.expression([part[1]], names)
                elif len(part) >= 4 and part[2].value == "=":
                    name = part[1].value
                    binding = self.c.expression(part[3:], names)
                else:
                    raise ValueError("if binding needs `if let name` or `if let name = expression`")
                bindings.append({"name": name, "value": binding})
                names[name] = {"op": "local", "key": name}
            else:
                tests.append(self.c.expression(part, names))
        test = {"op": "literal", "value": True}
        for expression in tests:
            test = expression if test.get("value") is True else {"op": "binary", "kind": "&&", "a": test, "b": expression}
        return bindings, test, names

    def presentation_binding(self, tokens):
        if tokens and tokens[0].value == "Binding" and len(tokens) > 2 and tokens[1].value == "(":
            end = matching(tokens, 1)
            parts = dict(arguments(tokens[2:end]))
            getter = self.c.closure_body(parts.get("get", []))
            if getter is None:
                return None, "truthy"
            expression = self.c.expression(getter, self.names)
            mode = "truthy"
            target = expression
            if expression.get("op") == "binary" and expression.get("kind") in ("!=", "=="):
                left, right = expression.get("a"), expression.get("b")
                if right == {"op": "literal", "value": None}:
                    target = left
                    mode = "nonNil" if expression["kind"] == "!=" else "nil"
                elif left == {"op": "literal", "value": None}:
                    target = right
                    mode = "nonNil" if expression["kind"] == "!=" else "nil"
            return self.c.binding_reference(target), mode
        try:
            expression = self.c.expression(tokens, self.names)
        except (ValueError, IndexError):
            return None, "truthy"
        return self.c.binding_reference(expression), "nonNil"

    def node(self):
        t = self.ts[self.i]
        self.i += 1
        if t.value in self.computed_views:
            if t.value in self.stack:
                return self.c.placeholder(t, "Recursive view expansion: " + " → ".join(self.stack + (t.value,)))
            content = ViewParser(self.c, self.computed_views[t.value], self.names,
                                 self.stack + (t.value,), self.computed_views, self.view_methods).parse()
            return self.modifiers({"type": "Group", "children": content})
        if t.value == "switch":
            start = self.i
            while self.i < len(self.ts) and self.ts[self.i].value != "{":
                if self.ts[self.i].value in ("(", "["):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("switch expression has no cases")
            expression = self.c.expression(self.ts[start:self.i], self.names)
            opening = self.i
            ending = matching(self.ts, opening)
            body = self.ts[opening + 1:ending]
            case_starts = []
            index = 0
            while index < len(body):
                token = body[index]
                if token.value in ("(", "[", "{"):
                    index = matching(body, index) + 1
                    continue
                if token.value in ("case", "default"):
                    case_starts.append(index)
                index += 1
            cases = []
            for case_index, case_start in enumerate(case_starts):
                next_start = case_starts[case_index + 1] if case_index + 1 < len(case_starts) else len(body)
                colon = None
                cursor = case_start + 1
                while cursor < next_start:
                    if body[cursor].value in ("(", "[", "{"):
                        cursor = matching(body, cursor) + 1
                    elif body[cursor].value == ":":
                        colon = cursor
                        break
                    else:
                        cursor += 1
                if colon is None:
                    raise ValueError("switch case needs a colon")
                is_default = body[case_start].value == "default"
                patterns = [] if is_default else split_top(body[case_start + 1:colon])
                pattern_values, bindings = [], []
                case_names = dict(self.names)
                for pattern in patterns:
                    if (len(pattern) >= 6 and pattern[0].value == "." and pattern[2].value == "("
                            and pattern[-1].value == ")" and matching(pattern, 2) == len(pattern) - 1
                            and all(part and part[0].value in ("let", "var") and len(part) == 2
                                    for part in split_top(pattern[3:-1]))):
                        case_name = pattern[1].value
                        pattern_values.append({"op": "enumCase", "name": case_name, "args": []})
                        for index, part in enumerate(split_top(pattern[3:-1])):
                            variable = part[1].value
                            bindings.append({"name": variable, "index": index})
                            case_names[variable] = {"op": "local", "key": variable}
                    elif not pattern or pattern[0].value in ("let", "var"):
                        self.c.report(body[case_start], "Switch case pattern is unsupported")
                    else:
                        pattern_values.append(self.c.expression(pattern, self.names))
                children = self.subviews(body[colon + 1:next_start], case_names)
                cases.append({"default": is_default, "values": pattern_values, "bindings": bindings, "children": children})
            self.i = ending + 1
            return self.modifiers({"type": "switch", "expression": expression, "cases": cases})
        if t.value == "if":
            start = self.i
            while self.i < len(self.ts) and self.ts[self.i].value != "{":
                if self.ts[self.i].value in ("(", "[", "{"):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            bindings, test, branch_names = self.condition(self.ts[start:self.i])
            yes = self.subviews(self.block(), branch_names)
            no = []
            if self.i < len(self.ts) and self.ts[self.i].value == "else":
                self.i += 1
                if self.i < len(self.ts) and self.ts[self.i].value == "if":
                    no = [self.node()]
                else:
                    no = self.subviews(self.block())
            return self.modifiers({"type": "if", "test": test, "bindings": bindings, "yes": yes, "no": no})
        if t.value in ("let", "var"):
            if self.i >= len(self.ts):
                return self.c.placeholder(t, "View local declaration is incomplete")
            name = self.ts[self.i].value
            self.i += 1
            while self.i < len(self.ts) and self.ts[self.i].value != "=":
                if self.ts[self.i].line > self.ts[self.i - 1].line and self.ts[self.i].value in ("Text", "if", "let", "var"):
                    return self.c.placeholder(t, f"View local {name} needs an initializer")
                self.i += 1
            if self.i >= len(self.ts):
                return self.c.placeholder(t, f"View local {name} needs an initializer")
            self.i += 1
            start = self.i
            while self.i < len(self.ts):
                if self.i > start and self.ts[self.i].line > self.ts[self.i - 1].line and self.ts[self.i].value not in (".", "+", "?", ":"):
                    break
                if self.ts[self.i].value in ("(", "[", "{"):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            expression = self.expr(self.ts[start:self.i])
            self.names[name] = {"op": "local", "key": name}
            return {"type": "let", "name": name, "value": expression}
        name = t.value
        args, closure, label = [], None, None
        if self.i < len(self.ts) and self.ts[self.i].value == "(":
            end = matching(self.ts, self.i)
            args = arguments(self.ts[self.i + 1:end])
            self.i = end + 1
        trailing_closures = []
        if self.i < len(self.ts) and self.ts[self.i].value == "{":
            closure = self.block()
            trailing_closures.append((None, closure))
        while (self.i + 2 < len(self.ts)
               and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.ts[self.i].value)
               and self.ts[self.i + 1].value == ":" and self.ts[self.i + 2].value == "{"):
            label_name = self.ts[self.i].value
            self.i += 2
            trailing_closures.append((label_name, self.block()))
            if label_name == "label":
                label = trailing_closures[-1][1]
        if label is not None and not any(name == "label" for name, _ in trailing_closures):
            trailing_closures.append(("label", label))
        argmap = dict(args)
        closure_args = dict(trailing_closures)
        positional = [v for k, v in args if k is None]
        first = positional[0] if positional else []
        recognized = {
            "Text": {None}, "Image": {None, "systemName"}, "VStack": {"alignment", "spacing"},
            "HStack": {"alignment", "spacing"}, "ZStack": {"alignment"}, "ScrollView": {None, "showsIndicators"},
            "Button": {None, "action", "label", "role"}, "TextField": {None, "text"}, "SecureField": {None, "text"},
            "Toggle": {None, "isOn"}, "NavigationLink": {None, "destination", "label"},
            "List": {None, "rowContent"},
            "ForEach": {None, "id"}, "Spacer": {"minLength"}, "Divider": set(),
            "NavigationStack": {"path"}, "NavigationView": set(), "Group": set(), "GroupBox": {None, "label"},
            "WindowGroup": {None}, "EmptyView": set(), "Circle": set(),
            "Form": set(), "LazyVGrid": {"columns", "alignment", "spacing"}, "LazyHGrid": {"rows", "alignment", "spacing"},
            "ScrollViewReader": set(), "TimelineView": {None}, "Picker": {None, "selection"},
            "Stepper": {None, "value", "in", "step"}, "Label": {None, "systemImage"},
            "ContentUnavailableView": {None, "systemImage"},
            "Section": {None, "header", "footer"}, "LabeledContent": {None, "value"},
            "DatePicker": {None, "selection", "displayedComponents"},
        }
        if name in recognized:
            for key, _ in args:
                if key not in recognized[name] or key == "showsIndicators":
                    self.c.report(t, f"Unsupported {name} argument: {key}")
            if len(positional) > 1:
                self.c.report(t, f"Additional positional arguments for {name} are not translated")
            if name == "WindowGroup" and positional:
                self.c.report(t, "WindowGroup window titles are not translated")
        if name in self.view_methods:
            function = self.view_methods[name]
            param_names = dict(self.names)
            positional_tokens = [part for key, part in args if key is None]
            named_tokens = {key: part for key, part in args if key is not None}
            for index, parameter in enumerate(function["params"]):
                key = parameter["name"]
                label_name = parameter["label"]
                supplied = named_tokens.get(label_name)
                if supplied is None and label_name in closure_args:
                    body = closure_args[label_name]
                    supplied = [Token("{", t.file, t.line), *body, Token("}", t.file, t.line)]
                if supplied is None and label_name == "_" and index < len(positional_tokens):
                    supplied = positional_tokens[index]
                if supplied is None and label_name != "_" and index < len(positional_tokens):
                    supplied = positional_tokens[index]
                if supplied is not None:
                    param_names[key] = self.c.expression(supplied, self.names)
            child_parser = ViewParser(self.c, function["body"], param_names,
                                      self.stack + (name,), self.computed_views, self.view_methods)
            node = {"type": "Group", "children": child_parser.parse()}
        elif name in self.c.declarations:
            if trailing_closures:
                component_args = list(args)
                closure_properties = self.c.declarations[name].closure_properties
                positional_closures = [body for label_name, body in trailing_closures if label_name is None]
                for index, property_name in enumerate(closure_properties):
                    body = closure_args.get(property_name)
                    if body is None and index < len(positional_closures):
                        body = positional_closures[index]
                    if body is not None and property_name not in argmap:
                        closure_tokens = [Token("{", t.file, t.line), *body, Token("}", t.file, t.line)]
                        component_args.append((property_name, closure_tokens))
                for closure_name, body in trailing_closures:
                    if closure_name and closure_name != "label" and closure_name not in closure_properties:
                        self.c.report(t, f"Unknown trailing closure {closure_name} for {name}")
                node = self.c.component(self.c.declarations[name], component_args, self.names, self.stack)
            else:
                node = self.c.component(self.c.declarations[name], args, self.names, self.stack)
        elif name in ("VStack", "HStack", "ZStack", "ScrollView", "Group", "GroupBox", "NavigationStack", "NavigationView", "WindowGroup", "Form", "LazyVGrid", "LazyHGrid", "ScrollViewReader", "TimelineView"):
            body = closure or []
            reader_param = None
            if name in ("ScrollViewReader", "TimelineView") and body:
                in_index = top_level_token(body, "in")
                if in_index is not None:
                    header = body[:in_index]
                    reader_param = next((token.value for token in header if re.fullmatch(r"[A-Za-z_$][\w$]*", token.value)), None)
                    body = body[in_index + 1:]
                    if name == "ScrollViewReader":
                        self.c.report(t, "ScrollViewReader content is rendered; programmatic scrollTo calls are skipped")
                    else:
                        self.c.report(t, "TimelineView renders once; scheduled timeline refreshes are skipped")
            node = {"type": name, "children": self.subviews(body)}
            if name == "NavigationStack" and "path" in argmap:
                path_tokens = argmap["path"]
                path_expr = self.expr(path_tokens[1:] if path_tokens and path_tokens[0].value == "$" else path_tokens)
                reference = self.c.binding_reference(path_expr)
                if reference:
                    node["path"] = {"op": "binding", **reference}
                else:
                    self.c.report(t, "NavigationStack path needs a writable @State binding")
            if name == "GroupBox":
                box_label = argmap.get("label")
                if box_label is not None:
                    label_body = self.c.closure_body(box_label)
                    node["label"] = self.subviews(label_body if label_body is not None else box_label)
                elif first:
                    node["label"] = [{"type": "Text", "text": self.expr(first)}]
            if "spacing" in argmap:
                node["gap"] = self.expr(argmap["spacing"], 8)
            if "alignment" in argmap:
                alignment = "".join(values(argmap["alignment"]))
                accepted = {".leading": "start", ".trailing": "end", ".center": "center", ".top": "start", ".bottom": "end"}
                if alignment in accepted:
                    node["align"] = accepted[alignment]
                else:
                    self.c.report(t, f"Unsupported alignment: {alignment}")
            if name == "ScrollView" and first:
                axis = "".join(values(first))
                if axis == ".horizontal":
                    node["axis"] = "horizontal"
                elif axis != ".vertical":
                    self.c.report(t, f"Unsupported scroll axes: {axis}")
        elif name == "Text":
            if not first:
                node = self.c.placeholder(t, "Text requires a supported string argument")
            elif any(re.search(r"\\\(\s*\)", token.value) for token in first if token.value.startswith('"')):
                node = self.c.placeholder(t, "Text contains an empty string interpolation")
            else:
                try:
                    text = Expr.parse(first, self.names)
                    node = {"type": "Text", "text": text}
                except (ValueError, IndexError) as e:
                    node = self.c.placeholder(t, "Unsupported Text expression: " + str(e))
        elif name == "Image":
            if "systemName" in argmap:
                symbol = self.expr(argmap["systemName"])
                svg = SYSTEM_SYMBOLS.get(symbol.get("value")) if symbol.get("op") == "literal" else None
                if svg:
                    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
                    node = {"type": "Image", "src": uri, "alt": symbol}
                else:
                    if symbol.get("op") != "literal" or symbol.get("value") not in {
                            "gear", "gearshape", "plus", "trash", "circle", "checkmark", "checkmark.circle",
                            "checkmark.circle.fill", "chevron.right", "chevron.left", "person.fill", "pencil",
                            "xmark", "arrow.up", "arrow.down", "star.fill", "exclamationmark.triangle.fill", "clock.fill"}:
                        self.c.report(t, "Unsupported system symbol is rendered as a generic SF-symbol glyph in the browser")
                    node = {"type": "Symbol", "symbolName": symbol}
            else:
                image = self.expr(first)
                uri = self.c.assets.get(image.get("value")) if image.get("op") == "literal" else None
                if uri:
                    node = {"type": "Image", "src": uri, "alt": image}
                elif image.get("op") in ("concat", "local"):
                    self.c.needs_asset_lookup = True
                    candidates = self.c.static_string_values(image)
                    missing = sorted({candidate for candidate in candidates or [] if not self.c.assets.get(candidate)})
                    if missing:
                        self.c.report(t, "Missing dynamic image asset(s): " + ", ".join(missing))
                    elif candidates is None:
                        self.c.report(t, "Dynamic image names are resolved from selected-target assets; unmatched names are hidden")
                    node = {"type": "Image", "assetName": image, "alt": image}
                else:
                    node = self.c.image_placeholder(t, "Missing, ambiguous, or dynamic image asset: " + str(image.get("value", "expression")))
        elif name in ("Spacer", "Divider", "EmptyView", "Circle"):
            node = {"type": name}
            if "minLength" in argmap:
                node["minLength"] = self.expr(argmap["minLength"], 0)
        elif name == "Button":
            action = argmap.get("action")
            named_label = argmap.get("label")
            action_body = action[1:-1] if action and action[0].value == "{" and matching(action, 0) == len(action) - 1 else None
            named_label_body = named_label[1:-1] if named_label and named_label[0].value == "{" and matching(named_label, 0) == len(named_label) - 1 else None
            if action:
                if action_body is None:
                    actions = self.c.actions(action, self.names)
                else:
                    actions = self.c.actions(action_body, self.names)
                label_tokens = named_label_body if named_label_body is not None else closure if closure is not None else label
                if named_label is not None and named_label_body is None:
                    self.c.report(t, "Button label must be an inline closure")
                children = self.subviews(label_tokens or []) if label_tokens is not None else [{"type": "Text", "text": self.expr(first, "Button")}]
            else:
                actions = self.c.actions(closure or [], self.names)
                if named_label is not None and named_label_body is not None:
                    children = self.subviews(named_label_body)
                else:
                    children = self.subviews(label) if label is not None else [{"type": "Text", "text": self.expr(first, "Button")}]
            node = {"type": "Button", "actions": actions, "children": children}
            if "role" in argmap:
                node["role"] = "destructive" if values(argmap["role"]) == [".", "destructive"] else text(argmap["role"])
        elif name in ("TextField", "SecureField", "Toggle"):
            binding = argmap.get("isOn" if name == "Toggle" else "text", [])
            reference, _ = self.presentation_binding(binding)
            if not reference:
                node = self.c.placeholder(t, f"{name} requires a writable @State binding")
            else:
                node = {"type": name, "key": reference["key"], "binding": {"op": "binding", **reference}, "label": self.expr(first, ""), "children": self.subviews(closure or [])}
        elif name == "Picker":
            binding_tokens = argmap.get("selection", [])
            reference, _ = self.presentation_binding(binding_tokens)
            if not reference:
                node = self.c.placeholder(t, "Picker requires a writable @State selection binding")
            else:
                node = {"type": name, "key": reference["key"], "binding": {"op": "binding", **reference}, "label": self.expr(first, ""), "children": self.subviews(closure or [])}
        elif name == "Stepper":
            binding_tokens = argmap.get("value", [])
            reference, _ = self.presentation_binding(binding_tokens)
            range_tokens = argmap.get("in", [])
            if not reference or not range_tokens:
                node = self.c.placeholder(t, "Stepper requires a writable value binding and a closed range")
            else:
                range_value = self.expr(range_tokens)
                if range_value.get("op") != "binary" or range_value.get("kind") not in ("...", "..<"):
                    node = self.c.placeholder(t, "Stepper range must use a numeric closed or half-open range")
                else:
                    node = {"type": name, "key": reference["key"], "binding": {"op": "binding", **reference}, "label": self.expr(first, ""),
                            "min": range_value["a"], "max": range_value["b"],
                            "inclusiveMax": range_value["kind"] == "...",
                            "step": self.expr(argmap["step"]) if "step" in argmap else {"op": "literal", "value": 1}}
        elif name == "Label":
            label_text = argmap.get(None, first)
            symbol_tokens = argmap.get("systemImage", [])
            text_node = {"type": "Text", "text": self.expr(label_text, "")}
            symbol = self.expr(symbol_tokens, "") if symbol_tokens else None
            node = {"type": "Label", "children": [text_node], "symbol": symbol}
        elif name == "ContentUnavailableView":
            node = {"type": name, "title": self.expr(first, ""),
                    "symbol": self.expr(argmap["systemImage"]) if "systemImage" in argmap else None}
        elif name == "Section":
            content = closure or []
            header = closure_args.get("header")
            footer = closure_args.get("footer")
            if header:
                header_view = self.subviews(header)
            elif first:
                header_view = [{"type": "Text", "text": self.expr(first)}]
            else:
                header_view = []
            node = {"type": name, "children": self.subviews(content), "header": header_view,
                    "footer": self.subviews(footer or [])}
        elif name == "LabeledContent":
            node = {"type": name, "label": self.expr(first, ""),
                    "value": self.expr(argmap["value"]) if "value" in argmap else {"op": "literal", "value": ""}}
        elif name == "DatePicker":
            binding_tokens = argmap.get("selection", [])
            reference, _ = self.presentation_binding(binding_tokens)
            if not reference:
                node = self.c.placeholder(t, "DatePicker requires a writable @State date binding")
            else:
                node = {"type": name, "key": reference["key"], "binding": {"op": "binding", **reference},
                        "label": self.expr(first, "")}
        elif name == "NavigationLink":
            destination = argmap.get("destination")
            named_label = argmap.get("label")
            label_body = self.c.closure_body(named_label) if named_label is not None else None
            destination_body = self.c.closure_body(destination) if destination is not None else None
            if named_label is not None:
                children = self.subviews(label_body if label_body is not None else named_label)
            elif label is not None:
                children = self.subviews(label)
            elif closure is not None and destination is not None:
                children = self.subviews(closure)
            elif first:
                children = [{"type": "Text", "text": self.expr(first, "Open")}]
            else:
                children = [{"type": "Text", "text": {"op": "literal", "value": "Open"}}]
            if destination is not None:
                dest = self.subviews(destination_body if destination_body is not None else destination)
            else:
                dest = self.subviews(closure or [])
            node = {"type": name, "children": children, "destination": dest}
        elif name == "List":
            items = self.expr(first, [])
            row = argmap.get("rowContent")
            row_body = self.c.closure_body(row) if row is not None else closure
            parameter, body = "$0", row_body or []
            if body:
                in_index = top_level_token(body, "in")
                if in_index is not None:
                    header = body[:in_index]
                    parameter = next((token.value for token in header if re.fullmatch(r"[A-Za-z_$][\w$]*", token.value)), "$0")
                    body = body[in_index + 1:]
            self.c.counter += 1
            loop = "list" + str(self.c.counter)
            row_names = dict(self.names)
            row_names[parameter] = {"op": "local", "key": loop}
            node = {"type": name, "items": items, "key": loop, "children": self.subviews(body, row_names)}
        elif name == "ForEach":
            if not first or closure is None:
                node = self.c.placeholder(t, "ForEach is incomplete; it needs a collection and a content closure")
                return self.modifiers(node)
            items = self.expr(first, [])
            literal_items = items.get("op") == "array" and all(
                x.get("op") == "literal" and isinstance(x.get("value"), (str, int, float, bool))
                for x in items.get("items", [])
            )
            dynamic_items = items.get("op") in ("state", "local", "prefix", "computed", "call", "sorted", "array", "member", "binary")
            if not (literal_items or dynamic_items):
                node = self.c.placeholder(t, "ForEach requires a literal or stored primitive array")
            else:
                content = closure or []
                in_index = None
                cursor = 0
                while cursor < len(content):
                    if content[cursor].value in ("(", "[", "{"):
                        cursor = matching(content, cursor) + 1
                    elif content[cursor].value == "in":
                        in_index = cursor
                        break
                    else:
                        cursor += 1
                if in_index is not None:
                    parameter_tokens, body = content[:in_index], content[in_index + 1:]
                    parameters = [token.value for token in parameter_tokens if re.fullmatch(r"[A-Za-z_$][\w$]*", token.value)]
                else:
                    parameters, body = ["$0"], content
                self.c.counter += 1
                loop = "loop" + str(self.c.counter)
                names = dict(self.names)
                candidates = self.c.static_array_values(items)
                for parameter in parameters:
                    names[parameter] = {"op": "local", "key": loop if len(parameters) == 1 else parameter, "candidates": candidates}
                if len(parameters) == 1:
                    names[parameters[0]] = {"op": "local", "key": loop, "candidates": candidates}
                names["$0"] = {"op": "local", "key": loop, "candidates": candidates}
                if "id" in argmap and values(argmap["id"]) != ["\\", ".", "self"]:
                    self.c.report(t, "ForEach supports id: \\.self only; index identity is used")
                node = {"type": name, "items": items, "key": loop, "params": parameters,
                        "children": self.subviews(body, names)}
        else:
            node = self.c.placeholder(t, f"Unsupported view: {name}")
        return self.modifiers(node)

    def modifiers(self, node):
        mods = []
        while self.i < len(self.ts) and self.ts[self.i].value == ".":
            self.i += 1
            if self.i >= len(self.ts):
                break
            t = self.ts[self.i]
            name = t.value
            self.i += 1
            args = []
            if self.i < len(self.ts) and self.ts[self.i].value == "(":
                end = matching(self.ts, self.i)
                args = arguments(self.ts[self.i + 1:end])
                self.i = end + 1
            trailing_closures = []
            if self.i < len(self.ts) and self.ts[self.i].value == "{":
                trailing_closures.append((None, self.block()))
            while (self.i + 2 < len(self.ts)
                   and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.ts[self.i].value)
                   and self.ts[self.i + 1].value == ":" and self.ts[self.i + 2].value == "{"):
                label_name = self.ts[self.i].value
                self.i += 2
                trailing_closures.append((label_name, self.block()))
            closure = trailing_closures[0][1] if trailing_closures else None
            try:
                mod = self.modifier(t, name, args, closure, dict(trailing_closures))
                if mod:
                    if mod.get("name") == "navigationDestination":
                        node["destination"] = mod["content"]
                        node["destinationParam"] = mod["param"]
                    else:
                        mods.append(mod)
            except (ValueError, IndexError) as e:
                self.c.report(t, f"Unsupported .{name}: {e}")
        if mods:
            node["modifiers"] = mods
        return node

    def modifier(self, t, name, args, closure, trailing_closures=None):
        trailing_closures = trailing_closures or {}
        pos = [v for k, v in args if k is None]
        named = {k: v for k, v in args if k is not None}
        first = pos[0] if pos else []
        expr = lambda ts: Expr.parse(ts, self.names)
        text = lambda ts: "".join(values(ts))
        if name == "task":
            if self.c.firebase_base_url and self.c.firebase_read_calls:
                self.c.firebase_requested = True
                return None
            self.c.report(t, "SwiftUI .task work is skipped; async lifecycle work and network requests are not run")
            return None
        if name == "navigationDestination":
            if closure is None:
                raise ValueError("navigationDestination requires a destination closure")
            body = closure
            in_index = top_level_token(body, "in")
            if in_index is None:
                raise ValueError("destination closure needs a route parameter")
            param = next((token.value for token in body[:in_index]
                          if re.fullmatch(r"[A-Za-z_$][\w$]*", token.value)), None)
            if not param:
                raise ValueError("destination closure needs a route parameter")
            names = dict(self.names)
            names[param] = {"op": "local", "key": param}
            return {"name": name, "param": param, "content": self.subviews(body[in_index + 1:], names)}
        if name in ("sheet", "confirmationDialog"):
            binding_label = "isPresented" if "isPresented" in named else "item"
            binding_tokens = named.get(binding_label, [])
            binding_reference, show_mode = self.presentation_binding(binding_tokens)
            if binding_label == "item":
                show_mode = "nonNil"
            elif binding_tokens and binding_tokens[0].value != "Binding":
                show_mode = "truthy"
            if not binding_reference:
                raise ValueError(f"{name} requires a writable @State presentation binding")
            if closure is None:
                raise ValueError(f"{name} requires a content closure")
            content_body = closure
            content_param = None
            in_index = top_level_token(content_body, "in")
            if in_index is not None:
                content_param = next((token.value for token in content_body[:in_index]
                                      if re.fullmatch(r"[A-Za-z_$][\w$]*", token.value)), None)
                content_names = dict(self.names)
                if content_param:
                    content_names[content_param] = {"op": "local", "key": content_param}
                content_body = content_body[in_index + 1:]
            else:
                content_names = self.names
            result = {"name": name, "binding": {"op": "binding", **binding_reference},
                      "showMode": show_mode, "param": content_param,
                      "content": self.subviews(content_body, content_names)}
            if name == "confirmationDialog":
                result["title"] = expr(pos[0]) if pos else {"op": "literal", "value": ""}
                button_body = trailing_closures.get(None, closure)
                message_body = trailing_closures.get("message", [])
                result["buttons"] = self.subviews(button_body or [])
                result["message"] = self.subviews(message_body)
            return result
        if name == "refreshable":
            self.c.report(t, "Pull-to-refresh closure is skipped; screen content remains available")
            return None
        if closure is not None:
            raise ValueError("modifier closures are unsupported")
        if name == "tag" and len(pos) == 1 and not named:
            return {"name": name, "value": expr(first)}
        if name in ("lineLimit", "minimumScaleFactor", "strikethrough", "accessibilityLabel", "buttonStyle",
                    "pickerStyle", "textFieldStyle", "textInputAutocapitalization", "autocorrectionDisabled",
                    "navigationBarTitleDisplayMode", "contentShape"):
            if name == "strikethrough":
                return {"name": name, "value": expr(first) if first else {"op": "literal", "value": True}}
            if name == "accessibilityLabel" and first:
                return {"name": name, "value": expr(first)}
            if name == "minimumScaleFactor" and first:
                return {"name": name, "value": expr(first)}
            if name == "lineLimit" and first and first[0].value not in (".",):
                return {"name": name, "value": expr(first)}
            if args:
                return {"name": name, "value": {"op": "literal", "value": text(first)}}
            return {"name": name}
        if name == "padding":
            if named or len(pos) > 2:
                raise ValueError("expected edges and/or length")
            edge = "all"
            amount = {"op": "literal", "value": 16}
            if first:
                if first[0].value == ".":
                    edge = text(first)[1:]
                    if len(pos) > 1:
                        amount = expr(pos[1])
                else:
                    amount = expr(first)
            if edge not in ("all", "horizontal", "vertical", "top", "bottom", "leading", "trailing"):
                raise ValueError("unsupported edge set")
            return {"name": name, "edge": edge, "value": amount}
        if name == "frame":
            supported = {"width", "height", "minWidth", "maxWidth", "minHeight", "maxHeight", "alignment"}
            if pos or not named.keys() <= supported:
                raise ValueError("unsupported frame arguments")
            result = {"name": name, "values": {}}
            for key, ts in named.items():
                if key == "alignment":
                    a = text(ts)
                    if a not in (".leading", ".trailing", ".center", ".top", ".bottom"):
                        raise ValueError("unsupported frame alignment")
                    result["alignment"] = a[1:]
                else:
                    result["values"][key] = {"op": "literal", "value": "infinity"} if text(ts) == ".infinity" else expr(ts)
            return result
        if name == "background" and not args:
            return {"name": name, "value": "var(--swiftui-background-style, #f2f2f7)"}
        if name in ("foregroundColor", "foregroundStyle", "background", "backgroundStyle", "tint"):
            if len(pos) != 1 or (named and (name != "background" or not named.keys() <= {"in"})):
                raise ValueError("expected a named color")
            color_tokens = first[2:] if values(first[:2]) == ["Color", "."] else first
            color_value = expr(color_tokens)
            constant_colors = {"red": "#ff3b30", "orange": "#ff9500", "yellow": "#ffcc00", "green": "#34c759", "blue": "#007aff", "purple": "#af52de", "pink": "#ff2d55", "white": "#fff", "black": "#000", "gray": "#8e8e93", "clear": "transparent", "primary": "#000", "secondary": "#8e8e93", "tertiary": "#8e8e93", "quaternary": "#f2f2f7", "accentColor": "#007aff"}
            if color_value.get("op") == "enumCase" and color_value.get("name") in constant_colors:
                color_value = constant_colors[color_value["name"]]
            result = {"name": name, "value": color_value}
            if name == "background" and "in" in named:
                shape_tokens = named["in"]
                shape = text(shape_tokens)
                if shape == "Capsule()":
                    result["shape"] = "Capsule"
                elif (values(shape_tokens[:3]) == ["RoundedRectangle", "(", "cornerRadius"]
                      and shape_tokens[-1].value == ")"
                      and matching(shape_tokens, 1) == len(shape_tokens) - 1):
                    shape_args = dict(arguments(shape_tokens[2:-1]))
                    if set(shape_args) != {"cornerRadius"}:
                        raise ValueError("RoundedRectangle background needs cornerRadius")
                    result["shape"] = "RoundedRectangle"
                    result["radius"] = expr(shape_args["cornerRadius"])
                else:
                    raise ValueError("background shapes support Capsule() and RoundedRectangle(cornerRadius: …)")
            return result
        if name == "font":
            if len(pos) != 1 or named:
                raise ValueError("expected one font")
            raw = text(first)
            sizes = {".largeTitle": 34, ".title": 28, ".title2": 22, ".title3": 20, ".headline": 17, ".body": 17, ".callout": 16, ".subheadline": 15, ".footnote": 13, ".caption": 12, ".caption2": 11}
            if raw in sizes:
                return {"name": name, "value": {"op": "literal", "value": sizes[raw]}, "weight": "600" if raw == ".headline" else "400"}
            style = text(first[:2]) if len(first) == 6 and first[0].value == "." else ""
            if (len(first) == 6 and style in sizes and first[2].value == "."
                    and first[3].value == "bold" and values(first[4:]) == ["(", ")"]):
                return {"name": name, "value": {"op": "literal", "value": sizes[style]}, "weight": "700"}
            if values(first[:3]) == [".", "system", "("] and matching(first, 2) == len(first) - 1:
                inner = dict(arguments(first[3:-1]))
                if "size" in inner and inner.keys() <= {"size", "weight"}:
                    weight = self.weight(text(inner.get("weight", [])) or ".regular")
                    return {"name": name, "value": expr(inner["size"]), "weight": weight}
            raise ValueError("unsupported font")
        if name == "fontWeight":
            return {"name": name, "value": self.weight(text(first))}
        if name == "toolbar":
            if pos or not named.keys() <= {"content"}:
                raise ValueError("expected only a content closure")
            content = named.get("content")
            body = self.c.closure_body(content) if content is not None else closure
            if body is None:
                raise ValueError("expected a toolbar content closure")
            return {"name": name, "content": self.subviews(body)}
        if name in ("cornerRadius", "opacity"):
            if len(pos) != 1 or named:
                raise ValueError("expected one numeric expression")
            return {"name": name, "value": expr(first)}
        if name in ("bold", "italic", "resizable", "scaledToFit", "scaledToFill"):
            if args:
                raise ValueError("arguments are unsupported")
            return {"name": name}
        if name == "fixedSize":
            if pos or not named.keys() <= {"horizontal", "vertical"}:
                raise ValueError("expected optional horizontal and vertical Boolean values")
            horizontal = expr(named["horizontal"]) if "horizontal" in named else {"op": "literal", "value": True}
            vertical = expr(named["vertical"]) if "vertical" in named else {"op": "literal", "value": True}
            if any(value.get("op") != "literal" or type(value.get("value")) is not bool for value in (horizontal, vertical)):
                raise ValueError("fixedSize axes must be literal Booleans")
            return {"name": name, "horizontal": horizontal["value"], "vertical": vertical["value"]}
        if name == "navigationTitle" and len(pos) == 1 and not named:
            return {"name": name, "value": expr(first)}
        if name == "disabled" and len(pos) == 1 and not named:
            return {"name": name, "value": expr(first)}
        if name == "multilineTextAlignment" and len(pos) == 1 and not named:
            alignment = text(first)
            if alignment not in (".leading", ".center", ".trailing"):
                raise ValueError("expected .leading, .center, or .trailing")
            return {"name": name, "value": {".leading": "left", ".center": "center", ".trailing": "right"}[alignment]}
        if name == "listStyle" and len(pos) == 1 and not named:
            style = text(first)
            if style not in (".automatic", ".plain", ".inset", ".grouped", ".insetGrouped", ".sidebar"):
                raise ValueError("unsupported ListStyle")
            return {"name": name, "value": style[1:]}
        raise ValueError("modifier is not translated")

    @staticmethod
    def weight(raw):
        weights = {".ultraLight": "200", ".light": "300", ".regular": "400", ".medium": "500", ".semibold": "600", ".bold": "700", ".heavy": "800", ".black": "900"}
        if raw not in weights:
            raise ValueError("unsupported font weight")
        return weights[raw]
