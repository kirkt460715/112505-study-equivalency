"""Command line interface.

``python3 -m studyvisit scenario``
    Replay the fixtures, exercise the lost-reply receipt recovery in-process,
    and print the canonical verification / review report.

``python3 -m studyvisit serve --port 8080``
    Run the JSON backend seeded from the fixtures.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from threading import Thread

from .gateway_client import GatewayClient
from .scenario import CERT_REFERENCE, build_report, replay_canonical


def _print_scenario(folder: str) -> int:
    svc = replay_canonical(folder)
    report = build_report(svc)

    # Genuinely exercise the counterparty lost-reply -> lookup recovery against
    # an in-process copy of the fixture gateway (stdlib only).
    gateway_info = None
    try:
        try:
            from fixture_gateway import FixtureGateway  # provided at repo root
        except ImportError:  # running from outside the repository root
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from fixture_gateway import FixtureGateway

        gw = FixtureGateway(0, Path(folder))
        thread = Thread(target=gw.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            client = GatewayClient(f"http://127.0.0.1:{gw.server_port}")
            gateway_info = client.publish_with_recovery(
                CERT_REFERENCE, "publish-certificate", "school-18"
            )
        finally:
            gw.shutdown()
            gw.server_close()
            thread.join(2)
    except Exception as exc:  # pragma: no cover - environment convenience
        gateway_info = {"error": str(exc)}

    report["gateway_recovery_demo"] = gateway_info
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")

    # Compact human summary on stderr so stdout stays pure JSON.
    cert = report["certificates"]["cert-7"]["current_corroboration"]
    print(
        f"[scenario] cert-7 -> in_review; unsupported={cert['unsupported_points']} "
        f"still_supported={cert['still_supported_points']} action={cert['recommended_action']}",
        file=sys.stderr,
    )
    return 0


def _serve(folder: str, port: int, gateway_url: str | None = None) -> int:
    from .server import create_server

    if gateway_url:
        from .scenario import sync_from_gateway

        svc = sync_from_gateway(GatewayClient(gateway_url))
    else:
        svc = replay_canonical(folder)
    httpd = create_server(port, svc)
    actual_port = httpd.server_address[1]
    source = gateway_url or f"fixtures={folder}"
    print(f"studyvisit backend on http://127.0.0.1:{actual_port} ({source})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()
    return 0


def _sync(gateway_url: str) -> int:
    from .scenario import sync_from_gateway

    svc = sync_from_gateway(GatewayClient(gateway_url))
    json.dump(build_report(svc), sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="studyvisit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_sc = sub.add_parser("scenario", help="replay fixtures and print the canonical report")
    p_sc.add_argument("--fixtures", default="fixtures")

    p_serve = sub.add_parser("serve", help="run the JSON HTTP backend")
    p_serve.add_argument("--fixtures", default="fixtures")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--gateway-url", default=None,
                         help="ingest register/events from a running fixture gateway")

    p_sync = sub.add_parser("sync", help="sync from a fixture gateway URL and print report")
    p_sync.add_argument("--gateway-url", default="http://127.0.0.1:8765")

    args = parser.parse_args(argv)
    if args.command == "scenario":
        return _print_scenario(args.fixtures)
    if args.command == "serve":
        return _serve(args.fixtures, args.port, args.gateway_url)
    if args.command == "sync":
        return _sync(args.gateway_url)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
