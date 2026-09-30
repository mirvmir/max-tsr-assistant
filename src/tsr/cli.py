"""Explicit operator commands and local demonstration through the application facade."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
from pathlib import Path
import re
import sys


def _settings():
    from tsr.config import load_settings
    result=load_settings()
    if not result.ok: raise ValueError('startup.configuration_invalid')
    return result.value


class UnconfiguredTransport:
    def send_view(self,permit,payload):
        from tsr.contracts import TransportResult
        return TransportResult(status='definitely_rejected',error_code='max_not_configured')
    def send_material(self,permit,reference): return self.send_view(permit,reference)
    def answer_callback(self,permit,answer): return self.send_view(permit,answer)
    def upload_file(self,*args):
        from tsr.contracts import Result
        return Result.failure('DATA_NOT_READY',safe_message_key='max_not_configured')


def _utc_timestamp(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError('timezone')
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError('UTC timestamp with explicit timezone required') from None


def _parser():
    parser=argparse.ArgumentParser(prog='tsr',description='MAX TSR assistant: operator commands and local demo')
    commands=parser.add_subparsers(dest='command',required=True)
    secrets=commands.add_parser('init-secrets')
    secrets.add_argument('--directory',type=Path,default=Path('secrets'))
    commands.add_parser('migrate')
    serve=commands.add_parser('serve')
    serve.add_argument('--host',default='0.0.0.0')
    serve.add_argument('--port',type=int,default=8080)
    commands.add_parser('worker')
    demo=commands.add_parser('demo')
    demo.add_argument('--scenario',choices=('purchase','support','both'),default='purchase')
    demo.add_argument('--role',choices=('self','representative'),default='self')
    demo.add_argument('--dataset',choices=('public','synthetic'))
    demo.add_argument('--download-dir',type=Path,default=Path('var/demo-downloads'))
    demo.add_argument('--show-dialog',action='store_true')
    commands.add_parser('cleanup')
    validate=commands.add_parser('validate-data',aliases=['validate-release'])
    validate.add_argument('--root',type=Path)
    validate.add_argument('--manifest')
    importing=commands.add_parser('import-release')
    importing.add_argument('--root',type=Path)
    importing.add_argument('--manifest')
    importing.add_argument('--actor-key',default='operator-cli')
    for name in ('activate-release','rollback-release','revoke-package'):
        operation=commands.add_parser(name)
        operation.add_argument('--id',required=True)
        operation.add_argument('--version',required=True)
        operation.add_argument('--actor-key',default='operator-cli')
        if name=='revoke-package': operation.add_argument('--reason-code',required=True)
        else: operation.add_argument('--mode',choices=('demo','pilot'))
    commands.add_parser('health')
    commands.add_parser('maintenance')
    backup=commands.add_parser('backup')
    backup.add_argument('--destination',type=Path,required=True)
    backup.add_argument('--maintenance',action='store_true',required=True)
    journal=commands.add_parser('export-deletion-journal')
    journal.add_argument('--destination',type=Path,required=True)
    journal.add_argument('--source-offline',action='store_true',required=True)
    journal.add_argument('--cutover',type=_utc_timestamp)
    restore=commands.add_parser('restore')
    restore.add_argument('--backup',type=Path,required=True)
    restore.add_argument('--journal',type=Path,required=True)
    restore.add_argument('--cutover',type=_utc_timestamp,required=True)
    restore.add_argument('--source-offline',action='store_true',required=True)
    restore.add_argument('--offline',action='store_true',required=True)
    restore.add_argument('--maintenance',action='store_true',required=True)
    restore.add_argument('--target-schema')
    return parser


def _operator_key(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',value): raise ValueError('operator_code_invalid')
    return value


def _resources(settings,*,files_required=True):
    from tsr.adapters.crypto import Crypto
    from tsr.adapters.db import Database
    from tsr.adapters.files import PrivateFiles
    crypto=Crypto(settings.encryption_key_bytes)
    files=PrivateFiles(settings.private_root,crypto,max_output_bytes=settings.max_file_bytes) if files_required else None
    return Database(settings.database_url,crypto,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        lock_timeout_ms=settings.db_lock_timeout_ms,
        idle_transaction_timeout_ms=settings.db_idle_transaction_timeout_ms,
        backup_snapshot_timeout_ms=settings.db_backup_snapshot_timeout_ms),files,crypto


def _checked(result):
    if not result.ok: raise ValueError('operation_rejected')
    return result.value


def _operator(args,settings):
    from tsr.contracts import BackupPolicy,VersionRef
    from tsr.operations import releases
    db,files,crypto=_resources(settings,files_required=args.command in ("maintenance","backup","restore"))
    now=datetime.now(timezone.utc)
    if args.command=='import-release':
        root=args.root or settings.release_root
        release=releases.load_release(root,args.manifest or settings.release_manifest)
        report=releases.import_release(root,release.manifest,_operator_key(args.actor_key),db=db,now=now)
        print(report.model_dump_json())
        return 0 if report.status=='staged' else 1
    if args.command in ('activate-release','rollback-release','revoke-package'):
        ref=VersionRef(id=args.id,version=args.version)
        actor=_operator_key(args.actor_key)
        if args.command=='revoke-package': result=releases.revoke_package(ref,_operator_key(args.reason_code),actor,now,db=db)
        else:
            action=releases.rollback_release if args.command=='rollback-release' else releases.activate_release
            result=action(ref,actor,args.mode or settings.mode,now,db=db,allow_synthetic_draft=settings.allow_synthetic_draft,bot_scope=settings.bot_scope)
        print(_checked(result).model_dump_json())
        return 0
    if args.command=='health':
        from tsr.adapters.max.readiness import collect_readiness
        health=collect_readiness(settings,db,now=now)
        print(health.model_dump_json())
        return 0 if health.status=='ready' else 1
    if args.command=='maintenance':
        from tsr.operations.maintenance import run_retention
        from tsr.operations.backup import purge_backups
        report=run_retention(now,settings.retention_policy,db=db,files=files,bot_scope=settings.bot_scope)
        purge_backups(settings.backup_root,settings.backup_policy,now)
        print(report.model_dump_json())
        return 1 if report.errors else 0
    from tsr.operations.backup import create_backup,restore_backup,export_deletion_journal,load_deletion_journal
    if args.command=='export-deletion-journal':
        cutover=args.cutover or now
        if cutover>now: raise ValueError('future_cutover')
        path=export_deletion_journal(args.destination,db=db,crypto=crypto,now=cutover,source_offline=args.source_offline)
        print('Encrypted current deletion journal: '+str(path))
        print('Cutover: '+cutover.isoformat())
        return 0
    policy=settings.backup_policy.model_copy(update={'maintenance':args.maintenance,
        'offline':getattr(args,'offline',False),'restore_target_schema':getattr(args,'target_schema',None)})
    if args.command=='backup': report=create_backup(args.destination,policy,db=db,files=files,crypto=crypto,now=now)
    else:
        journal=load_deletion_journal(args.journal,crypto=crypto)
        report=restore_backup(args.backup,journal,db=db,files=files,crypto=crypto,policy=policy,
            now=now,required_cutover=args.cutover,source_offline=args.source_offline)
    print(report.model_dump_json())
    return 1 if getattr(report,'errors',()) or getattr(report,'consistency_errors',()) or getattr(report,'ready',True) is False else 0


def _scenario_profile(release,role):
    matches=[profile for profile in release.demo_profiles if profile.role==role and profile.profile_ref==release.profile.ref]
    if not matches: raise ValueError('demo_profile_missing')
    return matches[0]


def main(argv=None):
    args=_parser().parse_args(argv)
    try:
        if args.command=='init-secrets':
            from tsr.bootstrap import generate_demo_secrets
            print('Secret files initialized; existing values preserved: '+', '.join(generate_demo_secrets(args.directory)))
            return 0
        if args.command in ('validate-data','validate-release'):
            from os import environ
            from tsr.operations.releases import load_release
            root=args.root or Path(environ.get('TSR_RELEASE_ROOT','.'))
            manifest=args.manifest or environ.get('TSR_RELEASE_MANIFEST','data/releases/demo-1.0.0/manifest.json')
            release=load_release(root,manifest)
            public=sum(offer.data_kind=='public_snapshot' for offer in release.offers)
            synthetic=len(release.offers)-public
            counts=', '.join(part for part in (f'{public} public offers' if public else '',f'{synthetic} synthetic offers' if synthetic else '') if part)
            dependencies=(release.profile,release.route,release.templates)
            models='; synthetic draft model dependencies' if any(item.data_kind=='synthetic' and item.review.status=='draft' for item in dependencies) else ''
            print(f'Validated demo release {release.release_ref.id} {release.release_ref.version}; {counts}{models}')
            return 0
        settings=_settings()
        if args.command in ('import-release','activate-release','rollback-release','revoke-package','health','maintenance','backup','restore','export-deletion-journal'):
            return _operator(args,settings)
        if args.command=="demo":
            if settings.mode!="demo": raise ValueError("local_simulator_requires_demo")
            dataset=args.dataset or "public"
            args.dataset=dataset
            manifest="data/releases/real-1.1.0/manifest.json" if dataset=="public" else "data/releases/demo-1.0.0/manifest.json"
            settings=settings.model_copy(update={"bot_scope":settings.bot_scope+":demo:"+dataset,"release_manifest":Path(manifest)})
        from tsr.bootstrap import build_application
        db,application,files=build_application(settings)
        if args.command=='migrate': print('PostgreSQL migrations applied; configured release staged and checked')
        elif args.command=='serve':
            import uvicorn
            from tsr.http import create_app
            uvicorn.run(create_app(settings,db,application),host=args.host,port=args.port,access_log=False)
        elif args.command=='worker':
            from tsr.runtime import TransportBindings,live_transport
            from tsr.worker.dispatcher import Worker
            configured=settings.max_token and settings.max_token.get_secret_value()
            transport=live_transport(settings,TransportBindings(db,files)) if configured else UnconfiguredTransport()
            if not configured: print('MAX is unconfigured. External delivery is rejected; readiness is degraded. Use tsr demo for local verification.')
            Worker(settings,db,application,files,transport).run()
        elif args.command=='demo':
            from tsr.demo import run_demo_scenario
            branches=('purchase','support') if args.scenario=='both' else (args.scenario,)
            for branch in branches:
                profile=_scenario_profile(application.release,args.role) if args.dataset=='public' or (args.dataset is None and application.release.catalog.data_kind=='public_snapshot') else None
                report=run_demo_scenario(settings,db,application,files,branch,args.download_dir,args.role,profile=profile)
                print(f'LOCAL DEMO — synthetic user input — {branch}: {report.bundle_status}; files={len(report.downloads)}; gap={report.gap_minor} kopeks')
                print('Preview/manifest hash: '+report.manifest_hash)
                for path in report.downloads: print('Downloaded: '+str(path.resolve()))
                if args.show_dialog:
                    for message in report.messages: print('\n'+message)
        elif args.command=='cleanup':
            from tsr.operations.cleanup import cleanup_deleted_cases
            report=cleanup_deleted_cases(db,files)
            print(f'Cleanup: removed={report.removed_count}; pending={report.remaining_count}')
            return 1 if report.errors else 0
        return 0
    except Exception:
        print('Operation failed safely. Check configuration, PostgreSQL availability and the runbook.',file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
