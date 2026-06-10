"""Knowledge graph construction and serialization."""

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    INFRA_INTERFACES,
    MEDIATOR_HANDLER_IFACES,
    ClassInfo,
    classify_type,
    generic_args,
    generic_root,
    parse_cs_file,
    parse_cshtml_file,
    parse_csproj,
    parse_route_registrations,
    parse_solution,
    to_kebab,
    type_contract_key,
    vc_tag_to_pascal,
)


class KnowledgeGraphBuilder:
    """Analyze a C# solution and build its knowledge graph."""

    def __init__(self, sln_path: Path, output_path: Path,
                 exclude_tests: bool = False, connected_only: bool = False):
        self.sln_path = sln_path
        self.sln_dir = sln_path.parent
        self.output_path = output_path
        self.exclude_tests = exclude_tests
        self.connected_only = connected_only
        self.projects = []
        self.project_infos = {}
        self.all_classes = []
        self.all_cshtml = []
        self.route_registrations = []
        self.class_by_name = {}
        self.interface_to_impl = {}
        self.node_id_map = {}
        self.interface_node_id_map = {}
        self.vc_class_to_node = {}
        self.features = set()
        self.page_routes = defaultdict(list)
        self.nodes = []
        self.edges = []
        self._edge_set = set()

    def analyze(self):
        """Run the full analysis pipeline."""
        print(f"\n  Parsing solution: {self.sln_path.name}")
        self.projects = parse_solution(self.sln_path)
        print(f"  Found {len(self.projects)} C# projects\n")
        if self.exclude_tests:
            before = len(self.projects)
            self.projects = [
                project for project in self.projects
                if not any(
                    keyword in project["name"].lower()
                    for keyword in ("test", "tests", "spec", "specs")
                )
            ]
            skipped = before - len(self.projects)
            if skipped:
                print(f"  Skipped {skipped} test project(s)\n")
        for project in self.projects:
            self._analyze_project(project)
        self._build_lookup_maps()
        self._build_nodes()
        self._build_edges()
        self._build_cshtml_edges()
        print(f"\n  Graph: {len(self.nodes)} nodes, {len(self.edges)} edges")

    def _analyze_project(self, project: dict):
        name = project["name"]
        project_dir = project["project_dir"]
        print(f"  [{name}]")
        info = parse_csproj(project["csproj_path"])
        self.project_infos[name] = info
        print(
            f"    csproj: {info['target_framework'] or '?'}, "
            f"{len(info['project_refs'])} refs, "
            f"{len(info['package_refs'])} packages"
        )
        cs_count = 0
        for cs_file in project_dir.rglob("*.cs"):
            if self._is_generated_path(cs_file):
                continue
            self.all_classes.extend(
                parse_cs_file(cs_file, name, project_dir)
            )
            cs_count += 1
            if (
                "route" in cs_file.stem.lower()
                or "startup" in cs_file.stem.lower()
            ):
                self.route_registrations.extend(
                    parse_route_registrations(cs_file)
                )
        cshtml_count = 0
        for cshtml_file in project_dir.rglob("*.cshtml"):
            if self._is_generated_path(cshtml_file):
                continue
            result = parse_cshtml_file(cshtml_file, name, project_dir)
            if result:
                self.all_cshtml.append(result)
                cshtml_count += 1
        print(f"    scanned: {cs_count} .cs, {cshtml_count} .cshtml")

    @staticmethod
    def _is_generated_path(path: Path) -> bool:
        parts = {part.lower() for part in path.parts}
        return "obj" in parts or "bin" in parts

    def _build_lookup_maps(self):
        for info in self.all_classes:
            info.node_type = classify_type(info)
        for info in self.all_classes:
            if info.is_interface:
                continue
            existing = self.class_by_name.get(info.name)
            if (
                existing is None
                or len(info.ctor_params) > len(existing.ctor_params)
            ):
                self.class_by_name[info.name] = info
        for info in self.all_classes:
            if info.is_interface:
                continue
            for interface in info.interfaces:
                if generic_root(interface) not in INFRA_INTERFACES:
                    self.interface_to_impl[
                        type_contract_key(interface)
                    ] = info
        for registration in self.route_registrations:
            page_path = registration["page"]
            page_class = page_path.rsplit("/", 1)[-1]
            self.page_routes[page_class].append(registration["route"])
        self.features.update(
            (info.project, info.feature)
            for info in self.all_classes
            if info.feature
        )

    def _make_node_id(self, info: ClassInfo) -> str:
        prefix = {
            "project": "proj",
            "controller": "ctrl",
            "viewComponent": "vc",
            "razorView": "page",
            "service": "svc",
            "query": "query",
            "messageHandler": "handler",
            "model": "model",
            "interface": "iface",
        }.get(info.node_type, "node")
        segments = [prefix, to_kebab(info.project)]
        if info.feature:
            segments.append(to_kebab(info.feature))
        segments.append(to_kebab(info.name))
        return ":".join(segments)

    def _get_ci_node_id(self, info: ClassInfo) -> str:
        qualified_name = (
            f"{info.namespace}.{info.name}"
            if info.namespace else info.name
        )
        node_map = (
            self.interface_node_id_map
            if info.is_interface else self.node_id_map
        )
        return node_map.get(qualified_name) or node_map.get(info.name)

    def _add_edge(self, source: str, target: str, relationship: str,
                  detail: str = None):
        key = (source, target, relationship)
        if key in self._edge_set:
            return
        self._edge_set.add(key)
        edge = {
            "source": source,
            "target": target,
            "relationship": relationship,
        }
        if detail:
            edge["detail"] = detail
        self.edges.append(edge)

    def _build_nodes(self):
        self._build_project_nodes()
        self._build_feature_nodes()
        self._build_type_nodes()

    def _build_project_nodes(self):
        for project in self.projects:
            name = project["name"]
            info = self.project_infos.get(name, {})
            self.nodes.append({
                "id": f"proj:{to_kebab(name)}",
                "type": "project",
                "label": name,
                "path": str(
                    project["csproj_path"].relative_to(self.sln_dir)
                ),
                "targetFramework": info.get("target_framework", ""),
                "outputType": info.get("output_type", ""),
            })

    def _build_feature_nodes(self):
        for project_name, feature_name in sorted(self.features):
            node_id = (
                f"feature:{to_kebab(project_name)}:"
                f"{to_kebab(feature_name)}"
            )
            self.nodes.append({
                "id": node_id,
                "type": "feature",
                "label": feature_name,
                "project": f"proj:{to_kebab(project_name)}",
            })
            self._add_edge(
                node_id,
                f"proj:{to_kebab(project_name)}",
                "belongsTo",
            )

    def _build_type_nodes(self):
        seen_ids = set()
        for info in self.all_classes:
            if not info.node_type or info.node_type.startswith("_"):
                continue
            node_id = self._make_node_id(info)
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            self._register_type_node_id(info, node_id)
            if info.node_type == "viewComponent":
                self.vc_class_to_node[info.name] = node_id
            node = {
                "id": node_id,
                "type": info.node_type,
                "label": info.name,
                "namespace": info.namespace,
                "path": self._relative_source_path(info.file_path),
                "project": f"proj:{to_kebab(info.project)}",
            }
            if info.feature:
                node["feature"] = info.feature
                node["featureId"] = (
                    f"feature:{to_kebab(info.project)}:"
                    f"{to_kebab(info.feature)}"
                )
            routes = list(set(
                (info.routes or []) + self.page_routes.get(info.name, [])
            ))
            if routes:
                node["routes"] = routes
            self.nodes.append(node)

    def _register_type_node_id(self, info: ClassInfo, node_id: str):
        qualified_name = (
            f"{info.namespace}.{info.name}"
            if info.namespace else info.name
        )
        node_map = (
            self.interface_node_id_map
            if info.is_interface else self.node_id_map
        )
        node_map[qualified_name] = node_id
        node_map.setdefault(info.name, node_id)

    def _relative_source_path(self, file_path: str) -> str:
        try:
            return str(Path(file_path).relative_to(self.sln_dir))
        except ValueError:
            return file_path

    def _resolve_type_to_node_id(self, type_name: str) -> str:
        root = generic_root(type_name)
        if root in self.node_id_map:
            return self.node_id_map[root]
        implementation = self.interface_to_impl.get(
            type_contract_key(type_name)
        )
        if implementation:
            implementation_id = self._get_ci_node_id(implementation)
            if implementation_id:
                return implementation_id
        if root.startswith("I") and root[1:2].isupper():
            return self.node_id_map.get(root[1:])
        return None

    def _build_edges(self):
        self._build_project_dependency_edges()
        self._build_feature_membership_edges()
        self._build_implementation_edges()
        self._build_injection_edges()
        self._build_dispatch_edges()
        self._build_handler_edges()

    def _build_project_dependency_edges(self):
        node_ids = {node["id"] for node in self.nodes}
        for project in self.projects:
            source_id = f"proj:{to_kebab(project['name'])}"
            info = self.project_infos.get(project["name"], {})
            for reference_name in info.get("project_refs", []):
                target_id = f"proj:{to_kebab(reference_name)}"
                if target_id in node_ids:
                    self._add_edge(source_id, target_id, "dependsOn")

    def _build_feature_membership_edges(self):
        for info in self.all_classes:
            if not info.feature:
                continue
            node_id = self._get_ci_node_id(info)
            if node_id:
                self._add_edge(
                    node_id,
                    f"feature:{to_kebab(info.project)}:"
                    f"{to_kebab(info.feature)}",
                    "belongsTo",
                )

    def _build_implementation_edges(self):
        for info in self.all_classes:
            if info.is_interface:
                continue
            source_id = self._get_ci_node_id(info)
            if not source_id:
                continue
            for interface in info.interfaces:
                root = generic_root(interface)
                if root in INFRA_INTERFACES:
                    continue
                target_id = self.interface_node_id_map.get(root)
                if target_id:
                    self._add_edge(source_id, target_id, "implements")

    def _build_injection_edges(self):
        for info in self.all_classes:
            if info.is_interface:
                continue
            source_id = self._get_ci_node_id(info)
            if not source_id:
                continue
            for type_name, parameter_name in info.ctor_params:
                target_id = self._resolve_type_to_node_id(type_name)
                if target_id and target_id != source_id:
                    self._add_edge(
                        source_id,
                        target_id,
                        "injects",
                        detail=(
                            f"{generic_root(type_name)} {parameter_name}"
                        ),
                    )

    def _build_dispatch_edges(self):
        for info in self.all_classes:
            if info.is_interface:
                continue
            source_id = self._get_ci_node_id(info)
            if not source_id:
                continue
            for dispatched in info.dispatches:
                target_id = self.node_id_map.get(dispatched)
                if target_id and target_id != source_id:
                    self._add_edge(source_id, target_id, "dispatches")

    def _build_handler_edges(self):
        for info in self.all_classes:
            if info.node_type != "messageHandler":
                continue
            handler_id = self._get_ci_node_id(info)
            if not handler_id:
                continue
            for interface in info.interfaces:
                if generic_root(interface) not in MEDIATOR_HANDLER_IFACES:
                    continue
                arguments = generic_args(interface)
                if not arguments:
                    continue
                query_id = self.node_id_map.get(arguments[0].strip())
                if query_id:
                    self._add_edge(
                        handler_id,
                        query_id,
                        "invokes",
                        detail="handles",
                    )

    def _build_cshtml_edges(self):
        for cshtml in self.all_cshtml:
            parent_id = self.node_id_map.get(cshtml["file_name"])
            if not parent_id:
                continue
            for tag in cshtml["vc_tags"]:
                class_name = vc_tag_to_pascal(tag)
                target_id = (
                    self.vc_class_to_node.get(class_name)
                    or self.node_id_map.get(class_name)
                )
                if target_id:
                    self._add_edge(parent_id, target_id, "renders")

    def to_json(self) -> dict:
        """Return the validated graph document."""
        node_ids = {node["id"] for node in self.nodes}
        valid_edges = [
            edge for edge in self.edges
            if edge["source"] in node_ids and edge["target"] in node_ids
        ]
        dangling_count = len(self.edges) - len(valid_edges)
        if dangling_count:
            print(f"  Removed {dangling_count} dangling edge(s)")
        final_nodes = self.nodes
        if self.connected_only:
            final_nodes, valid_edges = self._connected_graph(
                final_nodes, valid_edges
            )
        node_counts = defaultdict(int)
        relationship_counts = defaultdict(int)
        for node in final_nodes:
            node_counts[node["type"]] += 1
        for edge in valid_edges:
            relationship_counts[edge["relationship"]] += 1
        return {
            "metadata": {
                "title": (
                    f"{self.sln_path.stem} \u2014 Knowledge Graph"
                ),
                "description": (
                    "Auto-generated knowledge graph for "
                    f"{self.sln_path.name}"
                ),
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
                "nodesByType": dict(node_counts),
                "edgesByRelationship": dict(relationship_counts),
            },
        }

    def _connected_graph(self, nodes: list, edges: list) -> tuple:
        connected_ids = {
            endpoint
            for edge in edges
            for endpoint in (edge["source"], edge["target"])
        }
        final_nodes = [
            node for node in nodes
            if node["id"] in connected_ids
            or node["type"] in ("project", "feature")
        ]
        removed_count = len(nodes) - len(final_nodes)
        if removed_count:
            print(f"  Removed {removed_count} disconnected node(s)")
        final_ids = {node["id"] for node in final_nodes}
        final_edges = [
            edge for edge in edges
            if edge["source"] in final_ids and edge["target"] in final_ids
        ]
        return final_nodes, final_edges
