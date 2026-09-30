"""Offline QA05: real PostgreSQL/worker, synthetic catalog, no external MAX calls.

Run with TSR_TEST_DATABASE_URL and PYTHONPATH=src. An isolated schema is removed
on exit. Facade commands represent admitted local dialog events: 50 initial
starts form a burst, remaining events are paced at 5/s across those dialogs.
This does not measure an HTTP proxy, MAX delivery, or provider rate limits.
"""
from __future__ import annotations
import argparse
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from tsr.adapters.crypto import Crypto
from tsr.adapters.db import Database
from tsr.adapters.files import PrivateFiles
from tsr.application import Application
from tsr.config import Settings
from tsr.contracts import CommandEnvelope, CaseGuard, NavigatePayload, StartCasePayload
from tsr.demo import LocalTransport, create_demo_actor
from tsr.operations.releases import load_demo_release
from tsr.runtime import TransportBindings
from tsr.worker.dispatcher import Worker


@dataclass
class Step:
    call: object
    event: bool = True
    phase: str = 'dialogue'


def checked(result):
    if not result.ok:
        raise RuntimeError('facade_rejected:' + str(result.error.code))
    return result.value


def dialogue(application, db, ctx, branch):
    outcome = yield Step(lambda: checked(application.execute_command(CommandEnvelope(
        command_id=uuid4(), actor=ctx, type='start_case',
        payload=StartCasePayload(category_ref=application.release.profile.ref,requested_role='self')))))
    def choose(view, key):
        handle = next(action.action_handle for action in view.view.actions if action.label_key == key)
        return checked(application.execute_action(ctx, handle))
    for answer in ('420', '100', 'да', '100000', 'да'):
        proposed = yield Step(lambda outcome=outcome,answer=answer: checked(application.execute_text(ctx,answer,outcome.case.case_id)))
        outcome = yield Step(lambda proposed=proposed: choose(proposed,'confirm'))
    compared = yield Step(lambda: choose(outcome,'compare'))
    selected = yield Step(lambda: checked(application.execute_action(ctx,compared.view.actions[0].action_handle)))
    outcome = yield Step(lambda: choose(selected,branch))
    if branch == 'support':
        for answer in ('ru-alt','да','Синтетический заявитель QA','Синтетический адрес QA'):
            proposed = yield Step(lambda outcome=outcome,answer=answer: checked(application.execute_text(ctx,answer,outcome.case.case_id)))
            outcome = yield Step(lambda proposed=proposed: choose(proposed,'confirm'))
    for _ in range(100):
        if any(action.label_key == 'prepare' for action in outcome.view.actions):
            break
        forward = next(action.label_key for action in outcome.view.actions if 'Далее' in action.label_key)
        outcome = yield Step(lambda outcome=outcome,forward=forward: choose(outcome,forward))
    else:
        raise RuntimeError('review_bound_exceeded')
    confirmed = yield Step(lambda: choose(outcome,'prepare'),phase='confirm_bundle')
    def bundle_ready():
        with db.uow() as unit:
            bundle = unit.bundles.get(confirmed.created_bundle_id)
        if bundle.status in ('failed','partial'):
            raise RuntimeError('bundle_failed')
        return bundle.status == 'ready'
    while not (yield Step(bundle_ready,event=False)):
        pass
    outcome = yield Step(lambda: checked(application.execute_command(CommandEnvelope(
        command_id=uuid4(), actor=ctx, type='navigate',
        case_guard=CaseGuard(case_id=confirmed.case.case_id,expected_revision=confirmed.case.case_revision,
                             expected_deletion_epoch=confirmed.case.deletion_epoch),
        payload=NavigatePayload(destination='materials')))))
    with db.uow() as unit:
        handles = [action.action_handle for action in outcome.view.actions
                   if checked(unit.handles.resolve(ctx,action.action_handle,datetime.now(timezone.utc))).action.command_type == 'request_material']
    for handle in handles:
        yield Step(lambda handle=handle: checked(application.execute_action(ctx,handle)))
    return len(handles)


def percentile(samples):
    if not samples:
        return None
    return round(sorted(samples)[max(0,math.ceil(len(samples)*0.95)-1)] * 1000,3)


def run(args):
    dsn=os.environ.get('TSR_TEST_DATABASE_URL')
    if not dsn:
        raise ValueError('TSR_TEST_DATABASE_URL required')
    schema='qa_load_'+uuid4().hex
    args.output.parent.mkdir(parents=True,exist_ok=True)
    private=args.output.parent / (schema+'_private')
    with psycopg.connect(dsn) as connection:
        connection.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    worker=None
    thread=None
    stop=threading.Event()
    pause_worker=threading.Event()
    worker_paused=threading.Event()
    errors=[]
    work_rejections=[]
    command_times=[]
    queue_times=[]
    render_times=[]
    delivery_times=[]
    event_times=[]
    start=time.monotonic()
    completed=0
    requested=0
    mode=getattr(args,'mode','dialogs')
    queued_documents=None
    document_queue_seconds=None
    try:
        crypto=Crypto(os.urandom(32))
        scoped=make_conninfo(dsn,options='-c search_path='+schema)
        db=Database(scoped,crypto)
        db.migrate()
        settings=Settings(database_url=dsn,encryption_key=SecretStr('11'*32),identity_hmac_key=SecretStr('qa-synthetic-only'),
                          bot_scope='qa-offline-'+uuid4().hex,private_root=private,backup_root=private/'backups',
                          heartbeat_seconds=1,release_commit='qa-offline-load')
        release=load_demo_release(Path(__file__).resolve().parents[1])
        offers=tuple(release.offers[index % len(release.offers)].model_copy(update={
            'snapshot_id':uuid4(),'offer_id':'qa-synthetic-'+str(index),'seller_sku':'QA-'+str(index)})
            for index in range(args.snapshots))
        assert all(offer.data_kind=='synthetic' for offer in offers)
        release=release.model_copy(update={'offers':offers,'catalog':release.catalog.model_copy(update={'offers':offers})})
        app=Application(db,release,settings)
        files=PrivateFiles(private,crypto)
        class MeasuredTransport(LocalTransport):
            material_reads=0
            def timed(self,method,*values):
                began=time.monotonic()
                try:
                    return method(*values)
                finally:
                    delivery_times.append(time.monotonic()-began)
            def send_view(self,*values): return self.timed(super().send_view,*values)
            def send_material(self,*values):
                outcome=self.timed(super().send_material,*values)
                if outcome.status=='confirmed':
                    self.material_reads+=1
                return outcome
        transport=MeasuredTransport(TransportBindings(db,files))
        class MeasuredWorker(Worker):
            def _failure(self,claim,error):
                work_rejections.append(str(error.code))
                return super()._failure(claim,error)
            def _claim(self,kind):
                claim=super()._claim(kind)
                if claim:
                    with db.uow() as unit:
                        created=unit.connection.execute('SELECT created_at FROM jobs WHERE id=%s',(claim.job_id,)).fetchone()['created_at']
                    queue_times.append(max(0,(datetime.now(timezone.utc)-created).total_seconds()))
                return claim
            def _poll(self):
                active=self.render
                if active is not None and active.future.done():
                    render_times.append(time.monotonic()-active.started)
                return super()._poll()
        worker=MeasuredWorker(settings,db,app,files,transport)
        def service():
            while not stop.is_set():
                if pause_worker.is_set():
                    worker_paused.set()
                    stop.wait(0.01)
                    continue
                worker_paused.clear()
                try:
                    worker.tick()
                except Exception:
                    errors.append('worker_iteration_failed')
                stop.wait(0.01)
        thread=threading.Thread(target=service,name='qa-one-worker')
        thread.start()
        tasks=deque()
        burst_start=time.monotonic()
        for index in range(args.actors):
            ctx=create_demo_actor(settings,db)
            generator=dialogue(app,db,ctx,'support' if index%2 else 'purchase')
            step=next(generator)
            began=time.monotonic()
            value=step.call()
            command_times.append(time.monotonic()-began)
            event_times.append(time.monotonic())
            tasks.append((generator,generator.send(value),ctx.owner_id))
        burst_elapsed=time.monotonic()-burst_start
        # Dialogues await their initial view before sending the next user event.
        # The burst still queues every start without waiting between actors.
        while time.monotonic()-start<args.timeout:
            with db.uow() as unit:
                pending=unit.connection.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running','retry_wait')").fetchone()['n']
            if not pending:
                break
            stop.wait(0.025)
        steady_start=time.monotonic()
        next_event=steady_start
        steady_count=0
        steady_arrivals=[]
        held=deque()
        document_started=False
        while tasks and time.monotonic()-start<args.timeout:
            generator,step,owner=tasks.popleft()
            if mode=='documents' and not document_started and step.phase=='confirm_bundle':
                held.append((generator,step,owner))
                if not tasks:
                    # Every frozen preview is prepared before document load.
                    while time.monotonic()-start<args.timeout:
                        with db.uow() as unit:
                            pending=unit.connection.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running','retry_wait')").fetchone()['n']
                        if not pending:
                            break
                        stop.wait(0.01)
                    pause_worker.set()
                    if not worker_paused.wait(5):
                        raise RuntimeError('worker_pause_timeout')
                    command_times.clear();queue_times.clear();render_times.clear();delivery_times.clear()
                    steady_arrivals.clear();steady_count=0
                    document_started=True
                    steady_start=time.monotonic()
                    for held_generator,confirm,held_owner in held:
                        began=time.monotonic()
                        confirmed=confirm.call()
                        command_times.append(time.monotonic()-began)
                        tasks.append((held_generator,held_generator.send(confirmed),held_owner))
                    with db.uow() as unit:
                        queued_documents=unit.connection.execute("SELECT count(*) AS n FROM jobs WHERE kind='render_artifact' AND status='queued'").fetchone()['n']
                    document_queue_seconds=time.monotonic()-steady_start
                    held.clear()
                    pause_worker.clear()
                continue
            if step.event:
                with db.uow() as unit:
                    awaiting_view=unit.connection.execute("SELECT 1 FROM jobs WHERE owner_id=%s AND kind='deliver_outbox' AND status IN ('queued','running','retry_wait') LIMIT 1",(owner,)).fetchone()
                if awaiting_view:
                    tasks.append((generator,step,owner))
                    stop.wait(0.005)
                    continue
                if mode=='dialogs':
                    pause=next_event-time.monotonic()
                    if pause>0:
                        stop.wait(pause)
                began=time.monotonic()
                if mode=='dialogs':
                    next_event+=1/args.rate
                # Keep target admission cadence independent of facade compute.
                # At most one second of missed cadence is caught up; actor
                # delivery guards above still prevent screen overtaking.
                next_event=max(next_event,began-1.0)
            try:
                value=step.call()
                if step.event:
                    command_times.append(time.monotonic()-began)
                    event_times.append(time.monotonic())
                    steady_arrivals.append(began)
                    steady_count+=1
                tasks.append((generator,generator.send(value),owner))
            except StopIteration as finished:
                completed+=1
                requested+=finished.value or 0
            except Exception:
                errors.append('dialogue_failed')
            if tasks and all(not pending.event for _,pending,_ in tasks):
                stop.wait(0.01)
        steady_elapsed=time.monotonic()-steady_start
        while time.monotonic()-start<args.timeout:
            with db.uow() as unit:
                remaining=unit.connection.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running','retry_wait')").fetchone()['n']
            if remaining==0:
                break
            stop.wait(0.025)
        if tasks or remaining:
            errors.append('deadline_exceeded')
        stop.set()
        thread.join(timeout=5)
        if thread.is_alive():
            errors.append('worker_stop_timeout')
        worker.close()
        worker=None
        with db.uow() as unit:
            jobs={row['status']:row['n'] for row in unit.connection.execute('SELECT status,count(*) AS n FROM jobs GROUP BY status').fetchall()}
            outbox={row['status']:row['n'] for row in unit.connection.execute('SELECT status,count(*) AS n FROM outbox_messages GROUP BY status').fetchall()}
            documents=unit.connection.execute("SELECT count(*) AS n FROM document_artifacts WHERE status='published'").fetchone()['n']
        if jobs.get('failed',0) or outbox.get('delivery_unknown',0) or outbox.get('definitely_rejected',0):
            errors.append('durable_failure')
        observed=(len(steady_arrivals)-1)/(steady_arrivals[-1]-steady_arrivals[0]) if len(steady_arrivals)>1 else 0
        if mode=='dialogs' and observed<args.rate*0.98:
            errors.append('target_rate_not_met')
        if mode=='documents' and (queued_documents is None or queued_documents<args.min_documents):
            errors.append('document_queue_target_not_met')
        passed=not errors and completed==args.actors and documents>=args.min_documents and requested>=args.min_documents and transport.material_reads>=args.min_documents
        result={'passed':passed,'external_max_calls':0,'workload':{'mode':mode,'actors':args.actors,'synthetic_snapshots':len(offers),
            'public_snapshots':0,'burst_events':args.actors,'target_steady_events_per_second':args.rate if mode=='dialogs' else None,
            'steady_events':steady_count,'observed_steady_events_per_second':round(observed,3) if mode=='dialogs' else None,
            'burst_events_per_second':round(args.actors/burst_elapsed,3),'event_boundary':'application facade (not HTTP)'},
            'completed_dialogues':completed,'ready_documents':documents,'requested_documents':requested,
            'downloaded_documents_local':transport.material_reads,'queued_documents_at_burst':queued_documents,
            'document_queue_seconds':round(document_queue_seconds,3) if document_queue_seconds is not None else None,
            'durable_jobs':jobs,'durable_outbox':outbox,'error_count':len(errors),'error_codes':dict(Counter(errors)),
            'guarded_work_rejections':dict(Counter(work_rejections)),
            'duration_seconds':round(time.monotonic()-start,3),'document_load_seconds':round(steady_elapsed,3) if mode=='documents' else None,
            'p95_ms':{'queue_wait':percentile(queue_times),
            'facade_compute':percentile(command_times),'render_compute_and_poll':percentile(render_times),
            'local_delivery':percentile(delivery_times)},'sample_counts':{'queue':len(queue_times),'facade':len(command_times),
            'render':len(render_times),'delivery':len(delivery_times)},
            'bounds':{'render_processes':1,'delivery_threads':1,'timeout_seconds':args.timeout},
            'methodology':'Dialogs: initial start burst, then deadline-based cadence independent of compute, bounded one-second catch-up and per-actor preceding-view guards. Observed arrivals use first/last steady admission and exclude completion wait; target tolerance2%. Documents: unpaced setup to all frozen previews, paused worker while confirmations queue all render jobs, then one worker handles burst/render/local downloads; metrics reset after setup. Queue delay is created_at to claim; render includes spawn/poll. Synthetic-only isolated schema removed. Receipts/uploads/download reads are local simulations. No proxy/network/provider measurements.'}
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False))
        return 0 if passed else 1
    finally:
        stop.set()
        pause_worker.clear()
        if thread is not None:
            thread.join(timeout=5)
        if worker is not None:
            worker.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))
        import shutil
        shutil.rmtree(private,ignore_errors=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('dialogs','documents'),default='dialogs')
    parser.add_argument('--actors',type=int)
    parser.add_argument('--snapshots',type=int,default=1000)
    parser.add_argument('--rate',type=float,default=5)
    parser.add_argument('--min-documents',type=int,default=100)
    parser.add_argument('--timeout',type=float,default=360)
    parser.add_argument('--output',type=Path,default=Path('var/qa/load-check.json'))
    args=parser.parse_args()
    args.actors=args.actors or (40 if args.mode=='documents' else 50)
    if not 1<=args.actors<=50 or not 1<=args.snapshots<=1000 or not 0<args.rate<=50 or not 0<args.timeout<=600 or args.min_documents<0:
        parser.error('Workload exceeds bounded offline limits')
    try:
        return run(args)
    except Exception:
        print('Offline load check failed safely; no live MAX call was attempted.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
