"""The `omnivia` CLI (B10).

A Core Service client and nothing more. It discovers a service, builds contract
envelopes and reports what it is told. It holds no lease, takes no lock and opens no
database.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from omnivia_core_cli.client import build_request, encode, read_descriptor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnivia",
        description="Talk to a running OmniVia Core Service. Never owns a workspace.",
    )
    parser.add_argument(
        "--runtime-state",
        required=True,
        type=Path,
        help="installation runtime directory holding service.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discover", help="show the discovered service, if any")

    for name, help_text in (
        ("health", "ask the service whether it is alive"),
        ("readiness", "ask the service whether it is writable-ready"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--principal", default=None)
        sub.add_argument("--json", action="store_true", help="emit the request envelope")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = read_descriptor(args.runtime_state)

    if args.command == "discover":
        if service is None:
            sys.stdout.write("no service is advertised\n")
            return 1
        sys.stdout.write(
            json.dumps(
                {
                    # The output key stays `endpoint`: the value moved to
                    # `endpoint_uri` in the published document, what it means to a
                    # caller of this command did not, and renaming it would break
                    # every script reading this JSON for no gain.
                    "endpoint": service.endpoint_uri,
                    "workspace_id": service.workspace_id,
                    "service_instance_id": service.service_instance_id,
                    "fencing_generation": service.fencing_generation,
                    "ready": service.ready,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0

    if service is None:
        sys.stderr.write("no service is advertised; start omnivia-core-service first\n")
        return 1

    request = build_request(
        f"core.{args.command}",
        workspace_id=service.workspace_id,
        request_id=f"cli-{uuid.uuid4()}",
        principal=args.principal,
    )
    sys.stdout.write(encode(request) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
