"""Operator commands and an explicitly synthetic local dialogue simulator."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


def _settings():
    from tsr.config import load_settings

    settings = load_settings()
    if not settings.ok:
        raise ValueError("startup.configuration_invalid")
    return settings.value


class UnconfiguredTransport:
    def send_view(self, permit, payload):
        from tsr.contracts import TransportResult
        return TransportResult(status="definitely_rejected", error_code="max_not_configured")

    def send_material(self, permit, reference):
        return self.send_view(permit, reference)

    def answer_callback(self, permit, answer):
        return self.send_view(permit, answer)

    def upload_file(self, *args):
        from tsr.contracts import Result
        return Result.failure("DATA_NOT_READY", safe_message_key="max_not_configured")


def _parser():
    parser = argparse.ArgumentParser(prog="tsr", description="MAX TSR assistant: operator and synthetic demo commands")
    commands = parser.add_subparsers(dest="command", required=True)
    secrets = commands.add_parser("init-secrets", help="Create local secret files without rotating existing keys")
    secrets.add_argument("--directory", type=Path, default=Path("secrets"))
    commands.add_parser("migrate", help="Apply idempotent PostgreSQL migrations and stage the demo release")
    serve = commands.add_parser("serve", help="Run technical HTTP endpoints")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    commands.add_parser("worker", help="Run bounded inbox/render/delivery worker")
    demo = commands.add_parser("demo", help="Run the same application and worker with synthetic local MAX transport")
    demo.add_argument("--scenario", choices=("purchase", "support", "both"), default="purchase")
    demo.add_argument("--role", choices=("self", "representative"), default="self")
    demo.add_argument("--download-dir", type=Path, default=Path("var/demo-downloads"))
    demo.add_argument("--show-dialog", action="store_true")
    commands.add_parser("cleanup", help="Remove ciphertext for tombstoned cases")
    commands.add_parser("validate-data", help="Validate local immutable release, refs, hashes and assets")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "init-secrets":
            from tsr.bootstrap import generate_demo_secrets
            names = generate_demo_secrets(args.directory)
            print("Secret files initialized; existing values preserved: " + ", ".join(names))
            return 0
        if args.command == "validate-data":
            from tsr.operations.releases import load_demo_release
            from os import environ
            release = load_demo_release(Path(environ.get("TSR_RELEASE_ROOT", ".")))
            print(f"Valid synthetic draft: {release.release_ref.id} {release.release_ref.version}; {len(release.offers)} offers")
            return 0
        from tsr.bootstrap import build_application
        settings = _settings()
        db, application, files = build_application(settings)
        if args.command == "migrate":
            print("PostgreSQL migrations applied; demo release staged and checked")
        elif args.command == "serve":
            import uvicorn
            from tsr.http import create_app
            uvicorn.run(create_app(settings, db, application), host=args.host, port=args.port, access_log=False)
        elif args.command == "worker":
            from tsr.runtime import TransportBindings, live_transport
            from tsr.worker.dispatcher import Worker
            configured = settings.max_token and settings.max_token.get_secret_value()
            transport = live_transport(settings, TransportBindings(db, files)) if configured else UnconfiguredTransport()
            if not configured:
                print("MAX is unconfigured. External deliveries are definitely rejected; use tsr demo for local verification.")
            Worker(settings, db, application, files, transport).run()
        elif args.command == "demo":
            from tsr.demo import run_demo_scenario
            branches = ("purchase", "support") if args.scenario == "both" else (args.scenario,)
            for branch in branches:
                report = run_demo_scenario(settings, db, application, files, branch, args.download_dir, args.role)
                print(f"DEMO LOCAL — {branch}: {report.bundle_status}; files={len(report.downloads)}; gap={report.gap_minor} kopeks")
                print("Preview/manifest hash: " + report.manifest_hash)
                for path in report.downloads:
                    print("Downloaded: " + str(path.resolve()))
                if args.show_dialog:
                    for message in report.messages:
                        print("\n" + message)
        elif args.command == "cleanup":
            from tsr.operations.cleanup import cleanup_deleted_cases
            report = cleanup_deleted_cases(db, files)
            print(f"Cleanup: removed={report.removed_count}; pending={report.remaining_count}")
            return 1 if report.errors else 0
        return 0
    except Exception:
        # Paths, DSNs, tokens, platform payloads and exception strings stay private.
        print("Operation failed safely. Check configuration, PostgreSQL availability and the runbook.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
