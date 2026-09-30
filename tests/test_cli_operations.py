"""Operator gates and explicit release references, without leaking configuration."""
from pathlib import Path
from datetime import datetime,timezone
import pytest


def test_restore_cli_requires_offline_source_and_explicit_cutover():
    from tsr.cli import _parser
    complete=['restore','--backup','backup.tsrb','--journal','current.tsrj','--cutover','2026-09-30T00:00:00Z',
              '--source-offline','--offline','--maintenance']
    args=_parser().parse_args(complete)
    assert args.cutover==datetime(2026,9,30,tzinfo=timezone.utc)
    assert args.source_offline and args.offline and args.maintenance
    for absent in ('--source-offline','--offline','--maintenance'):
        with pytest.raises(SystemExit): _parser().parse_args([value for value in complete if value!=absent])
    with pytest.raises(SystemExit): _parser().parse_args(['restore','--backup','backup.tsrb','--journal','current.tsrj'])


def test_public_demo_answers_are_read_from_synthetic_fixture():
    from tsr.operations.releases import load_release
    from tsr.demo import demo_profile_answers
    release=load_release(Path(__file__).parents[1],'data/releases/real-1.1.0/manifest.json')
    profile=next(p for p in release.demo_profiles if p.role=='self')
    assert demo_profile_answers(profile,release.profile)==('405','100','да','10000','yes')
    assert profile.is_synthetic and profile.certificate_amount.minor==1000000
    assert all(offer.accepts_certificate.status!='known' for offer in release.offers)


def test_operator_failure_is_sanitized(monkeypatch,capsys):
    import tsr.cli
    def sensitive_failure(): raise RuntimeError('postgres://secret-password@host/personal-database')
    monkeypatch.setattr(tsr.cli,'_settings',sensitive_failure)
    assert tsr.cli.main(['health'])==1
    output=capsys.readouterr()
    assert 'secret-password' not in output.err and 'personal-database' not in output.err


from test_application import app


def test_operator_release_commands_are_scoped_and_never_bypass_revocation(app,monkeypatch,capsys):
    import tsr.cli
    from tsr.contracts import VersionRef
    root=Path(__file__).parents[1]
    settings=app.settings.model_copy(update={'release_root':root,'bot_scope':'operator-isolated'})
    monkeypatch.setattr(tsr.cli,'_settings',lambda:settings)
    monkeypatch.setattr(tsr.cli,'_resources',lambda *args,**kwargs:(app.db,None,app.db.crypto))
    assert tsr.cli.main(['import-release','--root',str(root),'--manifest','data/releases/demo-1.0.0/manifest.json'])==0
    ref=app.release.release_ref
    selected=['--id',ref.id,'--version',ref.version]
    assert tsr.cli.main(['activate-release',*selected])==0
    with app.db.uow() as unit:
        assert unit.releases.get_active('demo',bot_scope=settings.bot_scope).ref==ref
        assert unit.releases.get_active('demo',bot_scope='working-bot') is None
    leaf=app.release.profile.ref
    assert tsr.cli.main(['revoke-package','--id',leaf.id,'--version',leaf.version,'--reason-code','operator_test'])==0
    assert tsr.cli.main(['rollback-release',*selected])==1
    with app.db.uow() as unit:
        assert unit.releases.get_active('demo',bot_scope=settings.bot_scope).ref==ref
    assert 'Operation failed safely' in capsys.readouterr().err


def test_validate_public_catalog_copy_distinguishes_model_dependencies(capsys):
    from tsr.cli import main
    root=Path(__file__).parents[1]
    assert main(['validate-data','--root',str(root),'--manifest','data/releases/real-1.1.0/manifest.json'])==0
    output=capsys.readouterr().out
    assert '12 public offers' in output and 'synthetic draft model dependencies' in output
