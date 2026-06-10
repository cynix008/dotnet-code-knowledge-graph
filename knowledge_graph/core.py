"""Parsing, type modeling, and classification for C# source files."""

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path


CONTROLLER_BASES = {
    "Controller", "ControllerBase", "ApiController", "ODataController",
}
VIEW_COMPONENT_BASES = {"ViewComponent"}
PAGE_MODEL_BASES = {"PageModel"}
MEDIATOR_REQUEST_IFACES = {
    "IRequest", "ICommand", "IQuery", "IStreamRequest",
}
MEDIATOR_HANDLER_IFACES = {
    "IRequestHandler", "ICommandHandler", "IQueryHandler",
    "INotificationHandler", "IStreamRequestHandler",
}
MODEL_INDICATORS = {
    "Model", "ViewModel", "Dto", "Response", "Request", "Command", "Event",
}
MODEL_FOLDERS = {
    "Models", "ViewModels", "Dtos", "Contracts", "Events", "Requests",
    "Responses",
}
INFRA_INTERFACES = {
    "IDisposable", "IAsyncDisposable", "IEquatable", "IComparable",
    "IEnumerable", "IEnumerator", "ICollection", "IList",
    "ICloneable", "IFormattable", "IConvertible", "ISerializable",
}

RE_SLN_PROJECT = re.compile(
    r'Project\("\{[^}]+\}"\)\s*=\s*"([^"]+)",\s*"([^"]+)",\s*"\{[^}]+\}"'
)
RE_NAMESPACE = re.compile(r"namespace\s+([\w.]+)")
RE_CLASS = re.compile(
    r"(?:public|internal)\s+"
    r"(?:(?:abstract|sealed|static|partial|readonly|new|unsafe)\s+)*"
    r"class\s+(\w+)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s*:\s*(.+?))?"
    r"\s*(?:where\b|{)",
    re.MULTILINE,
)
RE_RECORD = re.compile(
    r"(?:public|internal)\s+"
    r"(?:(?:abstract|sealed|partial)\s+)*"
    r"record(?:\s+struct|\s+class)?\s+(\w+)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s*\([^)]*\))?"
    r"(?:\s*:\s*(.+?))?"
    r"\s*[;{]",
    re.MULTILINE,
)
RE_INTERFACE = re.compile(
    r"(?:public|internal)\s+"
    r"(?:partial\s+)?"
    r"interface\s+(\w+)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s*:\s*(.+?))?"
    r"\s*{",
    re.MULTILINE,
)
RE_CTOR = re.compile(
    r"(?:public|protected|internal)\s+(\w+)\s*\((.*?)\)\s*"
    r"(?::\s*(?:base|this)\s*\([^)]*\)\s*)?{",
    re.DOTALL,
)
RE_ROUTE_ATTR = re.compile(r'\[Route\(\s*"([^"]+)"\s*\)\]')
RE_HTTP_ATTR = re.compile(
    r'\[Http(?:Get|Post|Put|Delete|Patch)\(\s*"([^"]+)"\s*\)\]'
)
RE_MEDIATOR_SEND = re.compile(r"\.Send\w*\s*[<(]\s*(?:new\s+)?(\w+)")
RE_MEDIATOR_PUBLISH = re.compile(
    r"\.Publish\w*\s*[<(]\s*(?:new\s+)?(\w+)"
)
RE_VC_TAG = re.compile(r"<vc:([a-z][a-z0-9-]*)")
RE_PAGE_DIRECTIVE = re.compile(r'@page\s+"([^"]*)"')
RE_MODEL_DIRECTIVE = re.compile(r"@model\s+(\S+)")
RE_ADD_PAGE_ROUTE = re.compile(
    r'AddPageRoute\(\s*"([^"]+)"\s*,\s*\n?\s*"([^"]+)"\s*\)'
)
RE_FEATURE_FOLDER = re.compile(
    r"[/\\](?:Features|Areas|Modules|Domain)[/\\](\w+)[/\\]",
    re.IGNORECASE,
)


def to_kebab(name: str) -> str:
    """Convert PascalCase or camelCase to kebab-case."""
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    return value.lower().strip("-")


def read_text(path: Path) -> str:
    """Read text while tolerating BOMs and legacy encodings."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def strip_comments(text: str) -> str:
    """Remove C-style comments using the generator's best-effort behavior."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def generic_root(type_name: str) -> str:
    """Return the non-generic root of a C# type name."""
    index = type_name.find("<")
    return type_name[:index].strip() if index > 0 else type_name.strip()


def split_top_level(value: str, delimiter: str = ",") -> list:
    """Split a C# list while preserving nested generic arguments."""
    parts = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif char == delimiter and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def generic_args(type_name: str) -> list:
    """Return generic arguments from a C# type name."""
    match = re.search(r"<(.+)>", type_name)
    return split_top_level(match.group(1)) if match else []


def type_contract_key(type_name: str) -> tuple:
    """Return root name and generic arity for contract matching."""
    return generic_root(type_name), len(generic_args(type_name))


def vc_tag_to_pascal(tag: str) -> str:
    """Convert a kebab-case ViewComponent tag to PascalCase."""
    return "".join(part.capitalize() for part in tag.split("-"))


def parse_solution(sln_path: Path) -> list:
    """Parse C# projects from a solution file."""
    projects = []
    for match in RE_SLN_PROJECT.finditer(read_text(sln_path)):
        name, relative_path = match.group(1), match.group(2)
        normalized = relative_path.replace("\\", os.sep).replace("/", os.sep)
        full_path = sln_path.parent / normalized
        if full_path.suffix.lower() == ".csproj" and full_path.exists():
            projects.append({
                "name": name,
                "relative_path": relative_path,
                "csproj_path": full_path,
                "project_dir": full_path.parent,
            })
    return projects


def parse_csproj(csproj_path: Path) -> dict:
    """Parse project metadata and references from a .csproj file."""
    info = {
        "sdk": None,
        "target_framework": None,
        "output_type": None,
        "project_refs": [],
        "package_refs": [],
    }
    try:
        root = ET.parse(csproj_path).getroot()
        namespace = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        info["sdk"] = root.attrib.get("Sdk", "")
        for group in root.iter(f"{namespace}PropertyGroup"):
            for framework in group.iter(f"{namespace}TargetFramework"):
                info["target_framework"] = framework.text
            for frameworks in group.iter(f"{namespace}TargetFrameworks"):
                info["target_framework"] = frameworks.text
            for output_type in group.iter(f"{namespace}OutputType"):
                info["output_type"] = (output_type.text or "").lower()
        for reference in root.iter(f"{namespace}ProjectReference"):
            include = reference.attrib.get("Include", "")
            info["project_refs"].append(Path(include.replace("\\", "/")).stem)
        for reference in root.iter(f"{namespace}PackageReference"):
            info["package_refs"].append({
                "name": reference.attrib.get("Include", ""),
                "version": reference.attrib.get("Version", ""),
            })
    except ET.ParseError:
        pass
    return info


class ClassInfo:
    """Information collected about one C# class, record, or interface."""

    __slots__ = (
        "name", "namespace", "base_list", "bases", "interfaces",
        "ctor_params", "dispatches", "file_path", "feature",
        "project", "routes", "node_type", "is_interface",
    )

    def __init__(self, name: str, namespace: str, base_list: str,
                 file_path: str, project: str, feature: str):
        self.name = name
        self.namespace = namespace
        self.base_list = base_list
        self.file_path = file_path
        self.project = project
        self.feature = feature
        self.routes = []
        self.dispatches = []
        self.ctor_params = []
        self.node_type = None
        self.is_interface = False
        self.bases = []
        self.interfaces = []
        for part in split_top_level(base_list) if base_list else []:
            root = generic_root(part)
            target = (
                self.interfaces
                if root.startswith("I") and root[1:2].isupper()
                else self.bases
            )
            target.append(part)

    def __repr__(self):
        return f"ClassInfo({self.name}, type={self.node_type})"


def _detect_feature(file_path: Path, project_dir: Path = None) -> str:
    match = RE_FEATURE_FOLDER.search(str(file_path))
    if match:
        return match.group(1)
    if project_dir:
        try:
            parts = file_path.relative_to(project_dir).parts
            if len(parts) > 1 and parts[0].lower() not in {
                "bin", "obj", "properties", "wwwroot",
                ".codex_bin", ".codex_obj",
            }:
                return parts[0]
        except ValueError:
            pass
    return ""


def _source_declarations(clean_source: str) -> list:
    declarations = []
    for kind, pattern in (
        ("class", RE_CLASS),
        ("record", RE_RECORD),
        ("interface", RE_INTERFACE),
    ):
        for match in pattern.finditer(clean_source):
            declarations.append(
                (kind, match.group(1), match.group(2) or "", match.start())
            )
    return sorted(declarations, key=lambda declaration: declaration[3])


def _outer_classes_by_position(clean_source: str, declarations: list) -> dict:
    class_scope_stack = []
    brace_depth = 0
    declaration_index = 0
    outer_classes = {}
    for position, char in enumerate(clean_source):
        while (
            declaration_index < len(declarations)
            and declarations[declaration_index][3] <= position
        ):
            declaration = declarations[declaration_index]
            outer_classes[declaration[3]] = (
                ".".join(scope[0] for scope in class_scope_stack)
                if class_scope_stack else ""
            )
            declaration_index += 1
        if char == "{":
            recent = next(
                (item for item in reversed(declarations) if item[3] < position),
                None,
            )
            open_positions = {scope[2] for scope in class_scope_stack}
            if (
                recent
                and recent[3] not in open_positions
                and clean_source[recent[3]:position].count("{") == 0
            ):
                full_name = recent[1]
                if class_scope_stack:
                    full_name = (
                        ".".join(scope[0] for scope in class_scope_stack)
                        + "."
                        + full_name
                    )
                class_scope_stack.append(
                    (full_name, brace_depth, recent[3])
                )
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
            if (
                class_scope_stack
                and brace_depth == class_scope_stack[-1][1]
            ):
                class_scope_stack.pop()
    return outer_classes


def parse_cs_file(file_path: Path, project_name: str,
                  project_dir: Path = None) -> list:
    """Parse classes, records, interfaces, routes, dispatches, and DI."""
    source = read_text(file_path)
    if not source:
        return []
    clean = strip_comments(source)
    namespace_match = RE_NAMESPACE.search(clean)
    namespace = namespace_match.group(1) if namespace_match else ""
    feature = _detect_feature(file_path, project_dir)
    routes = RE_ROUTE_ATTR.findall(clean) + RE_HTTP_ATTR.findall(clean)
    dispatches = (
        RE_MEDIATOR_SEND.findall(clean) + RE_MEDIATOR_PUBLISH.findall(clean)
    )
    declarations = _source_declarations(clean)
    outer_classes = _outer_classes_by_position(clean, declarations)
    results = []
    for kind, name, base_list, position in declarations:
        outer = outer_classes.get(position, "")
        full_name = f"{outer}.{name}" if outer else name
        info = ClassInfo(
            full_name, namespace, base_list, str(file_path),
            project_name, feature,
        )
        if kind == "interface":
            info.is_interface = True
        else:
            info.routes = routes
            info.dispatches = dispatches
        results.append(info)
    for match in RE_CTOR.finditer(clean):
        constructor_name = match.group(1)
        parameters = match.group(2).strip()
        if not parameters:
            continue
        target = next(
            (
                info for info in results
                if info.name.rsplit(".", 1)[-1] == constructor_name
                and not info.is_interface
            ),
            None,
        )
        if not target:
            continue
        for parameter in split_top_level(parameters):
            parts = parameter.split()
            if len(parts) >= 2:
                target.ctor_params.append(
                    (" ".join(parts[:-1]), parts[-1])
                )
    return results


def parse_cshtml_file(file_path: Path, project_name: str,
                      project_dir: Path = None) -> dict:
    """Parse ViewComponent tags and Razor metadata from a .cshtml file."""
    source = read_text(file_path)
    if not source:
        return None
    models = RE_MODEL_DIRECTIVE.findall(source)
    return {
        "file_path": str(file_path),
        "file_name": file_path.stem,
        "project": project_name,
        "feature": _detect_feature(file_path, project_dir),
        "vc_tags": RE_VC_TAG.findall(source),
        "page_routes": RE_PAGE_DIRECTIVE.findall(source),
        "model_type": models[0] if models else None,
    }


def parse_route_registrations(file_path: Path) -> list:
    """Parse AddPageRoute registrations from C# source."""
    source = read_text(file_path)
    if not source:
        return []
    return [
        {"page": match.group(1), "route": match.group(2)}
        for match in RE_ADD_PAGE_ROUTE.finditer(strip_comments(source))
    ]


def classify_type(info: ClassInfo) -> str:
    """Classify a parsed C# type into a graph node type."""
    if info.is_interface:
        return "interface"
    base_roots = {generic_root(base) for base in info.bases}
    interface_roots = {
        generic_root(interface) for interface in info.interfaces
    }
    if base_roots & CONTROLLER_BASES:
        return "controller"
    if base_roots & VIEW_COMPONENT_BASES:
        return "viewComponent"
    if base_roots & PAGE_MODEL_BASES:
        return "razorView"
    if interface_roots & MEDIATOR_HANDLER_IFACES:
        return "messageHandler"
    if interface_roots & MEDIATOR_REQUEST_IFACES:
        return "query"
    if set(Path(info.file_path).parts) & MODEL_FOLDERS:
        return "model"
    if any(
        info.name.endswith(indicator) and not info.name.startswith("I")
        for indicator in MODEL_INDICATORS
    ):
        return "model"
    custom_interfaces = [
        interface for interface in info.interfaces
        if generic_root(interface) not in INFRA_INTERFACES
    ]
    if custom_interfaces and not info.name.endswith("Exception"):
        return "service"
    if len(info.ctor_params) >= 2:
        return "service"
    return None
