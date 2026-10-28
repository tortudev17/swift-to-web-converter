"""Small, deliberately bounded Swift lexer and expression parser. No source is executed."""
from dataclasses import dataclass
import json
import re


class ConversionError(Exception):
    pass


@dataclass(frozen=True)
class Token:
    value: str
    file: str = ""
    line: int = 1


def lex(source, file=""):
    tokens, i, line = [], 0, 1
    while i < len(source):
        c = source[i]
        if c.isspace():
            line += c == "\n"
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
        elif source.startswith("/*", i):
            start, depth = i, 1
            i += 2
            while i < len(source) and depth:
                if source.startswith("/*", i):
                    depth += 1
                    i += 2
                elif source.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ConversionError(f"{file}:{line}: unterminated comment")
            line += source[start:i].count("\n")
        elif c == '"':
            start, start_line = i, line
            triple = source.startswith('"""', i)
            quote = '"""' if triple else '"'
            i += len(quote)
            interpolation_depth = 0
            while i < len(source):
                if source.startswith('\\(', i):
                    # Swift interpolation can contain nested calls and quoted strings.
                    i += 2
                    interpolation_depth = 1
                    while i < len(source) and interpolation_depth:
                        if source[i] == '"':
                            i += 1
                            while i < len(source) and source[i] != '"':
                                i += 2 if source[i] == '\\' else 1
                            i += 1
                        elif source[i] == '(':
                            interpolation_depth += 1
                            i += 1
                        elif source[i] == ')':
                            interpolation_depth -= 1
                            i += 1
                        else:
                            i += 1
                elif source[i] == '\\':
                    i += 2
                elif source.startswith(quote, i):
                    i += len(quote)
                    break
                else:
                    i += 1
            else:
                raise ConversionError(f"{file}:{start_line}: unterminated string")
            tokens.append(Token(source[start:i], file, start_line))
            line += source[start:i].count("\n")
        else:
            match = re.match(r"(?:[A-Za-z_$][\w$]*|\d(?:_?\d)*(?:\.\d(?:_?\d)*)?|\.\.\.<|\.\.<|\.\.\.|==|!=|<=|>=|&&|\|\||\?\?|\+=|-=|->|[^\s])", source[i:])
            value = match.group()
            tokens.append(Token(value, file, line))
            i += len(value)
    return tokens


def values(tokens):
    return [t.value for t in tokens]


def matching(tokens, start):
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for i in range(start, len(tokens)):
        v = tokens[i].value
        if v in pairs:
            stack.append(pairs[v])
        elif v in pairs.values():
            if not stack or stack.pop() != v:
                raise ConversionError(f"{tokens[i].file}:{tokens[i].line}: unbalanced delimiter")
            if not stack:
                return i
    t = tokens[start]
    raise ConversionError(f"{t.file}:{t.line}: unclosed {t.value}")


def split_top(tokens, delimiter=","):
    result, start, i = [], 0, 0
    while i < len(tokens):
        if tokens[i].value in ("(", "[", "{"):
            i = matching(tokens, i) + 1
        elif tokens[i].value == delimiter:
            result.append(tokens[start:i])
            start = i + 1
            i += 1
        else:
            i += 1
    if start < len(tokens):
        result.append(tokens[start:])
    return result


def arguments(tokens):
    result = []
    for part in split_top(tokens):
        if (len(part) > 2 and re.fullmatch(r"[A-Za-z_$][\w$]*", part[0].value)
                and part[1].value == ":"):
            result.append((part[0].value, part[2:]))
        else:
            result.append((None, part))
    return result


def bind_arguments(args, params, name):
    """Bind Swift call arguments by external label, including skipped defaults."""
    bound, cursor = {}, 0
    for label, part in args:
        if label is None:
            index = cursor
            if index < len(params) and params[index].get("label") != "_":
                raise ValueError(f"missing argument label {params[index]['label']!r} in call to {name}")
        else:
            index = next((candidate for candidate in range(cursor, len(params))
                          if params[candidate].get("label") == label), None)
            if index is None:
                expected = params[cursor].get("label") if cursor < len(params) else "no more arguments"
                raise ValueError(f"unexpected argument label {label!r}; expected {expected!r}")
            for skipped in range(cursor, index):
                parameter = params[skipped]
                if "default" not in parameter:
                    raise ValueError(f"missing argument {parameter.get('label')!r} in call to {name}")
                bound[parameter["name"]] = parameter["default"]
        if index >= len(params):
            raise ValueError(f"{name} received too many arguments")
        parameter = params[index]
        if index < cursor:
            raise ValueError(f"arguments for {name} must follow declaration order")
        bound[parameter["name"]] = part
        cursor = index + 1
    for index in range(cursor, len(params)):
        parameter = params[index]
        if "default" not in parameter:
            raise ValueError(f"missing argument {parameter.get('label')!r} in call to {name}")
        bound[parameter["name"]] = parameter["default"]
    return bound


def binding_target(expression):
    if not isinstance(expression, dict):
        return None
    if expression.get("op") == "state":
        return {"op": "binding", "key": expression["key"], "path": []}
    if expression.get("op") == "binding":
        return expression
    if expression.get("op") == "member":
        parent = binding_target(expression.get("a"))
        if parent:
            return {**parent, "path": parent.get("path", []) + [expression["name"]]}
    return None


class Expr:
    """Produces JSON expression trees consumed by a closed browser interpreter."""
    precedence = {"??": 0, "||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, ">": 4, "...": 4, "..<": 4,
                  "<=": 4, ">=": 4, "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}

    def __init__(self, tokens, names):
        self.tokens, self.names, self.i = tokens, names, 0

    @classmethod
    def parse(cls, tokens, names):
        tokens = list(tokens)
        optional_try = False
        while tokens and tokens[0].value in ("try", "await"):
            marker = tokens.pop(0).value
            if marker == "try" and tokens and tokens[0].value in ("?", "!"):
                optional_try = tokens.pop(0).value == "?" or optional_try
        parser = cls(tokens, names)
        result = parser.expression()
        if parser.i != len(tokens):
            raise ValueError("unsupported expression: " + " ".join(values(tokens)))
        return {"op": "tryOptional", "a": result} if optional_try else result

    def take(self):
        if self.i >= len(self.tokens):
            raise ValueError("incomplete expression")
        t = self.tokens[self.i]
        self.i += 1
        return t

    def peek(self, value):
        return self.i < len(self.tokens) and self.tokens[self.i].value == value

    def sorted_closure(self, closure):
        if not closure or closure[0].value != "{" or matching(closure, 0) != len(closure) - 1:
            raise ValueError("sorted requires a comparator closure")
        body = closure[1:-1]
        depth = 0
        in_index = None
        for i, token in enumerate(body):
            if token.value in ("(", "[", "{"):
                depth += 1
            elif token.value in (")", "]", "}"):
                depth -= 1
            elif token.value == "in" and depth == 0:
                in_index = i
                break
        if in_index is None:
            params = ["$0", "$1"]
        else:
            params = [token.value for token in body[:in_index] if token.value != ","]
            body = body[in_index + 1:]
            if len(params) != 2:
                raise ValueError("sorted comparator needs two parameters")
        names = dict(self.names)
        names.update({name: {"op": "local", "key": name} for name in params})
        if any(token.value in ("if", "return", "let", "var") for token in body):
            closure = {"op": "closure", "params": params, "body": ClosureStatements(body, names).parse()}
            return {"op": "sorted", "a": None, "closure": closure, "params": params}
        return {"op": "sorted", "a": None, "compare": Expr.parse(body, names), "params": params}

    def expression(self, minimum=0):
        t = self.take()
        v = t.value
        if v == "&":
            # Swift's inout marker is a call-site annotation. The translated
            # values are objects/arrays for supported mutable helpers, so the
            # same reference reaches the generated JavaScript helper.
            left = self.expression(7)
        elif v in ("!", "-"):
            left = {"op": "unary", "kind": v, "a": self.expression(7)}
        elif v == "." and self.i < len(self.tokens) and self.tokens[self.i].value == "init":
            self.i += 1
            if not self.peek("("):
                raise ValueError(".init requires named arguments")
            end = matching(self.tokens, self.i)
            parts = arguments(self.tokens[self.i + 1:end])
            if any(name is None for name, _ in parts):
                raise ValueError(".init requires named arguments")
            fields = [{"name": name, "value": Expr.parse(part, self.names)} for name, part in parts]
            self.i = end + 1
            left = {"op": "construct", "type": "__inferred__", "fields": fields}
        elif v == "." and self.i < len(self.tokens) and self.tokens[self.i].value in ("day", "month", "year"):
            left = {"op": "literal", "value": self.tokens[self.i].value}
            self.i += 1
        elif v == "." and self.i < len(self.tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.tokens[self.i].value):
            left = {"op": "enumCase", "name": self.tokens[self.i].value, "args": []}
            self.i += 1
        elif v == "(":
            left = self.expression()
            items = [left]
            while self.peek(","):
                self.take()
                items.append(self.expression())
            if self.take().value != ")":
                raise ValueError("expected closing parenthesis")
            if len(items) > 1:
                left = {"op": "tuple", "items": items}
        elif v == "[":
            if self.peek(":"):
                self.take()
                if self.take().value != "]":
                    raise ValueError("empty dictionary literal must be [:]")
                left = {"op": "dictionary", "entries": []}
            else:
                end = matching(self.tokens, self.i - 1)
                content = self.tokens[self.i:end]
                is_dictionary = False
                cursor = 0
                while cursor < len(content):
                    if content[cursor].value in ("(", "[", "{"):
                        cursor = matching(content, cursor) + 1
                    elif content[cursor].value == ":":
                        is_dictionary = True
                        break
                    else:
                        cursor += 1
                if is_dictionary:
                    entries = []
                    for part in split_top(content):
                        colon = next((index for index, item in enumerate(part) if item.value == ":"), None)
                        if colon is None or colon == 0 or colon + 1 >= len(part):
                            raise ValueError("dictionary entries need key: value pairs")
                        entries.append((Expr.parse(part[:colon], self.names), Expr.parse(part[colon + 1:], self.names)))
                    left = {"op": "dictionary", "entries": entries}
                else:
                    items = []
                    if content:
                        items = [Expr.parse(part, self.names) for part in split_top(content)]
                    left = {"op": "array", "items": items}
                self.i = end + 1
        elif v == "{" and matching(self.tokens, self.i - 1) == len(self.tokens) - 1:
            left = Expr.parse_closure(self.tokens[self.i - 1:], self.names)
            self.i = len(self.tokens)
        elif v.startswith('"'):
            left = self.string(t)
        elif re.fullmatch(r"\d(?:_?\d)*(?:\.\d(?:_?\d)*)?", v):
            normalized = v.replace("_", "")
            left = {"op": "literal", "value": float(normalized) if "." in normalized else int(normalized)}
        elif v in ("true", "false", "nil"):
            left = {"op": "literal", "value": {"true": True, "false": False, "nil": None}[v]}
        elif v in ("min", "max") and self.peek("("):
            end = matching(self.tokens, self.i)
            parts = arguments(self.tokens[self.i + 1:end])
            if any(label is not None for label, _ in parts) or len(parts) != 2:
                raise ValueError(f"{v} requires two numeric arguments")
            left = {"op": "call", "name": v, "a": {"op": "type", "name": v},
                    "args": [Expr.parse(part, self.names) for _, part in parts]}
            self.i = end + 1
        elif v in self.names:
            left = self.names[v]
        elif re.fullmatch(r"[A-Z][A-Za-z0-9_]*", v):
            left = {"op": "type", "name": v}
        else:
            raise ValueError(f"unsupported expression or unknown name: {v}")
        while self.i < len(self.tokens):
            op = self.tokens[self.i].value
            if op == "(" and left.get("op") == "enumCase":
                end = matching(self.tokens, self.i)
                parts = arguments(self.tokens[self.i + 1:end])
                if any(label is not None for label, _ in parts):
                    raise ValueError("associated enum cases support positional values only")
                left["args"] = [Expr.parse(part, self.names) for _, part in parts]
                self.i = end + 1
                continue
            if op == "(" and left.get("op") == "type":
                end = matching(self.tokens, self.i)
                call_tokens = self.tokens[self.i + 1:end]
                parts = arguments(call_tokens)
                if left["name"] == "Date":
                    named = {label: part for label, part in parts if label is not None}
                    positional = [part for label, part in parts if label is None]
                    if not parts:
                        left = {"op": "nowDate"}
                        self.i = end + 1
                        continue
                    if not positional and set(named) == {"timeIntervalSinceReferenceDate"}:
                        left = {"op": "date", "seconds": Expr.parse(named["timeIntervalSinceReferenceDate"], self.names)}
                        self.i = end + 1
                        continue
                    if not positional and set(named) == {"timeIntervalSince1970"}:
                        left = {"op": "date1970", "seconds": Expr.parse(named["timeIntervalSince1970"], self.names)}
                        self.i = end + 1
                        continue
                    raise ValueError("Date supports current date and timeIntervalSinceReferenceDate:/timeIntervalSince1970:")
                if left["name"] in ("Array", "Set"):
                    positional = [part for label, part in parts if label is None]
                    if len(positional) == 1 and not any(label is not None for label, _ in parts):
                        left = Expr.parse(positional[0], self.names)
                        self.i = end + 1
                        continue
                    raise ValueError(f"{left['name']} supports one collection expression")
                if left["name"] == "String":
                    named = {label: part for label, part in parts if label is not None}
                    positional = [part for label, part in parts if label is None]
                    template_tokens = named.get("format", [])
                    if (len(template_tokens) == 1 and template_tokens[0].value.startswith('"')
                            and re.fullmatch(r"(?:[^%]|%%|%0?\d*d)*", json.loads(template_tokens[0].value))):
                        template = json.loads(template_tokens[0].value)
                        placeholders = re.findall(r"%(?:0?(\d*))d", template)
                        if len(placeholders) == len(positional):
                            left = {"op": "format", "template": template,
                                    "args": [Expr.parse(part, self.names) for part in positional]}
                            self.i = end + 1
                            continue
                if left["name"] in ("Int", "Int64", "Double", "String", "Bool"):
                    positional = [part for label, part in parts if label is None]
                    if len(positional) == 1 and not any(label is not None for label, _ in parts):
                        left = {"op": "convert", "type": left["name"], "value": Expr.parse(positional[0], self.names)}
                        self.i = end + 1
                        continue
                    raise ValueError(f"{left['name']} supports one conversion expression")
                if left["name"] == "TimeInterval":
                    positional = [part for label, part in parts if label is None]
                    if len(positional) == 1 and not any(label is not None for label, _ in parts):
                        left = Expr.parse(positional[0], self.names)
                        self.i = end + 1
                        continue
                    raise ValueError("TimeInterval supports one numeric expression")
                if left["name"] == "String":
                    named = {label: part for label, part in parts if label is not None}
                    positional = [part for label, part in parts if label is None]
                    template_tokens = named.get("format", [])
                    if (len(template_tokens) == 1 and template_tokens[0].value.startswith('"')
                            and re.fullmatch(r"(?:[^%]|%%|%0?\d*d)*", json.loads(template_tokens[0].value))):
                        template = json.loads(template_tokens[0].value)
                        placeholders = re.findall(r"%(?:0?(\d*))d", template)
                        if len(placeholders) == len(positional):
                            left = {"op": "format", "template": template,
                                    "args": [Expr.parse(part, self.names) for part in positional]}
                            self.i = end + 1
                            continue
                    raise ValueError("String(format:) supports integer %d placeholders")
                if left["name"] == "Dictionary":
                    positional = [part for label, part in parts if label is None]
                    named = {label: part for label, part in parts if label is not None}
                    if (len(positional) == 1 and values(named.get("uniquingKeysWith", [])) == ["+"]):
                        left = {"op": "dictionaryCounts", "a": Expr.parse(positional[0], self.names)}
                        self.i = end + 1
                        continue
                    raise ValueError("only Dictionary(_:uniquingKeysWith: +) frequency counts are supported")
                if any(name is None for name, _ in parts):
                    raise ValueError("model initializers require named arguments")
                fields = []
                for name, part in parts:
                    fields.append({"name": name, "value": Expr.parse(part, self.names)})
                self.i = end + 1
                closure_properties = self.names.get("__typeClosures", {}).get(left["name"], [])
                if self.peek("{") and closure_properties:
                    if len(closure_properties) != 1:
                        raise ValueError(f"{left['name']} has multiple closure properties; label the trailing closure")
                    closure_end = matching(self.tokens, self.i)
                    closure = Expr.parse_closure(self.tokens[self.i:closure_end + 1], self.names)
                    fields.append({"name": closure_properties[0], "value": closure})
                    self.i = closure_end + 1
                left = {"op": "construct", "type": left["name"], "fields": fields}
                continue
            if op == "(" and left.get("op") in ("methodRef", "functionRef", "componentMethodRef", "firebaseRef"):
                end = matching(self.tokens, self.i)
                args = arguments(self.tokens[self.i + 1:end])
                self.i = end + 1
                if left.get("op") == "functionRef":
                    params = left.get("params", [])
                    call_args = {name: Expr.parse(part, self.names) if isinstance(part, list) else part
                                 for name, part in bind_arguments(args, params, left["name"]).items()}
                    left = {"op": "globalCall", "name": left["name"], "args": call_args}
                elif left.get("op") == "firebaseRef":
                    params = left.get("params", [])
                    call_args = {}
                    for position, (label, part) in enumerate(args):
                        if position >= len(params):
                            raise ValueError(f"{left['name']} received too many arguments")
                        param = params[position]
                        if label is not None and label != param:
                            raise ValueError(f"unexpected argument label {label!r}; expected {param!r}")
                        call_args[param] = (Expr.parse(part, self.names) if param != "as"
                                            else {"op": "literal", "value": "".join(values(part)).replace(".self", "")})
                    left = {"op": "firebaseCall", "name": left["name"], "args": call_args}
                elif left.get("op") == "componentMethodRef":
                    params = left.get("params", [])
                    call_args = {name: Expr.parse(part, self.names) if isinstance(part, list) else part
                                 for name, part in bind_arguments(args, params, left["key"]).items()}
                    left = {"op": "componentCall", "key": left["key"], "args": call_args}
                else:
                    call_args = [Expr.parse(part, self.names) for _, part in args]
                    left = {"op": "call", "name": left["name"], "a": {"op": "self"}, "args": call_args}
                continue
            if op == "[":
                end = matching(self.tokens, self.i)
                parts = arguments(self.tokens[self.i + 1:end])
                positional = [part for label, part in parts if label is None]
                named = {label: part for label, part in parts if label is not None}
                if len(positional) != 1 or not named.keys() <= {"default"}:
                    raise ValueError("subscript supports one key and optional default:")
                left = {"op": "subscript", "a": left, "key": Expr.parse(positional[0], self.names)}
                if "default" in named:
                    left["default"] = Expr.parse(named["default"], self.names)
                self.i = end + 1
                continue
            if (op == "." and self.i + 2 < len(self.tokens)
                    and self.tokens[self.i + 1].value == "prefix"
                    and self.tokens[self.i + 2].value == "("):
                end = matching(self.tokens, self.i + 2)
                args = split_top(self.tokens[self.i + 3:end])
                if len(args) != 1 or not args[0]:
                    raise ValueError(".prefix requires one count expression")
                count = Expr.parse(args[0], self.names)
                self.i = end + 1
                left = {"op": "prefix", "a": left, "count": count}
                continue
            if op == "." and self.i + 1 < len(self.tokens) and self.tokens[self.i + 1].value in ("count", "isEmpty"):
                self.i += 2
                left = {"op": "property", "kind": self.tokens[self.i - 1].value, "a": left}
                continue
            if op == "?" and self.i + 2 < len(self.tokens) and self.tokens[self.i + 1].value == ".":
                member = self.tokens[self.i + 2].value
                ternary_color = self.i + 3 < len(self.tokens) and self.tokens[self.i + 3].value == ":"
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", member) and not ternary_color:
                    self.i += 3
                    left = {"op": "member", "name": member, "a": left}
                    continue
            if op == "?" and self.i + 1 < len(self.tokens) and self.tokens[self.i + 1].value == "[":
                end = matching(self.tokens, self.i + 1)
                parts = arguments(self.tokens[self.i + 2:end])
                positional = [part for label, part in parts if label is None]
                named = {label: part for label, part in parts if label is not None}
                if len(positional) != 1 or not named.keys() <= {"default"}:
                    raise ValueError("optional subscript supports one key and optional default:")
                left = {"op": "subscript", "a": left, "key": Expr.parse(positional[0], self.names)}
                if "default" in named:
                    left["default"] = Expr.parse(named["default"], self.names)
                self.i = end + 1
                continue
            if op == "." and self.i + 1 < len(self.tokens):
                member = self.tokens[self.i + 1].value
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", member):
                    if left.get("op") == "type" and left.get("name") == "Calendar" and member == "current":
                        self.i += 2
                        left = {"op": "calendar"}
                        continue
                    if left.get("op") == "type":
                        self.i += 2
                        left = {"op": "enumCase", "type": left["name"], "name": member, "args": []}
                        continue
                    self.i += 2
                    if member == "sorted" and self.peek("{"):
                        end = matching(self.tokens, self.i)
                        sorted_expr = self.sorted_closure(self.tokens[self.i:end + 1])
                        sorted_expr["a"] = left
                        self.i = end + 1
                        left = sorted_expr
                        continue
                    if self.peek("("):
                        end = matching(self.tokens, self.i)
                        args = arguments(self.tokens[self.i + 1:end])
                        if member == "sorted" and len(args) == 1 and args[0][0] == "using":
                            sorted_expr = self.sorted_closure(args[0][1])
                            sorted_expr["a"] = left
                            self.i = end + 1
                            left = sorted_expr
                            continue
                        if member in ("min", "max") and len(args) == 1 and args[0][0] in ("by", "using"):
                            sorted_expr = self.sorted_closure(args[0][1])
                            left = {"op": "extremumBy", "kind": member, "a": left,
                                    "compare": sorted_expr.get("compare"), "params": sorted_expr.get("params")}
                            self.i = end + 1
                            continue
                        if member == "reduce":
                            named = {label: part for label, part in args if label is not None}
                            positional = [part for label, part in args if label is None]
                            initial_tokens = named.get("into") or (positional[0] if positional else [])
                            if not initial_tokens:
                                raise ValueError("reduce requires an initial accumulator")
                            if self.tokens[end + 1:end + 2] and self.tokens[end + 1].value == "{":
                                closure_end = matching(self.tokens, end + 1)
                                closure = Expr.parse_closure(self.tokens[end + 1:closure_end + 1], self.names, ["$0", "$1"])
                                self.i = closure_end + 1
                            elif len(positional) == 2 and values(positional[1]) in (["+"], ["-"], ["*"], ["/"], ["min"], ["max"]):
                                operation = values(positional[1])[0]
                                if operation in ("min", "max"):
                                    returned = {"op": "call", "name": operation, "a": {"op": "type", "name": operation},
                                                "args": [{"op": "local", "key": "$0"}, {"op": "local", "key": "$1"}]}
                                else:
                                    returned = {"op": "binary", "kind": operation,
                                                "a": {"op": "local", "key": "$0"}, "b": {"op": "local", "key": "$1"}}
                                closure = {"op": "closure", "params": ["$0", "$1"], "body": [{"kind": "return", "value": returned}]}
                                self.i = end + 1
                            else:
                                raise ValueError("reduce supports an inline closure or +, -, *, / operator")
                            left = {"op": "reduce", "a": left, "initial": Expr.parse(initial_tokens, self.names), "closure": closure}
                            continue
                        call_args = [Expr.parse(part, self.names) for _, part in args]
                        self.i = end + 1
                        left = {"op": "call", "name": member, "a": left, "args": call_args}
                    elif member == "removeAll" and self.peek("{"):
                        end = matching(self.tokens, self.i)
                        closure = Expr.parse_closure(self.tokens[self.i:end + 1], self.names)
                        self.i = end + 1
                        left = {"op": "call", "name": member, "a": left, "args": [closure]}
                    elif member in ("min", "max") and self.peek("{"):
                        end = matching(self.tokens, self.i)
                        sorted_expr = self.sorted_closure(self.tokens[self.i:end + 1])
                        left = {"op": "extremumBy", "kind": member, "a": left,
                                "compare": sorted_expr.get("compare"), "params": sorted_expr.get("params")}
                        self.i = end + 1
                    elif member in ("map", "mapValues", "compactMap", "flatMap", "filter") and self.peek("{"):
                        end = matching(self.tokens, self.i)
                        closure = Expr.parse_closure(self.tokens[self.i:end + 1], self.names)
                        self.i = end + 1
                        left = {"op": "call", "name": member, "a": left, "args": [closure]}
                    elif member == "enumerated":
                        left = {"op": "call", "name": member, "a": left, "args": []}
                    elif member in ("first", "last"):
                        left = {"op": "property", "kind": member, "a": left}
                    else:
                        left = {"op": "member", "name": member, "a": left}
                    continue
            if op == "(" and left.get("op") in ("member", "local", "state", "binding", "closure"):
                end = matching(self.tokens, self.i)
                args = arguments(self.tokens[self.i + 1:end])
                if any(label is not None for label, _ in args):
                    raise ValueError("function-valued calls support positional arguments only")
                left = {"op": "invoke", "callee": left,
                        "args": [Expr.parse(part, self.names) for _, part in args]}
                self.i = end + 1
                continue
            if op == "(" and left.get("op") == "dismissRef":
                end = matching(self.tokens, self.i)
                if end != self.i + 1:
                    raise ValueError("dismiss() takes no arguments")
                left = {"op": "dismissCall"}
                self.i = end + 1
                continue
            if op == "!":
                self.i += 1
                left = {"op": "unwrap", "a": left}
                continue
            if op == "as" and self.i + 2 < len(self.tokens) and self.tokens[self.i + 1].value in ("?", "!"):
                cast_kind = self.tokens[self.i + 1].value
                type_token = self.tokens[self.i + 2]
                if not re.fullmatch(r"[A-Z][A-Za-z0-9_]*", type_token.value):
                    raise ValueError("casts require a named Swift type")
                self.i += 3
                left = {"op": "cast", "kind": cast_kind, "type": type_token.value, "a": left}
                continue
            priority = self.precedence.get(op, -1)
            if priority < minimum:
                break
            self.i += 1
            left = {"op": "binary", "kind": op, "a": left, "b": self.expression(priority + 1)}
        if minimum == 0 and self.peek("?"):
            self.take()
            yes = self.expression()
            if self.take().value != ":":
                raise ValueError("expected : in conditional")
            left = {"op": "conditional", "test": left, "yes": yes, "no": self.expression()}
        return left

    def string(self, token):
        raw = token.value
        if raw.startswith('"""'):
            raise ValueError("multiline strings are unsupported")
        body, parts, start, i = raw[1:-1], [], 0, 0
        while i < len(body):
            if body.startswith('\\(', i):
                parts.append({"op": "literal", "value": self.decode(body[start:i])})
                depth, end = 1, i + 2
                in_string = False
                while end < len(body) and depth:
                    c = body[end]
                    if c == '\\':
                        end += 2
                        continue
                    if c == '"':
                        in_string = not in_string
                    if not in_string:
                        depth += (c == '(') - (c == ')')
                    end += 1
                if depth:
                    raise ValueError("unclosed string interpolation")
                interpolation = lex(body[i + 2:end - 1], token.file)
                interpolation_args = arguments(interpolation)
                labels = {label: part for label, part in interpolation_args if label is not None}
                positional = [part for label, part in interpolation_args if label is None]
                specifier = labels.get("specifier", [])
                if (len(positional) == 1 and len(specifier) == 1 and specifier[0].value.startswith('"')
                        and re.fullmatch(r"%0?\.\d+f", json.loads(specifier[0].value))):
                    precision = int(re.search(r"\.(\d+)f", json.loads(specifier[0].value)).group(1))
                    parts.append({"op": "fixed", "value": Expr.parse(positional[0], self.names), "precision": precision})
                else:
                    parts.append(Expr.parse(interpolation, self.names))
                i = start = end
            elif body[i] == '\\':
                i += 2
            else:
                i += 1
        parts.append({"op": "literal", "value": self.decode(body[start:])})
        return parts[0] if len(parts) == 1 else {"op": "concat", "items": parts}

    @staticmethod
    def parse_closure(tokens, outer_names, default_params=None):
        if not tokens or tokens[0].value != "{" or matching(tokens, 0) != len(tokens) - 1:
            raise ValueError("expected a closure")
        body = tokens[1:-1]
        depth = 0
        in_index = None
        for index, token in enumerate(body):
            if token.value in ("(", "[", "{"):
                depth += 1
            elif token.value in (")", "]", "}"):
                depth -= 1
            elif token.value == "in" and depth == 0:
                in_index = index
                break
        if in_index is None:
            params, statements = (default_params or ["$0"]), body
        else:
            header, statements = body[:in_index], body[in_index + 1:]
            arrow = next((i for i, token in enumerate(header) if token.value == "->"), len(header))
            params = [token.value for token in header[:arrow] if token.value not in (",", "(", ")")]
            if not params:
                params = ["$0"]
        names = dict(outer_names)
        for param in params:
            names[param] = {"op": "local", "key": param}
        statements = ClosureStatements(statements, names).parse()
        return {"op": "closure", "params": params, "body": statements}

    @staticmethod
    def decode(text):
        # JSON shares Swift's common escapes; reject unsupported escapes explicitly.
        try:
            return json.loads('"' + text + '"')
        except (ValueError, json.JSONDecodeError):
            raise ValueError("unsupported string escape") from None


class ClosureStatements:
    """Small closure-body parser for literal array transforms and predicates."""
    def __init__(self, tokens, names):
        self.ts, self.names, self.i = tokens, names, 0

    def block(self):
        if self.i >= len(self.ts) or self.ts[self.i].value != "{":
            raise ValueError("guard else needs a block")
        end = matching(self.ts, self.i)
        body = self.ts[self.i + 1:end]
        self.i = end + 1
        return ClosureStatements(body, self.names).parse()

    def expression_until_line(self, stop_words=()):
        start = self.i
        while self.i < len(self.ts):
            token = self.ts[self.i]
            if token.value == ";" or token.value in stop_words:
                break
            if self.i > start and token.line > self.ts[self.i - 1].line:
                break
            if token.value in ("(", "[", "{"):
                self.i = matching(self.ts, self.i) + 1
            else:
                self.i += 1
        result = self.ts[start:self.i]
        if self.i < len(self.ts) and self.ts[self.i].value == ";":
            self.i += 1
        if not result:
            raise ValueError("expected a closure expression")
        return Expr.parse(result, self.names)

    def statement(self):
        token = self.ts[self.i]
        if token.value == "guard":
            self.i += 1
            start = self.i
            while self.i < len(self.ts) and self.ts[self.i].value != "else":
                if self.ts[self.i].value in ("(", "[", "{"):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("guard must include an else block")
            bindings, tests = [], []
            for part in split_top(self.ts[start:self.i]):
                if part and part[0].value in ("let", "var"):
                    if len(part) < 2:
                        raise ValueError("guard binding needs a name")
                    name = part[1].value
                    equal = next((index for index, item in enumerate(part) if item.value == "="), None)
                    expression = Expr.parse(part[equal + 1:], self.names) if equal is not None else Expr.parse([part[1]], self.names)
                    bindings.append({"name": name, "value": expression})
                    self.names[name] = {"op": "local", "key": name}
                elif part:
                    tests.append(Expr.parse(part, self.names))
            self.i += 1
            else_body = self.block()
            test = {"op": "literal", "value": True}
            for expression in tests:
                test = expression if test.get("value") is True else {"op": "binary", "kind": "&&", "a": test, "b": expression}
            return {"kind": "guard", "bindings": bindings, "test": test, "else": else_body}
        if token.value == "if":
            self.i += 1
            start = self.i
            while self.i < len(self.ts) and self.ts[self.i].value != "{":
                if self.ts[self.i].value in ("(", "["):
                    self.i = matching(self.ts, self.i) + 1
                else:
                    self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("if closure has no body")
            test = Expr.parse(self.ts[start:self.i], self.names)
            yes = self.block()
            no = []
            if self.i < len(self.ts) and self.ts[self.i].value == "else":
                self.i += 1
                if self.i < len(self.ts) and self.ts[self.i].value == "if":
                    no = [self.statement()]
                else:
                    no = self.block()
            return {"kind": "if", "test": test, "yes": yes, "no": no}
        if token.value in ("let", "var"):
            self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("closure local needs a name")
            name = self.ts[self.i].value
            self.i += 1
            while self.i < len(self.ts) and self.ts[self.i].value != "=":
                self.i += 1
            if self.i >= len(self.ts):
                raise ValueError("closure local needs an initializer")
            self.i += 1
            value = self.expression_until_line()
            self.names[name] = {"op": "local", "key": name}
            return {"kind": "declare", "name": name, "value": value}
        if token.value == "return":
            self.i += 1
            return {"kind": "return", "value": self.expression_until_line()}
        if token.value == ";":
            self.i += 1
            return None
        if self.i + 1 < len(self.ts) and self.ts[self.i + 1].value in ("=", "+=", "-="):
            name = token.value
            operator = self.ts[self.i + 1].value
            self.i += 2
            expression = self.names.get(name)
            target = binding_target(expression)
            value = self.expression_until_line()
            if target:
                return {"kind": "stateAssign", "binding": target, "operator": operator, "value": value}
            return {"kind": "assign", "name": name, "operator": operator, "value": value}
        return {"kind": "expression", "value": self.expression_until_line()}

    def parse(self):
        result = []
        while self.i < len(self.ts):
            start = self.i
            statement = self.statement()
            if statement:
                result.append(statement)
            if self.i <= start:
                raise ValueError("closure parser did not advance")
        if len(result) == 1 and result[0]["kind"] == "expression":
            result[0]["kind"] = "return"
        return result
