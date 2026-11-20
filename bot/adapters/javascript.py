"""JavaScript/JSX(React) 처리기. tree-sitter로 함수·컴포넌트, 호출, HTTP 요청을 뽑는다."""
from __future__ import annotations

import posixpath
import re

import tree_sitter_javascript
from tree_sitter import Language, Node, Parser

from .base import Chunk, Edge, FileAnalysis, Frame, LanguageAdapter, RawCall, Route, normalize_path

_LANG = Language(tree_sitter_javascript.language())
_FUNC_VALUES = ("arrow_function", "function_expression", "function", "generator_function")
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_WRAPPERS = ("memo", "forwardRef", "useCallback", "observer")

# at loadRoom (https://host/assets/index-a1b2.js:1:48211) / at https://host/x.js:1:2
# 사람이 붙여 넣은 글은 줄이 합쳐져 있기도 해서 줄 시작·끝에 묶지 않는다
_FRAME = re.compile(r"(?:^|\s)at\s+(?:async\s+)?(?:(?P<fn>[^\s(]+)\s+\()?(?P<file>[^\s()]+?):(?P<line>\d+):(?P<col>\d+)\)?")
# Firefox/Safari: loadRoom@https://host/x.js:1:2
_FRAME_FF = re.compile(r"(?:^|\s)(?P<fn>[^\s@]*)@(?P<file>\S+?):(?P<line>\d+):(?P<col>\d+)")
# "at" 없이 위치만 적힌 경우: index-a1b2.js:1:48211
_FRAME_BARE = re.compile(r"(?P<fn>)(?P<file>[^\s()'\"]+\.(?:js|jsx|mjs)):(?P<line>\d+):(?P<col>\d+)")


def _t(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _walk(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _unwrap_function(value: Node | None) -> Node | None:
    """const X = memo(() => {...}) 처럼 감싼 경우 안쪽 함수를 꺼낸다."""
    if value is None:
        return None
    if value.type in _FUNC_VALUES:
        return value
    if value.type == "call_expression":
        callee = _t(value.child_by_field_name("function")).split(".")[-1]
        if callee in _WRAPPERS:
            args = value.child_by_field_name("arguments")
            if args is not None:
                for a in args.named_children:
                    inner = _unwrap_function(a)
                    if inner is not None:
                        return inner
    return None


class JavaScriptAdapter(LanguageAdapter):
    language = "javascript"
    suffixes = (".js", ".jsx", ".mjs")

    def __init__(self) -> None:
        self._parser = Parser(_LANG)

    def extract_chunks(self, source: bytes, file_path: str) -> list[Chunk]:
        return self.analyze(source, file_path).chunks

    def extract_calls(self, source: bytes, file_path: str) -> list[RawCall]:
        return self.analyze(source, file_path).calls

    def extract_routes(self, source: bytes, file_path: str) -> tuple[list[Route], list[Route]]:
        a = self.analyze(source, file_path)
        return a.routes_served, a.routes_called

    def parse_stack_trace(self, text: str, base_packages: list[str] | None = None) -> list[Frame]:
        frames: list[Frame] = []
        seen: set[tuple] = set()
        for pattern in (_FRAME, _FRAME_FF, _FRAME_BARE):
            for m in pattern.finditer(text):
                file = m.group("file")
                if "node_modules" in file or file.startswith("node:"):
                    continue
                fn = (m.group("fn") or "").strip()
                if fn in ("new", "async"):
                    fn = ""
                key = (file, m.group("line"), m.group("col"))
                if key in seen:
                    continue
                seen.add(key)
                frames.append(
                    Frame(
                        symbol=fn or "<anonymous>",
                        file=file,
                        line=int(m.group("line")),
                        column=int(m.group("col")),
                        method=fn or None,
                    )
                )
        return frames

    # ── 한 번의 파싱으로 전부 뽑기 ─────────────────────────────
    def analyze(self, source: bytes, file_path: str) -> FileAnalysis:
        root = self._parser.parse(source).root_node
        a = FileAnalysis(
            file_path=file_path,
            meta={
                "imports": {},  # 지역 이름 → (모듈 경로, 가져온 이름)
                "exports_default": None,
                "locals": set(),  # 이 파일의 최상위 청크 이름
                "consts": {},  # 최상위 문자열 상수
            },
        )
        self._collect_consts(root, a)
        for node in root.named_children:
            self._top_level(node, source, a)
        return a

    def _collect_consts(self, root: Node, a: FileAnalysis) -> None:
        consts = a.meta["consts"]
        for node in root.named_children:
            decl = node
            if node.type == "export_statement":
                decl = node.child_by_field_name("declaration") or node
            if decl.type not in ("lexical_declaration", "variable_declaration"):
                continue
            for d in decl.named_children:
                if d.type != "variable_declarator":
                    continue
                value = d.child_by_field_name("value")
                name = _t(d.child_by_field_name("name"))
                if value is None:
                    continue
                if value.type in ("string", "template_string", "binary_expression"):
                    resolved = self._url_text(value, consts)
                    if resolved is not None:
                        consts[name] = resolved

    def _top_level(self, node: Node, source: bytes, a: FileAnalysis) -> None:
        if node.type == "import_statement":
            self._import(node, a)
            return
        outer = node
        is_default = False
        if node.type == "export_statement":
            is_default = any(c.type == "default" for c in node.children)
            decl = node.child_by_field_name("declaration")
            if decl is None:
                value = node.child_by_field_name("value")
                if is_default and value is not None:
                    fn = _unwrap_function(value)
                    if fn is not None:
                        self._add_chunk("default", fn, outer, source, a)
                        a.meta["exports_default"] = "default"
                    elif value.type == "identifier":
                        a.meta["exports_default"] = _t(value)
                return
            node = decl
        if node.type in ("function_declaration", "generator_function_declaration"):
            name = _t(node.child_by_field_name("name")) or "default"
            self._add_chunk(name, node, outer, source, a)
            if is_default:
                a.meta["exports_default"] = name
        elif node.type == "class_declaration":
            cname = _t(node.child_by_field_name("name")) or "default"
            body = node.child_by_field_name("body")
            if body is not None:
                for m in body.named_children:
                    if m.type == "method_definition":
                        self._add_chunk(f"{cname}.{_t(m.child_by_field_name('name'))}", m, m, source, a)
            if is_default:
                a.meta["exports_default"] = cname
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for d in node.named_children:
                if d.type != "variable_declarator":
                    continue
                name = _t(d.child_by_field_name("name"))
                value = d.child_by_field_name("value")
                fn = _unwrap_function(value)
                if fn is not None:
                    self._add_chunk(name, fn, outer if len(node.named_children) == 1 else d, source, a)
                elif value is not None and value.type == "object":
                    # const roomApi = { getRoom: (id) => api.get(...) }
                    for pair in value.named_children:
                        if pair.type == "pair":
                            pfn = _unwrap_function(pair.child_by_field_name("value"))
                            if pfn is not None:
                                self._add_chunk(f"{name}.{_t(pair.child_by_field_name('key'))}", pfn, pair, source, a)
                        elif pair.type == "method_definition":
                            self._add_chunk(f"{name}.{_t(pair.child_by_field_name('name'))}", pair, pair, source, a)

    def _import(self, node: Node, a: FileAnalysis) -> None:
        src = node.child_by_field_name("source")
        module = _t(src)[1:-1] if src is not None else ""
        for c in _walk(node):
            if c.type == "import_clause":
                for x in c.named_children:
                    if x.type == "identifier":
                        a.meta["imports"][_t(x)] = (module, "default")
                    elif x.type == "namespace_import":
                        ident = next((i for i in x.named_children if i.type == "identifier"), None)
                        if ident is not None:
                            a.meta["imports"][_t(ident)] = (module, "*")
                    elif x.type == "named_imports":
                        for spec in x.named_children:
                            if spec.type == "import_specifier":
                                orig = _t(spec.child_by_field_name("name"))
                                alias = _t(spec.child_by_field_name("alias")) or orig
                                a.meta["imports"][alias] = (module, orig)

    def _add_chunk(self, name: str, fn: Node, outer: Node, source: bytes, a: FileAnalysis) -> None:
        qualified = f"{a.file_path}#{name}"
        base = posixpath.basename(a.file_path)
        text = source[outer.start_byte : outer.end_byte].decode("utf-8", "replace")
        has_jsx = any(n.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment") for n in _walk(fn))
        short = name.rsplit(".", 1)[-1]
        kind = "component" if has_jsx and short[:1].isupper() else "function"
        body = fn.child_by_field_name("body")
        sig_end = body.start_byte if body is not None else fn.end_byte
        signature = " ".join(source[outer.start_byte : sig_end].decode("utf-8", "replace").split())
        a.chunks.append(
            Chunk(
                qualified_name=qualified,
                name=f"{base}#{name}",
                kind=kind,
                file_path=a.file_path,
                start_line=outer.start_point.row + 1,
                end_line=outer.end_point.row + 1,
                signature=signature[:300],
                content=text,
            )
        )
        a.meta["locals"].add(name)
        self._calls(fn, qualified, a)

    def _calls(self, fn: Node, caller: str, a: FileAnalysis) -> None:
        consts = a.meta["consts"]
        for n in _walk(fn):
            if n.type == "call_expression":
                callee = n.child_by_field_name("function")
                args = n.child_by_field_name("arguments")
                arg_nodes = args.named_children if args is not None else []
                line = n.start_point.row + 1
                if callee is None:
                    continue
                if callee.type == "identifier":
                    name = _t(callee)
                    if name == "fetch" and arg_nodes:
                        url = self._url_text(arg_nodes[0], consts)
                        if url is not None and self._looks_like_path(url):
                            method = self._fetch_method(arg_nodes[1] if len(arg_nodes) > 1 else None)
                            a.routes_called.append(Route(method, normalize_path(url), caller, url, line))
                        continue
                    a.calls.append(RawCall(caller=caller, name=name, receiver=None, argc=len(arg_nodes), line=line))
                elif callee.type == "member_expression":
                    obj = callee.child_by_field_name("object")
                    prop = _t(callee.child_by_field_name("property"))
                    if prop in _HTTP_METHODS and arg_nodes and arg_nodes[0].type in ("string", "template_string", "binary_expression", "identifier"):
                        url = self._url_text(arg_nodes[0], consts)
                        if url is not None and self._looks_like_path(url):
                            a.routes_called.append(Route(prop.upper(), normalize_path(url), caller, url, line))
                            continue
                    if obj is not None and obj.type == "identifier":
                        a.calls.append(RawCall(caller=caller, name=prop, receiver=_t(obj), argc=len(arg_nodes), line=line))
            elif n.type in ("jsx_opening_element", "jsx_self_closing_element"):
                tag = _t(n.child_by_field_name("name"))
                if tag[:1].isupper():
                    head, _, rest = tag.partition(".")
                    a.calls.append(
                        RawCall(caller=caller, name=rest or head, receiver=head if rest else None, kind="render", line=n.start_point.row + 1)
                    )

    @staticmethod
    def _looks_like_path(url: str) -> bool:
        cleaned = re.sub(r"^(\{\})+", "", url)
        return cleaned.startswith("/") or cleaned.startswith("http")

    @staticmethod
    def _fetch_method(options: Node | None) -> str:
        if options is not None and options.type == "object":
            for pair in options.named_children:
                if pair.type == "pair" and _t(pair.child_by_field_name("key")).strip("'\"") == "method":
                    value = pair.child_by_field_name("value")
                    if value is not None and value.type == "string":
                        return _t(value)[1:-1].upper()
                    return "ANY"
        return "GET" if options is None or options.type == "object" else "ANY"

    def _url_text(self, node: Node, consts: dict[str, str]) -> str | None:
        """URL 식을 문자열로 만든다. 값을 알 수 없는 부분은 {} 로 둔다."""
        if node.type == "string":
            return _t(node)[1:-1]
        if node.type == "template_string":
            out = ""
            for c in node.children:
                if c.type in ("string_fragment", "escape_sequence"):
                    out += _t(c)
                elif c.type == "template_substitution":
                    inner = c.named_children[0] if c.named_children else None
                    if inner is not None and inner.type == "identifier" and _t(inner) in consts:
                        out += consts[_t(inner)]
                    else:
                        out += "{}"
            return out
        if node.type == "binary_expression" and any(c.type == "+" for c in node.children):
            left = self._url_text(node.child_by_field_name("left"), consts)
            right = self._url_text(node.child_by_field_name("right"), consts)
            return (left if left is not None else "{}") + (right if right is not None else "{}")
        if node.type == "identifier":
            return consts.get(_t(node))
        if node.type == "parenthesized_expression" and node.named_children:
            return self._url_text(node.named_children[0], consts)
        return None

    # ── 서비스 전체를 보고 호출 연결 ─────────────────────────────
    def resolve(self, analyses: list[FileAnalysis], aliases: dict[str, str] | None = None) -> list[Edge]:
        aliases = aliases or {}
        files = {a.file_path: a for a in analyses}

        def resolve_module(from_file: str, module: str) -> str | None:
            if module.startswith("."):
                base = posixpath.normpath(posixpath.join(posixpath.dirname(from_file), module))
            else:
                base = None
                for alias, target in aliases.items():
                    if module == alias or module.startswith(alias.rstrip("/") + "/"):
                        base = posixpath.normpath(posixpath.join(target, module[len(alias):].lstrip("/")))
                        break
                if base is None:
                    return None  # 외부 패키지
            for cand in (base, base + ".js", base + ".jsx", base + ".mjs", base + "/index.js", base + "/index.jsx"):
                if cand in files:
                    return cand
            return None

        def target(file_path: str, exported: str) -> str | None:
            a = files.get(file_path)
            if a is None:
                return None
            if exported == "default":
                exported = a.meta["exports_default"] or "default"
            return f"{file_path}#{exported}" if exported in a.meta["locals"] else None

        edges: dict[tuple[str, str, str], Edge] = {}

        def add(caller: str, callee: str | None, kind: str, file_path: str) -> None:
            if callee and caller != callee:
                edges.setdefault((caller, callee, kind), Edge(caller, callee, kind, file_path))

        for a in analyses:
            imports = a.meta["imports"]
            local = a.meta["locals"]
            for call in a.calls:
                if call.receiver is None:
                    if call.name in local:
                        add(call.caller, f"{a.file_path}#{call.name}", call.kind, a.file_path)
                    elif call.name in imports:
                        module, orig = imports[call.name]
                        mod_file = resolve_module(a.file_path, module)
                        if mod_file:
                            add(call.caller, target(mod_file, orig), call.kind, a.file_path)
                else:
                    dotted = f"{call.receiver}.{call.name}"
                    if dotted in local:
                        add(call.caller, f"{a.file_path}#{dotted}", call.kind, a.file_path)
                    elif call.receiver in imports:
                        module, orig = imports[call.receiver]
                        mod_file = resolve_module(a.file_path, module)
                        if not mod_file:
                            continue
                        if orig == "*":
                            add(call.caller, target(mod_file, call.name), call.kind, a.file_path)
                        else:
                            if orig == "default":
                                orig = files[mod_file].meta["exports_default"] or "default"
                            add(call.caller, target(mod_file, f"{orig}.{call.name}"), call.kind, a.file_path)
            for r in a.routes_called:
                add(r.symbol, r.key, "http", a.file_path)
        return list(edges.values())
