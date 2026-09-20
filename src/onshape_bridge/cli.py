"""Command line entry point: onshape-bridge <command>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

from .client import OnshapeClient, OnshapeError
from .config import ConfigError, discover_projects, load_project
from .sync import SyncResult, pull_exports, push_feature_studios


def _load(path: Path):
    """Load one project, or every project beneath a directory."""
    path = Path(path)
    if path.is_dir() and not (path / "onshape.yml").is_file():
        projects = discover_projects(path)
        if not projects:
            raise ConfigError(f"no onshape.yml found anywhere under {path}")
        return projects
    return [load_project(path)]


def cmd_doctor(args: argparse.Namespace) -> int:
    """Prove an API key pair works, and say what it can reach."""
    try:
        client = OnshapeClient.from_env(args.base_url)
    except ValueError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2

    try:
        info = client.session_info()
    except requests.RequestException as exc:
        print(f"could not reach {client.base_url}: {exc}", file=sys.stderr)
        print(
            "  The keys were never checked. Fix network/proxy access to Onshape first.",
            file=sys.stderr,
        )
        return 1
    except OnshapeError as exc:
        print(f"Onshape rejected the key pair: HTTP {exc.status}", file=sys.stderr)
        if exc.status in (401, 403):
            print(
                "  401/403 means the keys are wrong, revoked, or lack the scopes you "
                "ticked when creating them. Re-create the pair in the dev portal with "
                "at least read/write on documents.",
                file=sys.stderr,
            )
        print(f"  body: {exc.body[:300]}", file=sys.stderr)
        return 1

    name = info.get("name") or info.get("email") or "(unnamed)"
    print(f"authenticated as: {name}")
    for key in ("email", "id", "state"):
        if info.get(key):
            print(f"  {key}: {info[key]}")
    print("\nAPI access works on this account. Keys are valid and the REST API answered.")
    _report_usage(client)
    return 0


def _report_usage(client: OnshapeClient) -> None:
    """Close every run with what it spent, and what it was talking to.

    Only metered responses are counted -- Onshape does not charge for 4xx or
    5xx -- so this number is what belongs in API_BUDGET.md. The API version is
    read off the response headers and costs nothing to report; it is the only
    way to see which version an unversioned base URL resolved to.
    """
    print(f"\nOnshape API calls this run: {client.call_count}")
    details = []
    if client.api_version:
        details.append(f"api version: {client.api_version}")
    if client.rate_limit_remaining is not None:
        details.append(f"endpoint calls left in window: {client.rate_limit_remaining}")
    if details:
        print("  " + "  |  ".join(details))


def cmd_elements(args: argparse.Namespace) -> int:
    """List a document's tabs with their element ids, to fill in onshape.yml."""
    client = OnshapeClient.from_env(args.base_url)
    for project in _load(args.path):
        print(f"\n{project.name}  (document {project.document_id})")
        for element in client.elements(project.document_id, project.workspace_id):
            print(
                f"  {element.get('id')}  {element.get('elementType', '?'):<12} "
                f"{element.get('name', '')}"
            )
    _report_usage(client)
    return 0


def _report(results: list[SyncResult], client: OnshapeClient) -> int:
    for result in results:
        print(result)
    if not results:
        print("nothing configured to sync")
    _report_usage(client)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    client = OnshapeClient.from_env(args.base_url)
    results: list[SyncResult] = []
    for project in _load(args.path):
        results += push_feature_studios(
            client, project, dry_run=args.dry_run, assume_changed=args.assume_changed
        )
    return _report(results, client)


def cmd_pull(args: argparse.Namespace) -> int:
    client = OnshapeClient.from_env(args.base_url)
    results: list[SyncResult] = []
    for project in _load(args.path):
        results += pull_exports(client, project, dry_run=args.dry_run)
    return _report(results, client)


def cmd_validate(args: argparse.Namespace) -> int:
    """Parse configs without touching the network."""
    for project in _load(args.path):
        print(
            f"ok  {project.name}: {len(project.feature_studios)} feature studio(s), "
            f"{len(project.exports)} export(s)"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onshape-bridge",
        description="Sync FeatureScript and exports between a git repo and Onshape.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override the API base URL (default: $ONSHAPE_BASE_URL or cad.onshape.com/api/v10)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="check that your API keys work").set_defaults(
        func=cmd_doctor
    )

    for name, help_text, func in (
        ("validate", "parse onshape.yml files without calling the API", cmd_validate),
        ("elements", "list document tabs and their element ids", cmd_elements),
        ("push", "upload FeatureScript from git into Onshape", cmd_push),
        ("pull", "download exports from Onshape into git", cmd_pull),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("path", type=Path, help="a project directory, onshape.yml, or a tree")
        if func in (cmd_push, cmd_pull):
            sub.add_argument(
                "--dry-run", action="store_true", help="report what would change, change nothing"
            )
        if func is cmd_push:
            sub.add_argument(
                "--assume-changed",
                action="store_true",
                help="skip the comparison read and upload unconditionally, halving "
                "the call cost per file (costs a needless microversion if unchanged)",
            )
        sub.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    except OnshapeError as exc:
        print(f"onshape: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"network: could not reach Onshape: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
