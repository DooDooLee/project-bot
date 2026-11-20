"""Java(Spring) 처리기. tree-sitter로 AST를 만들어 청크, 호출, API 경로를 뽑는다."""
from __future__ import annotations

import re

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from .base import (
    Chunk,
    Edge,
    FileAnalysis,
    Frame,
    LanguageAdapter,
    RawCall,
    Route,
    join_paths,
    normalize_path,
)

_LANG = Language(tree_sitter_java.language())

_TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
_MAPPING = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "ANY",
    "MessageMapping": "MESSAGE",
}
_SELF = "@self"
_SUPER = "@super"

# at com.example.Foo$Bar.method(Foo.java:42)
# 모듈 접두어(java.base/, app//)가 붙은 줄도 받는다
_FRAME = re.compile(r"^\s*at\s+(?:[\w.@-]*/)*([\w.$]+)\.([\w$<>]+)\(([\w$.-]+\.(?:java|kt)):(\d+)\)", re.M)


def _t(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _base_type(type_text: str) -> str:
    """List<Room> → List, Room[] → Room, a.b.Room → a.b.Room"""
    s = re.sub(r"\s+", "", type_text)
    # 제네릭 제거 (중첩 포함)
    while "<" in s:
        new = re.sub(r"<[^<>]*>", "", s)
        if new == s:
            break
        s = new
    s = s.replace("...", "").replace("[]", "")
    s = re.sub(r"@\w+", "", s)
    return s


def _param_type(type_text: str, varargs: bool = False) -> str:
    s = re.sub(r"\s+", "", type_text)
    while "<" in s:
        new = re.sub(r"<[^<>]*>", "", s)
        if new == s:
            break
        s = new
    s = re.sub(r"@\w+", "", s)
    s = s.rsplit(".", 1)[-1] if "." in s and not s.endswith("...") else s
    return s + ("..." if varargs else "")


def _walk(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _modifiers(node: Node) -> Node | None:
    for c in node.children:
        if c.type == "modifiers":
            return c
    return None


def _annotations(node: Node) -> list[Node]:
    mods = _modifiers(node)
    if mods is None:
        return []
    return [c for c in mods.children if c.type in ("annotation", "marker_annotation")]


def _string_values(node: Node | None, consts: dict[str, str]) -> list[str]:
    """어노테이션 값에서 문자열을 뽑는다. "a", {"a","b"}, 상수 이름을 처리한다."""
    if node is None:
        return []
    if node.type == "string_literal":
        return [_t(node)[1:-1]]
    if node.type == "element_value_array_initializer":
        out: list[str] = []
        for c in node.named_children:
            out.extend(_string_values(c, consts))
        return out
    if node.type == "identifier" and _t(node) in consts:
        return [consts[_t(node)]]
    if node.type == "binary_expression":
        left = _string_values(node.child_by_field_name("left"), consts)
        right = _string_values(node.child_by_field_name("right"), consts)
        if left and right:
            return [left[0] + right[0]]
    return []


def _annotation_args(ann: Node, consts: dict[str, str]) -> dict[str, list[str]]:
    """{'value': [...], 'method': [...]} 형태로 돌려준다."""
    out: dict[str, list[str]] = {}
    args = ann.child_by_field_name("arguments")
    if args is None:
        return out
    for c in args.named_children:
        if c.type == "element_value_pair":
            key = _t(c.child_by_field_name("key"))
            value = c.child_by_field_name("value")
            if key == "method":
                out["method"] = re.findall(r"RequestMethod\.(\w+)", _t(value))
            else:
                out[key] = _string_values(value, consts)
        else:
            out.setdefault("value", []).extend(_string_values(c, consts))
    return out


def _mapping_paths(args: dict[str, list[str]]) -> list[str]:
    paths = args.get("value") or args.get("path") or []
    return paths or [""]


class JavaAdapter(LanguageAdapter):
    language = "java"
    suffixes = (".java",)

    def __init__(self) -> None:
        self._parser = Parser(_LANG)

    # ── 인터페이스의 네 가지 메서드 ─────────────────────────────
    def extract_chunks(self, source: bytes, file_path: str) -> list[Chunk]:
        return self.analyze(source, file_path).chunks

    def extract_calls(self, source: bytes, file_path: str) -> list[RawCall]:
        return self.analyze(source, file_path).calls

    def extract_routes(self, source: bytes, file_path: str) -> tuple[list[Route], list[Route]]:
        a = self.analyze(source, file_path)
        return a.routes_served, a.routes_called

    def parse_stack_trace(self, text: str, base_packages: list[str] | None = None) -> list[Frame]:
        frames: list[Frame] = []
        for m in _FRAME.finditer(text):
            cls, method, file, line = m.group(1), m.group(2), m.group(3), int(m.group(4))
            if base_packages and not any(cls.startswith(p) for p in base_packages):
                continue
            # 프록시(RoomService$$SpringCGLIB$$0)와 람다(lambda$findRoom$0)를 원래 이름으로
            cls = re.sub(r"\$\$.*$", "", cls)
            lam = re.match(r"lambda\$(\w+)\$\d+", method)
            if lam:
                method = lam.group(1)
            cls = cls.replace("$", ".")
            cls = re.sub(r"\.\d+$", "", cls)  # 익명 클래스 Foo$1
            frames.append(
                Frame(symbol=f"{cls}.{method}", file=file, line=line, class_name=cls, method=method)
            )
        return frames

    # ── 한 번의 파싱으로 전부 뽑기 ─────────────────────────────
    def analyze(self, source: bytes, file_path: str) -> FileAnalysis:
        root = self._parser.parse(source).root_node
        package = ""
        imports: dict[str, str] = {}
        wildcards: list[str] = []
        for c in root.named_children:
            if c.type == "package_declaration":
                package = _t(c).replace("package", "").replace(";", "").strip()
            elif c.type == "import_declaration":
                txt = _t(c).replace("import", "", 1).replace(";", "").strip()
                if txt.startswith("static "):
                    continue
                if txt.endswith(".*"):
                    wildcards.append(txt[:-2])
                else:
                    imports[txt.rsplit(".", 1)[-1]] = txt
        analysis = FileAnalysis(
            file_path=file_path,
            meta={
                "package": package,
                "imports": imports,
                "wildcards": wildcards,
                "classes": {},  # fqcn → 정보
                "owner": {},  # 청크 qualified_name → 클래스 fqcn
            },
        )
        for c in root.named_children:
            if c.type in _TYPE_DECLS:
                self._visit_type(c, [], source, analysis)
        return analysis

    def _visit_type(self, node: Node, outer: list[str], source: bytes, a: FileAnalysis) -> None:
        name = _t(node.child_by_field_name("name"))
        if not name:
            return
        simple = ".".join(outer + [name])
        package = a.meta["package"]
        fqcn = f"{package}.{simple}" if package else simple
        kind = _TYPE_DECLS[node.type]
        body = node.child_by_field_name("body")
        if body is None:
            return

        supers: list[str] = []
        for fname in ("superclass", "interfaces"):
            sup = node.child_by_field_name(fname)
            if sup is not None:
                supers.extend(_base_type(_t(t)) for t in _walk(sup) if t.type in ("type_identifier", "scoped_type_identifier") and t.parent.type != "scoped_type_identifier" and t.parent.type != "type_arguments")
        for c in node.children:
            if c.type == "extends_interfaces":
                supers.extend(_base_type(_t(t)) for t in _walk(c) if t.type == "type_identifier" and t.parent.type != "type_arguments")

        members = list(body.named_children)
        for c in body.named_children:
            if c.type == "enum_body_declarations":
                members.extend(c.named_children)

        # 필드 타입과 문자열 상수
        fields: dict[str, str] = {}
        consts: dict[str, str] = {}
        field_lines: list[str] = []
        for m in members:
            if m.type == "field_declaration":
                ftype = _base_type(_t(m.child_by_field_name("type")))
                for d in m.named_children:
                    if d.type == "variable_declarator":
                        fname = _t(d.child_by_field_name("name"))
                        fields[fname] = ftype
                        value = d.child_by_field_name("value")
                        if value is not None and value.type == "string_literal":
                            consts[fname] = _t(value)[1:-1]
                field_lines.append(" ".join(_t(m).split()))
        if node.type == "record_declaration":
            params = node.child_by_field_name("parameters")
            if params is not None:
                for p in params.named_children:
                    if p.type == "formal_parameter":
                        fields[_t(p.child_by_field_name("name"))] = _base_type(_t(p.child_by_field_name("type")))

        prefix = ""
        for ann in _annotations(node):
            if _t(ann.child_by_field_name("name")) == "RequestMapping":
                prefix = _mapping_paths(_annotation_args(ann, consts))[0]

        info = {"simple": simple, "kind": kind, "supers": supers, "methods": {}, "file": a.file_path}
        a.meta["classes"][fqcn] = info

        signatures: list[str] = []
        for m in members:
            if m.type in ("method_declaration", "constructor_declaration"):
                chunk = self._method_chunk(m, simple, fqcn, a.file_path, source)
                a.chunks.append(chunk)
                a.meta["owner"][chunk.qualified_name] = fqcn
                mname = _t(m.child_by_field_name("name"))
                argc = self._argc(m)
                info["methods"].setdefault(mname, []).append((argc, chunk.qualified_name))
                signatures.append(chunk.signature)
                mbody = m.child_by_field_name("body")
                if mbody is not None:
                    a.calls.extend(self._calls(m, mbody, chunk.qualified_name, fields))
                a.routes_served.extend(self._routes(m, chunk.qualified_name, prefix, consts))
            elif m.type in _TYPE_DECLS:
                self._visit_type(m, outer + [name], source, a)

        # 클래스 요약 청크: 선언부 + 필드 + 메서드 시그니처
        header = " ".join(source[node.start_byte : body.start_byte].decode("utf-8", "replace").split())
        summary = header + " {\n" + "".join(f"    {f}\n" for f in field_lines)
        summary += "".join(f"    {s};\n" for s in signatures) + "}"
        a.chunks.append(
            Chunk(
                qualified_name=fqcn,
                name=simple,
                kind="class",
                file_path=a.file_path,
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                signature=header,
                content=summary,
            )
        )

    @staticmethod
    def _params(method: Node) -> list[tuple[str, str]]:
        """[(파라미터 타입 표시, 이름)]"""
        out: list[tuple[str, str]] = []
        params = method.child_by_field_name("parameters")
        if params is None:
            return out
        for p in params.named_children:
            if p.type == "formal_parameter":
                out.append((_param_type(_t(p.child_by_field_name("type"))), _t(p.child_by_field_name("name"))))
            elif p.type == "spread_parameter":
                ptype = next((c for c in p.named_children if c.type != "variable_declarator" and c.type != "modifiers"), None)
                pname = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                out.append((_param_type(_t(ptype), varargs=True), _t(pname.child_by_field_name("name")) if pname else ""))
        return out

    def _argc(self, method: Node) -> int:
        return len(self._params(method))

    def _method_chunk(self, m: Node, simple: str, fqcn: str, file_path: str, src: bytes) -> Chunk:
        mname = _t(m.child_by_field_name("name"))
        ptypes = ",".join(t for t, _ in self._params(m))
        body = m.child_by_field_name("body")
        # 바로 위에 붙은 주석(Javadoc)도 함께 담는다
        start = m
        prev = m.prev_named_sibling
        while prev is not None and prev.type in ("block_comment", "line_comment") and start.start_point.row - prev.end_point.row <= 1:
            start = prev
            prev = prev.prev_named_sibling
        content = src[start.start_byte : m.end_byte].decode("utf-8", "replace")
        sig_end = body.start_byte if body is not None else m.end_byte
        signature = " ".join(src[m.start_byte : sig_end].decode("utf-8", "replace").split()).rstrip(";")
        return Chunk(
            qualified_name=f"{fqcn}#{mname}({ptypes})",
            name=f"{simple}#{mname}({ptypes})",
            kind="constructor" if m.type == "constructor_declaration" else "method",
            file_path=file_path,
            start_line=start.start_point.row + 1,
            end_line=m.end_point.row + 1,
            signature=signature,
            content=content,
        )

    def _calls(self, method: Node, body: Node, caller: str, fields: dict[str, str]) -> list[RawCall]:
        local: dict[str, str] = {name: _base_type(t) for t, name in self._params(method)}
        calls: list[RawCall] = []
        nodes = list(_walk(body))
        for n in nodes:
            if n.type == "local_variable_declaration":
                vtype = _base_type(_t(n.child_by_field_name("type")))
                for d in n.named_children:
                    if d.type == "variable_declarator":
                        local[_t(d.child_by_field_name("name"))] = vtype
            elif n.type in ("enhanced_for_statement", "catch_formal_parameter", "resource"):
                vtype = n.child_by_field_name("type")
                vname = n.child_by_field_name("name")
                if vtype is not None and vname is not None:
                    local[_t(vname)] = _base_type(_t(vtype))
        for n in nodes:
            if n.type != "method_invocation":
                continue
            name = _t(n.child_by_field_name("name"))
            obj = n.child_by_field_name("object")
            args = n.child_by_field_name("arguments")
            argc = len(args.named_children) if args is not None else 0
            receiver: str | None
            if obj is None or obj.type == "this":
                receiver = _SELF
            elif obj.type == "super":
                receiver = _SUPER
            elif obj.type == "identifier":
                ident = _t(obj)
                if ident in local:
                    receiver = local[ident]
                elif ident in fields:
                    receiver = fields[ident]
                elif ident[:1].isupper():
                    receiver = ident  # 정적 호출
                else:
                    receiver = None
            elif obj.type == "field_access":
                target = obj.child_by_field_name("object")
                fname = _t(obj.child_by_field_name("field"))
                receiver = fields.get(fname) if target is not None and target.type == "this" else None
            else:
                receiver = None  # 체이닝 등
            if receiver == "var":
                receiver = None
            calls.append(RawCall(caller=caller, name=name, receiver=receiver, argc=argc, line=n.start_point.row + 1))
        return calls

    def _routes(self, m: Node, symbol: str, prefix: str, consts: dict[str, str]) -> list[Route]:
        routes: list[Route] = []
        for ann in _annotations(m):
            aname = _t(ann.child_by_field_name("name"))
            if aname not in _MAPPING:
                continue
            args = _annotation_args(ann, consts) if ann.type == "annotation" else {}
            methods = args.get("method") or [_MAPPING[aname]]
            for path in _mapping_paths(args):
                full = join_paths(prefix if aname != "MessageMapping" else "", path)
                for http in methods:
                    routes.append(
                        Route(method=http, path=normalize_path(full), symbol=symbol, raw_path=full, line=m.start_point.row + 1)
                    )
        return routes

    # ── 서비스 전체를 보고 호출 연결 ─────────────────────────────
    def resolve(self, analyses: list[FileAnalysis]) -> list[Edge]:
        classes: dict[str, dict] = {}
        by_simple: dict[str, list[str]] = {}
        for a in analyses:
            for fqcn, info in a.meta["classes"].items():
                classes[fqcn] = {**info, "meta": a.meta}
                by_simple.setdefault(info["simple"].rsplit(".", 1)[-1], []).append(fqcn)
                by_simple.setdefault(info["simple"], []).append(fqcn)

        def resolve_type(type_name: str, meta: dict, current: str | None = None) -> str | None:
            if not type_name:
                return None
            if type_name in classes:
                return type_name
            head = type_name.split(".", 1)[0]
            rest = type_name[len(head):]
            imp = meta["imports"].get(head)
            if imp and imp + rest in classes:
                return imp + rest
            pkg = meta["package"]
            cand = f"{pkg}.{type_name}" if pkg else type_name
            if cand in classes:
                return cand
            if current:  # 같은 클래스 안의 중첩 타입
                outer = current
                while outer:
                    if f"{outer}.{type_name}" in classes:
                        return f"{outer}.{type_name}"
                    outer = outer.rsplit(".", 1)[0] if "." in outer and outer.rsplit(".", 1)[0] in classes else ""
            for w in meta["wildcards"]:
                if f"{w}.{type_name}" in classes:
                    return f"{w}.{type_name}"
            if imp:
                return None  # 외부 라이브러리 타입
            matches = set(by_simple.get(type_name, []))
            return next(iter(matches)) if len(matches) == 1 else None

        def supers_of(fqcn: str) -> list[str]:
            info = classes.get(fqcn)
            if not info:
                return []
            out = []
            for s in info["supers"]:
                r = resolve_type(s, info["meta"], fqcn)
                if r:
                    out.append(r)
            return out

        implementers: dict[str, list[str]] = {}
        for fqcn in classes:
            for s in supers_of(fqcn):
                implementers.setdefault(s, []).append(fqcn)

        def find_methods(fqcn: str, name: str, argc: int | None, seen: set[str] | None = None) -> list[str]:
            seen = seen or set()
            if fqcn in seen or fqcn not in classes:
                return []
            seen.add(fqcn)
            cands = classes[fqcn]["methods"].get(name, [])
            exact = [q for n, q in cands if argc is None or n == argc]
            found = exact or [q for _, q in cands]
            if found:
                return found
            for s in supers_of(fqcn):
                r = find_methods(s, name, argc, seen)
                if r:
                    return r
            return []

        def with_overrides(fqcn: str, name: str, argc: int | None, seen: set[str] | None = None) -> list[str]:
            """인터페이스·부모 타입으로 호출하면 구현 클래스의 같은 메서드도 대상이 된다."""
            seen = seen or set()
            out: list[str] = []
            for impl in implementers.get(fqcn, []):
                if impl in seen:
                    continue
                seen.add(impl)
                cands = classes[impl]["methods"].get(name, [])
                out.extend(q for n, q in cands if argc is None or n == argc)
                out.extend(with_overrides(impl, name, argc, seen))
            return out

        edges: dict[tuple[str, str, str], Edge] = {}

        def add(caller: str, callee: str, kind: str, file_path: str) -> None:
            if caller != callee:
                edges.setdefault((caller, callee, kind), Edge(caller, callee, kind, file_path))

        for a in analyses:
            for call in a.calls:
                owner = a.meta["owner"].get(call.caller)
                if call.receiver is None or owner is None:
                    continue
                if call.receiver == _SELF:
                    target = owner
                elif call.receiver == _SUPER:
                    sup = supers_of(owner)
                    target = sup[0] if sup else None
                else:
                    target = resolve_type(call.receiver, a.meta, owner)
                if not target:
                    continue
                for callee in find_methods(target, call.name, call.argc):
                    add(call.caller, callee, "call", a.file_path)
                for callee in with_overrides(target, call.name, call.argc):
                    add(call.caller, callee, "call", a.file_path)
            for r in a.routes_served:
                add(r.key, r.symbol, "route", a.file_path)
        return list(edges.values())
