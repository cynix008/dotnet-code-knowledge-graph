"""Command-line interface for the knowledge graph generator."""

import argparse
import json
from pathlib import Path

from .builder import KnowledgeGraphBuilder


def find_sln_files(directory: Path) -> list:
    """Find solution files directly inside a directory."""
    return sorted(directory.glob("*.sln"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a knowledge graph JSON from a C# solution."
    )
    parser.add_argument(
        "--solution", "-s",
        help="Path to the .sln file (will prompt if omitted)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON path (defaults to the solution directory)",
    )
    parser.add_argument(
        "--exclude-tests",
        action="store_true",
        help="Exclude projects whose names indicate tests or specs",
    )
    parser.add_argument(
        "--connected-only",
        action="store_true",
        help="Only include nodes with at least one relationship",
    )
    return parser


def resolve_solution_path(solution_argument: str = None) -> Path:
    """Resolve a solution path from an argument or interactive input."""
    if solution_argument:
        candidate = Path(solution_argument).resolve()
    else:
        user_input = input(
            "Enter path to C# solution (.sln) or directory: "
        ).strip()
        if not user_input:
            raise ValueError("No path provided.")
        candidate = Path(user_input).resolve()
    if candidate.is_file() and candidate.suffix.lower() == ".sln":
        return candidate
    if not candidate.is_dir():
        raise ValueError(
            f"Path not found or not a .sln file: {candidate}"
        )
    solutions = find_sln_files(candidate)
    if not solutions:
        for child in candidate.iterdir():
            if child.is_dir():
                solutions.extend(find_sln_files(child))
    if not solutions:
        raise ValueError(f"No .sln files found in {candidate}")
    if len(solutions) == 1:
        print(f"  Found: {solutions[0].name}")
        return solutions[0]
    print("\n  Multiple .sln files found:")
    for index, solution in enumerate(solutions, start=1):
        print(f"    [{index}] {solution.relative_to(candidate)}")
    choice = input(f"\n  Select (1-{len(solutions)}): ").strip()
    try:
        return solutions[int(choice) - 1]
    except (ValueError, IndexError) as error:
        raise ValueError("Invalid selection.") from error


def generate_graph(args: argparse.Namespace) -> tuple:
    """Analyze the selected solution and write the graph JSON."""
    solution_path = resolve_solution_path(args.solution)
    if not solution_path.exists():
        raise ValueError(f"Solution file not found: {solution_path}")
    output_path = (
        Path(args.output).resolve()
        if args.output
        else solution_path.parent / "knowledge-graph.json"
    )
    builder = KnowledgeGraphBuilder(
        solution_path,
        output_path,
        exclude_tests=args.exclude_tests,
        connected_only=args.connected_only,
    )
    builder.analyze()
    graph = builder.to_json()
    output_path.write_text(
        json.dumps(graph, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return graph, output_path


def print_summary(graph: dict, output_path: Path):
    """Print graph output location and aggregate statistics."""
    stats = graph["statistics"]
    print(f"\n  Output: {output_path}")
    print(f"\n  Summary:")
    print(f"    Nodes: {stats['totalNodes']}")
    for node_type, count in sorted(
        stats["nodesByType"].items(),
        key=lambda item: -item[1],
    ):
        print(f"      {node_type:20s} {count}")
    print(f"    Edges: {stats['totalEdges']}")
    for relationship, count in sorted(
        stats["edgesByRelationship"].items(),
        key=lambda item: -item[1],
    ):
        print(f"      {relationship:20s} {count}")
    print("\n  Open knowledge-graph-viewer.html and load the JSON.\n")


def main() -> int:
    """Run the command-line application."""
    args = build_parser().parse_args()
    try:
        graph, output_path = generate_graph(args)
    except ValueError as error:
        print(f"  {error}")
        return 1
    print_summary(graph, output_path)
    return 0
