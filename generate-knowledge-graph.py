#!/usr/bin/env python3
"""
Knowledge Graph Generator for C# Solutions

Analyzes a .NET/C# solution (.sln) and generates a knowledge-graph.json file
suitable for visualization with the Knowledge Graph Viewer.

Usage:
    python generate-knowledge-graph.py
    python generate-knowledge-graph.py --solution path/to/Solution.sln
    python generate-knowledge-graph.py --solution path/to/Solution.sln --output graph.json

Detected artifacts:
    Projects, Controllers, ViewComponents, Razor Pages (PageModel),
    Services (interface+impl), MediatR queries/commands & handlers,
    Constructor DI, MediatR dispatch chains, <vc:> rendering,
    Feature-folder groupings, Models/DTOs, Route attributes.

No external dependencies — Python 3.8+ standard library only.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def to_kebab(name: str) -> str:
    """PascalCase / camelCase → kebab-case."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s)
    return s.lower().strip("-")


def read_text(path: Path) -> str:
    """Read a text file, handling BOM."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def strip_comments(text: str) -> str:
    """Remove C-style comments (// and /* */) from source text."""
    # Remove multi-line comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Remove single-line comments (but not inside strings — best effort)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def generic_root(type_name: str) -> str:
    """Extract root type from a possibly-generic name: IOptions<Foo> → IOptions."""
    idx = type_name.find("<")
    return type_name[:idx].strip() if idx > 0 else type_name.strip()


def generic_args(type_name: str) -> list:
    """Extract generic arguments: IRequestHandler<Q, R> → ['Q', 'R']."""
    m = re.search(r"<(.+)>", type_name)
    if not m:
        return []
    # Simple split on comma (doesn't handle nested generics perfectly)
    return [a.strip() for a in m.group(1).split(",")]


def vc_tag_to_pascal(tag: str) -> str:
    """Convert kebab vc tag name to PascalCase class name.
    unified-search → UnifiedSearch
    """
    return "".join(part.capitalize() for part in tag.split("-"))


# ═══════════════════════════════════════════════════════════════
# Regex Patterns
# ═══════════════════════════════════════════════════════════════

RE_SLN_PROJECT = re.compile(
    r'Project\("\{[^}]+\}"\)\s*=\s*"([^"]+)",\s*"([^"]+)",\s*"\{[^}]+\}"'
)

RE_NAMESPACE = re.compile(r"namespace\s+([\w.]+)")

# Matches class / abstract class / sealed class / partial class / static class
RE_CLASS = re.compile(
    r"(?:public|internal)\s+"
    r"(?:(?:abstract|sealed|static|partial|readonly|new|unsafe)\s+)*"
    r"class\s+(\w+)"
    r"(?:\s*<[^>]*>)?"          # optional generic params on class itself
    r"(?:\s*:\s*(.+?))?"        # optional base list
    r"\s*(?:where\b|{)",
    re.MULTILINE,
)

# Records (C# 9+): public record FooCommand(...) : IRequest<R>;
RE_RECORD = re.compile(
    r"(?:public|internal)\s+"
    r"(?:(?:abstract|sealed|partial)\s+)*"
    r"record(?:\s+struct|\s+class)?\s+(\w+)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s*\([^)]*\))?"        # positional params
    r"(?:\s*:\s*(.+?))?"
    r"\s*[;{]",
    re.MULTILINE,
)

# Interface declaration
RE_INTERFACE = re.compile(
    r"(?:public|internal)\s+"
    r"(?:partial\s+)?"
    r"interface\s+(\w+)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s*:\s*(.+?))?"
    r"\s*{",
    re.MULTILINE,
)

# Constructor — matches possibly multi-line parameter list
RE_CTOR = re.compile(
    r"(?:public|protected|internal)\s+(\w+)\s*\((.*?)\)\s*(?::\s*(?:base|this)\s*\([^)]*\)\s*)?{",
    re.DOTALL,
)

# [Route("...")] attribute
RE_ROUTE_ATTR = re.compile(r'\[Route\(\s*"([^"]+)"\s*\)\]')
RE_HTTP_ATTR = re.compile(r'\[Http(?:Get|Post|Put|Delete|Patch)\(\s*"([^"]+)"\s*\)\]')

# MediatR Send / Publish
RE_MEDIATOR_SEND = re.compile(r"\.Send\w*\s*[<(]\s*(?:new\s+)?(\w+)")
RE_MEDIATOR_PUBLISH = re.compile(r"\.Publish\w*\s*[<(]\s*(?:new\s+)?(\w+)")

# Razor <vc:tag-name>
RE_VC_TAG = re.compile(r"<vc:([a-z][a-z0-9-]*)")

# @page directive
RE_PAGE_DIRECTIVE = re.compile(r'@page\s+"([^"]*)"')
RE_MODEL_DIRECTIVE = re.compile(r"@model\s+(\S+)")

# AddPageRoute in route registrations
RE_ADD_PAGE_ROUTE = re.compile(
    r'AddPageRoute\(\s*"([^"]+)"\s*,\s*\n?\s*"([^"]+)"\s*\)'
)

# Feature folder patterns
RE_FEATURE_FOLDER = re.compile(
    r"[/\\](?:Features|Areas|Modules|Domain)[/\\](\w+)[/\\]", re.IGNORECASE
)

# ═══════════════════════════════════════════════════════════════
# Classification Constants
# ═══════════════════════════════════════════════════════════════

CONTROLLER_BASES = {"Controller", "ControllerBase", "ApiController", "ODataController"}
VIEW_COMPONENT_BASES = {"ViewComponent"}
PAGE_MODEL_BASES = {"PageModel"}

MEDIATOR_REQUEST_IFACES = {"IRequest", "ICommand", "IQuery", "IStreamRequest"}
MEDIATOR_HANDLER_IFACES = {
    "IRequestHandler", "ICommandHandler", "IQueryHandler",
    "INotificationHandler", "IStreamRequestHandler",
}

# Names/paths that hint at "model" types
MODEL_INDICATORS = {"Model", "ViewModel", "Dto", "Response", "Request", "Command", "Event"}
MODEL_FOLDERS = {"Models", "ViewModels", "Dtos", "Contracts", "Events", "Requests", "Responses"}

# Common infrastructure interfaces to skip as "service" nodes
INFRA_INTERFACES = {
    "IDisposable", "IAsyncDisposable", "IEquatable", "IComparable",
    "IEnumerable", "IEnumerator", "ICollection", "IList",
    "ICloneable", "IFormattable", "IConvertible", "ISerializable",
}


# ═══════════════════════════════════════════════════════════════
# Solution & Project Parsing
# ═══════════════════════════════════════════════════════════════

def parse_solution(sln_path: Path) -> list:
    """Parse .sln file → list of {name, relative_path, csproj_path}."""
    text = read_text(sln_path)
    projects = []
    sln_dir = sln_path.parent

    for m in RE_SLN_PROJECT.finditer(text):
        name, rel_path = m.group(1), m.group(2)
        # Normalize path separators
        rel_path_normalized = rel_path.replace("\\", os.sep).replace("/", os.sep)
        full_path = sln_dir / rel_path_normalized

        # Only include C# projects
        if full_path.suffix.lower() == ".csproj" and full_path.exists():
            projects.append({
                "name": name,
                "relative_path": rel_path,
                "csproj_path": full_path,
                "project_dir": full_path.parent,
            })

    return projects


def parse_csproj(csproj_path: Path) -> dict:
    """Parse .csproj → {sdk, target_framework, project_refs, package_refs, output_type}."""
    info = {
        "sdk": None,
        "target_framework": None,
        "output_type": None,
        "project_refs": [],
        "package_refs": [],
    }
    try:
        tree = ET.parse(csproj_path)
        root = tree.getroot()
        # Handle namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        info["sdk"] = root.attrib.get("Sdk", "")

        for pg in root.iter(f"{ns}PropertyGroup"):
            for tf in pg.iter(f"{ns}TargetFramework"):
                info["target_framework"] = tf.text
            for tf in pg.iter(f"{ns}TargetFrameworks"):
                info["target_framework"] = tf.text
            for ot in pg.iter(f"{ns}OutputType"):
                info["output_type"] = (ot.text or "").lower()

        for pr in root.iter(f"{ns}ProjectReference"):
            inc = pr.attrib.get("Include", "")
            # Extract project name from path
            ref_name = Path(inc.replace("\\", "/")).stem
            info["project_refs"].append(ref_name)

        for pr in root.iter(f"{ns}PackageReference"):
            pkg = pr.attrib.get("Include", "")
            ver = pr.attrib.get("Version", "")
            info["package_refs"].append({"name": pkg, "version": ver})

    except ET.ParseError:
        pass
    return info


# ═══════════════════════════════════════════════════════════════
# C# File Parsing
# ═══════════════════════════════════════════════════════════════

class ClassInfo:
    """Collected information about a single C# class/record."""
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
        self.ctor_params = []  # list of (type_name, param_name)
        self.node_type = None
        self.is_interface = False

        # Parse base list into bases and interfaces
        self.bases = []
        self.interfaces = []
        if base_list:
            for part in self.base_list.split(","):
                part = part.strip()
                root = generic_root(part)
                if root.startswith("I") and root[1:2].isupper():
                    self.interfaces.append(part)
                else:
                    self.bases.append(part)

    def __repr__(self):
        return f"ClassInfo({self.name}, type={self.node_type})"


def _detect_feature(file_path: Path, project_dir: Path = None) -> str:
    """Detect feature from folder path.
    
    First tries standard patterns (Features/, Areas/, Modules/, Domain/).
    Falls back to the first subfolder under project_dir when provided.
    """
    fm = RE_FEATURE_FOLDER.search(str(file_path))
    if fm:
        return fm.group(1)

    # Fallback: first subfolder under project root
    if project_dir:
        try:
            rel = file_path.relative_to(project_dir)
            parts = rel.parts
            if len(parts) > 1:  # has at least one subfolder
                folder = parts[0]
                # Skip common non-feature folders
                if folder.lower() not in (
                    "bin", "obj", "properties", "wwwroot",
                    ".codex_bin", ".codex_obj",
                ):
                    return folder
        except ValueError:
            pass
    return ""


def parse_cs_file(file_path: Path, project_name: str,
                  project_dir: Path = None) -> list:
    """Parse a .cs file and return list of ClassInfo objects."""
    text = read_text(file_path)
    if not text:
        return []

    clean = strip_comments(text)
    results = []

    # Detect namespace
    ns_match = RE_NAMESPACE.search(clean)
    namespace = ns_match.group(1) if ns_match else ""

    # Detect feature from folder path
    feature = _detect_feature(file_path, project_dir)

    # Detect route attributes (file-level for controllers)
    file_routes = RE_ROUTE_ATTR.findall(clean)
    file_http_routes = RE_HTTP_ATTR.findall(clean)

    # Detect MediatR dispatches
    file_dispatches = RE_MEDIATOR_SEND.findall(clean) + RE_MEDIATOR_PUBLISH.findall(clean)

    # ── Detect nested classes via brace-depth tracking ──
    # Collect all class/record/interface declarations with their positions
    all_decls = []
    for m in RE_CLASS.finditer(clean):
        all_decls.append(("class", m.group(1), m.group(2) or "", m.start()))
    for m in RE_RECORD.finditer(clean):
        all_decls.append(("record", m.group(1), m.group(2) or "", m.start()))
    for m in RE_INTERFACE.finditer(clean):
        all_decls.append(("interface", m.group(1), m.group(2) or "", m.start()))
    all_decls.sort(key=lambda d: d[3])  # sort by position

    # Build a position→outer_class_name map using brace tracking
    # For each '{' and '}' in the cleaned source, track which class scope we're in
    class_scope_stack = []  # stack of (class_name, brace_depth_at_open)
    brace_depth = 0
    decl_idx = 0
    outer_class_at_pos = {}  # position → outer class name or ""

    for i, ch in enumerate(clean):
        # Before counting braces, check if a declaration starts here
        while decl_idx < len(all_decls) and all_decls[decl_idx][3] <= i:
            decl = all_decls[decl_idx]
            # Determine outer class: current top of stack
            outer = ".".join(cs[0] for cs in class_scope_stack) if class_scope_stack else ""
            outer_class_at_pos[decl[3]] = outer
            decl_idx += 1

        if ch == '{':
            # Check if this brace opens a class that was just declared
            # Find the most recent declaration before this brace
            recent_decl = None
            for d in all_decls:
                if d[3] < i:
                    recent_decl = d
                else:
                    break
            # If a class was declared right before this brace and we haven't pushed it yet
            if (recent_decl and
                recent_decl[3] not in {cs[2] for cs in class_scope_stack} and
                clean[recent_decl[3]:i].count('{') == 0):
                full_name = recent_decl[1]
                if class_scope_stack:
                    full_name = ".".join(cs[0] for cs in class_scope_stack) + "." + full_name
                class_scope_stack.append((full_name, brace_depth, recent_decl[3]))
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if class_scope_stack and brace_depth == class_scope_stack[-1][1]:
                class_scope_stack.pop()

    # Now create ClassInfo objects with proper nested names
    for kind, name, base_list, pos in all_decls:
        outer = outer_class_at_pos.get(pos, "")
        full_name = f"{outer}.{name}" if outer else name

        ci = ClassInfo(full_name, namespace, base_list, str(file_path), project_name, feature)

        if kind == "interface":
            ci.is_interface = True
        elif kind in ("class", "record"):
            ci.routes = file_routes + file_http_routes
            ci.dispatches = file_dispatches

        results.append(ci)

    # Parse constructors → DI params
    for m in RE_CTOR.finditer(clean):
        ctor_class_name = m.group(1)
        params_text = m.group(2).strip()
        if not params_text:
            continue
        # Find matching ClassInfo — match on last segment of possibly-nested name
        target = None
        for ci in results:
            short_name = ci.name.rsplit(".", 1)[-1]
            if short_name == ctor_class_name and not ci.is_interface:
                target = ci
                break
        if not target:
            continue
        # Parse params
        for param in params_text.split(","):
            param = param.strip()
            if not param:
                continue
            # Handle: IFoo foo, IOptions<Bar> bar, ILogger<Baz> logger
            parts = param.split()
            if len(parts) >= 2:
                type_name = " ".join(parts[:-1])
                param_name = parts[-1]
                target.ctor_params.append((type_name, param_name))

    return results


# ═══════════════════════════════════════════════════════════════
# Razor / CSHTML Parsing
# ═══════════════════════════════════════════════════════════════

def parse_cshtml_file(file_path: Path, project_name: str,
                      project_dir: Path = None) -> dict:
    """Parse a .cshtml file → {vc_tags, page_route, model_type, feature}."""
    text = read_text(file_path)
    if not text:
        return None

    feature = _detect_feature(file_path, project_dir)

    vc_tags = RE_VC_TAG.findall(text)
    page_routes = RE_PAGE_DIRECTIVE.findall(text)
    model_directives = RE_MODEL_DIRECTIVE.findall(text)

    return {
        "file_path": str(file_path),
        "file_name": file_path.stem,
        "project": project_name,
        "feature": feature,
        "vc_tags": vc_tags,           # e.g. ['unified-search', 'masthead-navigation']
        "page_routes": page_routes,
        "model_type": model_directives[0] if model_directives else None,
    }


def parse_route_registrations(file_path: Path) -> list:
    """Parse a C# file for AddPageRoute calls → [{page, route}]."""
    text = read_text(file_path)
    if not text:
        return []
    clean = strip_comments(text)
    results = []
    for m in RE_ADD_PAGE_ROUTE.finditer(clean):
        results.append({"page": m.group(1), "route": m.group(2)})
    return results


# ═══════════════════════════════════════════════════════════════
# Type Classification
# ═══════════════════════════════════════════════════════════════

def classify_type(ci: ClassInfo) -> str:
    """Determine the knowledge-graph node type for a ClassInfo."""
    if ci.is_interface:
        return "_interface"  # internal, not emitted as node

    base_roots = {generic_root(b) for b in ci.bases}
    iface_roots = {generic_root(i) for i in ci.interfaces}

    # Controller
    if base_roots & CONTROLLER_BASES:
        return "controller"

    # ViewComponent
    if base_roots & VIEW_COMPONENT_BASES:
        return "viewComponent"

    # PageModel (Razor Page)
    if base_roots & PAGE_MODEL_BASES:
        return "razorView"

    # MediatR Handler
    if iface_roots & MEDIATOR_HANDLER_IFACES:
        return "messageHandler"

    # MediatR Request / Command / Query
    if iface_roots & MEDIATOR_REQUEST_IFACES:
        return "query"

    # Check folder / name for Model hints
    path_parts = set(Path(ci.file_path).parts)
    if path_parts & MODEL_FOLDERS:
        return "model"
    for indicator in MODEL_INDICATORS:
        if ci.name.endswith(indicator) and not ci.name.startswith("I"):
            return "model"

    # If class name starts with I and is just an interface-like name, skip
    # If class implements a custom interface (IFooService pattern), call it a service
    custom_ifaces = [i for i in ci.interfaces if generic_root(i) not in INFRA_INTERFACES]
    if custom_ifaces and not ci.name.endswith("Exception"):
        return "service"

    # Fallback: if it has DI params, it's likely a service
    if ci.ctor_params and len(ci.ctor_params) >= 2:
        return "service"

    # Default: skip unclassified types (helpers, extensions, utils, etc.)
    return None


# ═══════════════════════════════════════════════════════════════
# Knowledge Graph Builder
# ═══════════════════════════════════════════════════════════════

class KnowledgeGraphBuilder:
    """Orchestrates parsing and builds the final graph JSON."""

    def __init__(self, sln_path: Path, output_path: Path,
                 exclude_tests: bool = False, connected_only: bool = False):
        self.sln_path = sln_path
        self.sln_dir = sln_path.parent
        self.output_path = output_path
        self.exclude_tests = exclude_tests
        self.connected_only = connected_only

        self.projects = []         # raw project entries from .sln
        self.project_infos = {}    # name → csproj parse result
        self.all_classes = []      # all ClassInfo objects
        self.all_cshtml = []       # all cshtml parse results
        self.route_registrations = []

        # Lookup maps built during analysis
        self.class_by_name = {}          # class name → ClassInfo
        self.interface_to_impl = {}      # interface name → ClassInfo (impl)
        self.node_id_map = {}            # class name → node id
        self.vc_class_to_node = {}       # PascalCase VC name → node id
        self.features = set()
        self.page_routes = defaultdict(list)  # page path → [routes]

        self.nodes = []
        self.edges = []
        self._edge_set = set()  # for dedup

    def analyze(self):
        """Run the full analysis pipeline."""
        print(f"\n  Parsing solution: {self.sln_path.name}")
        self.projects = parse_solution(self.sln_path)
        print(f"  Found {len(self.projects)} C# projects\n")

        if self.exclude_tests:
            before = len(self.projects)
            self.projects = [
                p for p in self.projects
                if not any(kw in p["name"].lower() for kw in ("test", "tests", "spec", "specs"))
            ]
            skipped = before - len(self.projects)
            if skipped:
                print(f"  Skipped {skipped} test project(s)\n")

        for proj in self.projects:
            self._analyze_project(proj)

        self._build_lookup_maps()
        self._build_nodes()
        self._build_edges()
        self._build_cshtml_edges()

        print(f"\n  Graph: {len(self.nodes)} nodes, {len(self.edges)} edges")

    def _analyze_project(self, proj: dict):
        """Analyze a single project."""
        name = proj["name"]
        proj_dir = proj["project_dir"]
        csproj_path = proj["csproj_path"]

        print(f"  [{name}]")

        # Parse csproj
        info = parse_csproj(csproj_path)
        self.project_infos[name] = info
        print(f"    csproj: {info['target_framework'] or '?'}, "
              f"{len(info['project_refs'])} refs, {len(info['package_refs'])} packages")

        # Scan .cs files
        cs_count = 0
        for cs_file in proj_dir.rglob("*.cs"):
            # Skip obj/bin directories
            parts_lower = [p.lower() for p in cs_file.parts]
            if "obj" in parts_lower or "bin" in parts_lower:
                continue
            classes = parse_cs_file(cs_file, name, proj_dir)
            self.all_classes.extend(classes)
            cs_count += 1

            # Check for route registrations
            if "route" in cs_file.stem.lower() or "startup" in cs_file.stem.lower():
                regs = parse_route_registrations(cs_file)
                self.route_registrations.extend(regs)

        # Scan .cshtml files
        cshtml_count = 0
        for cshtml_file in proj_dir.rglob("*.cshtml"):
            parts_lower = [p.lower() for p in cshtml_file.parts]
            if "obj" in parts_lower or "bin" in parts_lower:
                continue
            result = parse_cshtml_file(cshtml_file, name, proj_dir)
            if result:
                self.all_cshtml.append(result)
                cshtml_count += 1

        print(f"    scanned: {cs_count} .cs, {cshtml_count} .cshtml")

    def _build_lookup_maps(self):
        """Build maps for name → class and interface → implementation resolution."""
        # Classify all types
        for ci in self.all_classes:
            ci.node_type = classify_type(ci)

        # Build name→class map (prefer non-interface, non-model)
        for ci in self.all_classes:
            if ci.is_interface:
                continue
            key = ci.name
            if key not in self.class_by_name:
                self.class_by_name[key] = ci
            else:
                # Prefer the one with more info (DI params, dispatches)
                existing = self.class_by_name[key]
                if len(ci.ctor_params) > len(existing.ctor_params):
                    self.class_by_name[key] = ci

        # Build interface → implementation map
        for ci in self.all_classes:
            if ci.is_interface:
                continue
            for iface in ci.interfaces:
                root = generic_root(iface)
                if root not in INFRA_INTERFACES:
                    self.interface_to_impl[root] = ci

        # Build route map from registrations
        for reg in self.route_registrations:
            page_path = reg["page"]
            # Extract class name from page path: /Masthead/UnifiedMasthead → UnifiedMasthead
            page_class = page_path.rsplit("/", 1)[-1] if "/" in page_path else page_path
            self.page_routes[page_class].append(reg["route"])

        # Collect features
        for ci in self.all_classes:
            if ci.feature:
                self.features.add((ci.project, ci.feature))

    def _make_node_id(self, ci: ClassInfo) -> str:
        """Generate a stable, readable node ID."""
        prefix_map = {
            "project": "proj",
            "controller": "ctrl",
            "viewComponent": "vc",
            "razorView": "page",
            "service": "svc",
            "query": "query",
            "messageHandler": "handler",
            "model": "model",
        }
        prefix = prefix_map.get(ci.node_type, "node")
        slug = to_kebab(ci.name)
        # Include feature in ID to disambiguate same-named classes
        # in different feature folders (e.g. ContractCode PageModel vs
        # ContractCode ViewComponent under Eudc)
        if ci.feature:
            feat_slug = to_kebab(ci.feature)
            return f"{prefix}:{to_kebab(ci.project)}:{feat_slug}:{slug}"
        return f"{prefix}:{to_kebab(ci.project)}:{slug}"

    def _get_ci_node_id(self, ci: ClassInfo) -> str:
        """Look up the node ID for a specific ClassInfo, using qualified key."""
        key = f"{ci.namespace}.{ci.name}" if ci.namespace else ci.name
        return self.node_id_map.get(key) or self.node_id_map.get(ci.name)

    def _add_edge(self, source: str, target: str, relationship: str,
                  detail: str = None):
        """Add an edge, deduplicating."""
        key = (source, target, relationship)
        if key in self._edge_set:
            return
        self._edge_set.add(key)
        edge = {"source": source, "target": target, "relationship": relationship}
        if detail:
            edge["detail"] = detail
        self.edges.append(edge)

    def _build_nodes(self):
        """Create all graph nodes."""
        # Project nodes
        for proj in self.projects:
            name = proj["name"]
            info = self.project_infos.get(name, {})
            node_id = f"proj:{to_kebab(name)}"
            self.nodes.append({
                "id": node_id,
                "type": "project",
                "label": name,
                "path": str(proj["csproj_path"].relative_to(self.sln_dir)),
                "targetFramework": info.get("target_framework", ""),
                "outputType": info.get("output_type", ""),
            })

        # Feature nodes
        for proj_name, feat_name in sorted(self.features):
            node_id = f"feature:{to_kebab(proj_name)}:{to_kebab(feat_name)}"
            self.nodes.append({
                "id": node_id,
                "type": "feature",
                "label": feat_name,
                "project": f"proj:{to_kebab(proj_name)}",
            })
            # Feature → project edge
            self._add_edge(node_id, f"proj:{to_kebab(proj_name)}", "belongsTo")

        # Class/type nodes
        seen_ids = set()
        for ci in self.all_classes:
            if ci.is_interface:
                continue
            if not ci.node_type or ci.node_type.startswith("_"):
                continue

            node_id = self._make_node_id(ci)
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)

            # Store under qualified key (namespace.name) to avoid collision
            qualified_key = f"{ci.namespace}.{ci.name}" if ci.namespace else ci.name
            self.node_id_map[qualified_key] = node_id
            # Also store under simple name if not already taken (for DI resolution)
            if ci.name not in self.node_id_map:
                self.node_id_map[ci.name] = node_id

            if ci.node_type == "viewComponent":
                self.vc_class_to_node[ci.name] = node_id

            rel_path = ""
            try:
                rel_path = str(Path(ci.file_path).relative_to(self.sln_dir))
            except ValueError:
                rel_path = ci.file_path

            node = {
                "id": node_id,
                "type": ci.node_type,
                "label": ci.name,
                "namespace": ci.namespace,
                "path": rel_path,
                "project": f"proj:{to_kebab(ci.project)}",
            }

            if ci.feature:
                feat_id = f"feature:{to_kebab(ci.project)}:{to_kebab(ci.feature)}"
                node["feature"] = ci.feature
                node["featureId"] = feat_id

            # Routes for pages/controllers
            routes = ci.routes or []
            page_routes = self.page_routes.get(ci.name, [])
            all_routes = list(set(routes + page_routes))
            if all_routes:
                node["routes"] = all_routes

            self.nodes.append(node)

    def _resolve_type_to_node_id(self, type_name: str) -> str:
        """Resolve a type name (possibly an interface) to a node ID."""
        root = generic_root(type_name)

        # Direct class match
        if root in self.node_id_map:
            return self.node_id_map[root]

        # Interface → implementation
        if root in self.interface_to_impl:
            impl = self.interface_to_impl[root]
            if impl.name in self.node_id_map:
                return self.node_id_map[impl.name]

        # Try without 'I' prefix
        if root.startswith("I") and len(root) > 1 and root[1].isupper():
            bare = root[1:]
            if bare in self.node_id_map:
                return self.node_id_map[bare]

        return None

    def _build_edges(self):
        """Create all graph edges."""
        # Project dependency edges
        for proj in self.projects:
            name = proj["name"]
            info = self.project_infos.get(name, {})
            source_id = f"proj:{to_kebab(name)}"
            for ref_name in info.get("project_refs", []):
                target_id = f"proj:{to_kebab(ref_name)}"
                # Check target exists
                if any(n["id"] == target_id for n in self.nodes):
                    self._add_edge(source_id, target_id, "dependsOn")

        # Class → feature belongsTo
        for ci in self.all_classes:
            if ci.is_interface or not ci.feature:
                continue
            node_id = self._get_ci_node_id(ci)
            if not node_id:
                continue
            feat_id = f"feature:{to_kebab(ci.project)}:{to_kebab(ci.feature)}"
            self._add_edge(node_id, feat_id, "belongsTo")

        # Constructor injection edges
        for ci in self.all_classes:
            if ci.is_interface:
                continue
            source_id = self._get_ci_node_id(ci)
            if not source_id:
                continue
            for type_name, param_name in ci.ctor_params:
                target_id = self._resolve_type_to_node_id(type_name)
                if target_id and target_id != source_id:
                    self._add_edge(source_id, target_id, "injects",
                                   detail=f"{generic_root(type_name)} {param_name}")

        # MediatR dispatch edges
        for ci in self.all_classes:
            if ci.is_interface:
                continue
            source_id = self._get_ci_node_id(ci)
            if not source_id:
                continue
            for dispatched in ci.dispatches:
                target_id = self.node_id_map.get(dispatched)
                if target_id and target_id != source_id:
                    self._add_edge(source_id, target_id, "dispatches")

        # Handler → Query/Command handled edges
        for ci in self.all_classes:
            if ci.node_type != "messageHandler":
                continue
            handler_id = self._get_ci_node_id(ci)
            if not handler_id:
                continue
            for iface in ci.interfaces:
                root = generic_root(iface)
                if root in MEDIATOR_HANDLER_IFACES:
                    args = generic_args(iface)
                    if args:
                        query_name = args[0].strip()
                        query_id = self.node_id_map.get(query_name)
                        if query_id:
                            # The handler "handles" the query — represented as invokes
                            self._add_edge(handler_id, query_id, "invokes",
                                           detail="handles")

    def _build_cshtml_edges(self):
        """Create rendering edges from .cshtml analysis."""
        for cshtml in self.all_cshtml:
            # Find the parent class (the .cshtml.cs class)
            # Convention: FooBar.cshtml → FooBar class (PageModel or ViewComponent)
            file_stem = cshtml["file_name"]
            parent_id = self.node_id_map.get(file_stem)

            if not parent_id:
                # Try matching by feature + name
                for ci_name, nid in self.node_id_map.items():
                    if ci_name == file_stem:
                        parent_id = nid
                        break

            if not parent_id:
                continue

            # <vc:xxx> tags → renders edges
            for vc_tag in cshtml["vc_tags"]:
                vc_class_name = vc_tag_to_pascal(vc_tag)
                target_id = self.vc_class_to_node.get(vc_class_name)
                if not target_id:
                    # Try finding by node_id_map
                    target_id = self.node_id_map.get(vc_class_name)
                if target_id:
                    self._add_edge(parent_id, target_id, "renders")

    def to_json(self) -> dict:
        """Produce the final JSON structure."""
        # Validate edges — remove any with missing source/target
        node_ids = {n["id"] for n in self.nodes}
        valid_edges = [e for e in self.edges
                       if e["source"] in node_ids and e["target"] in node_ids]
        dangling = len(self.edges) - len(valid_edges)
        if dangling:
            print(f"  Removed {dangling} dangling edge(s)")

        # Optionally keep only connected nodes
        final_nodes = self.nodes
        if self.connected_only:
            connected_ids = set()
            for e in valid_edges:
                connected_ids.add(e["source"])
                connected_ids.add(e["target"])
            # Always keep project and feature nodes
            before = len(final_nodes)
            final_nodes = [n for n in final_nodes
                           if n["id"] in connected_ids or n["type"] in ("project", "feature")]
            removed = before - len(final_nodes)
            if removed:
                print(f"  Removed {removed} disconnected node(s)")
            # Re-validate edges against remaining nodes
            node_ids = {n["id"] for n in final_nodes}
            valid_edges = [e for e in valid_edges
                           if e["source"] in node_ids and e["target"] in node_ids]

        # Statistics
        type_counts = defaultdict(int)
        for n in final_nodes:
            type_counts[n["type"]] += 1
        rel_counts = defaultdict(int)
        for e in valid_edges:
            rel_counts[e["relationship"]] += 1

        return {
            "metadata": {
                "title": f"{self.sln_path.stem} — Knowledge Graph",
                "description": f"Auto-generated knowledge graph for {self.sln_path.name}",
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "generator": "generate-knowledge-graph.py",
                "solutionPath": str(self.sln_path),
                "solutionName": self.sln_path.stem,
            },
            "nodes": final_nodes,
            "edges": valid_edges,
            "statistics": {
                "totalNodes": len(final_nodes),
                "totalEdges": len(valid_edges),
                "nodesByType": dict(type_counts),
                "edgesByRelationship": dict(rel_counts),
            },
        }


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def find_sln_files(directory: Path) -> list:
    """Find all .sln files in a directory (non-recursive)."""
    return sorted(directory.glob("*.sln"))


def main():
    parser = argparse.ArgumentParser(
        description="Generate a knowledge graph JSON from a C# solution."
    )
    parser.add_argument(
        "--solution", "-s",
        help="Path to the .sln file (will prompt interactively if not provided)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path (default: knowledge-graph.json in solution dir)",
    )
    parser.add_argument(
        "--exclude-tests", action="store_true",
        help="Exclude test projects (names containing 'test' or 'tests')",
    )
    parser.add_argument(
        "--connected-only", action="store_true",
        help="Only include nodes that have at least one edge (reduces noise)",
    )
    args = parser.parse_args()

    # ── Resolve solution path ──
    sln_path = None

    if args.solution:
        sln_path = Path(args.solution).resolve()
    else:
        print("\n╔══════════════════════════════════════════════════╗")
        print("║  C# Solution → Knowledge Graph Generator        ║")
        print("╚══════════════════════════════════════════════════╝\n")
        user_input = input("  Enter path to C# solution (.sln) or directory: ").strip()
        if not user_input:
            print("  No path provided. Exiting.")
            sys.exit(1)

        candidate = Path(user_input).resolve()

        if candidate.is_file() and candidate.suffix.lower() == ".sln":
            sln_path = candidate
        elif candidate.is_dir():
            sln_files = find_sln_files(candidate)
            if not sln_files:
                # Try one level deep (src/ folder)
                for sub in candidate.iterdir():
                    if sub.is_dir():
                        sln_files.extend(find_sln_files(sub))
            if not sln_files:
                print(f"  No .sln files found in {candidate}")
                sys.exit(1)
            elif len(sln_files) == 1:
                sln_path = sln_files[0]
                print(f"  Found: {sln_path.name}")
            else:
                print(f"\n  Multiple .sln files found:")
                for i, sf in enumerate(sln_files):
                    print(f"    [{i + 1}] {sf.relative_to(candidate)}")
                choice = input(f"\n  Select (1-{len(sln_files)}): ").strip()
                try:
                    sln_path = sln_files[int(choice) - 1]
                except (ValueError, IndexError):
                    print("  Invalid selection. Exiting.")
                    sys.exit(1)
        else:
            print(f"  Path not found or not a .sln file: {candidate}")
            sys.exit(1)

    if not sln_path or not sln_path.exists():
        print(f"  Solution file not found: {sln_path}")
        sys.exit(1)

    # ── Resolve output path ──
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        output_path = sln_path.parent / "knowledge-graph.json"

    # ── Run analysis ──
    print(f"\n{'─' * 54}")
    builder = KnowledgeGraphBuilder(
        sln_path, output_path,
        exclude_tests=args.exclude_tests,
        connected_only=args.connected_only,
    )
    builder.analyze()

    # ── Write output ──
    graph = builder.to_json()
    output_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Output: {output_path}")
    print(f"{'─' * 54}")

    # Print summary
    stats = graph["statistics"]
    print(f"\n  Summary:")
    print(f"    Nodes: {stats['totalNodes']}")
    for t, c in sorted(stats["nodesByType"].items(), key=lambda x: -x[1]):
        print(f"      {t:20s} {c}")
    print(f"    Edges: {stats['totalEdges']}")
    for r, c in sorted(stats["edgesByRelationship"].items(), key=lambda x: -x[1]):
        print(f"      {r:20s} {c}")
    print(f"\n  Open knowledge-graph-viewer.html and load the generated JSON.\n")


if __name__ == "__main__":
    main()
