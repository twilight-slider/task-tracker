import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from ars_jobs import ArsJobs, AdapterError

class Fake:
    def __init__(self): self.creates=[];self.commands=[];self.lost=False;self.rows=[];self.remote_status='running'
    def ensure_available(self,seconds=10): return {'alive':True}
    def create(self,payload,key,request_id=None):
        self.creates.append((payload,key))
        if self.lost:self.lost=False;raise OSError('lost response')
        return {'job_id':'a'*32,'status':'running'}
    def command(self,job,method,params,key,request_id=None):
        self.commands.append((method,params,key));return {'status':'queued'}
    def status(self,job): return {'status':self.remote_status,'session_id':'native-own'}
    def events(self,job,after_seq=0): return [r for r in self.rows if r['seq']>after_seq]
    def stop(self,job,key,request_id=None): self.remote_status='stopped';return {'status':'stopped'}
class AdapterTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'.runtime/tests/ars-jobs';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup)
        self.fake=Fake();self.config={'bindings':{'TEST-1':{'schema_version':1,'profile_id':'codex-base','state_root':'state'}}}
        self.adapter=ArsJobs(Path(self.temp.name)/'jobs.sqlite',self.config,lambda:self.fake)
    def start(self): return self.adapter.call('start',{'thread_id':'parent','task_key':'TEST-1','prompt':'first'})
    def test_lost_create_reuses_saved_intent(self):
        self.fake.lost=True
        with self.assertRaises(AdapterError):self.start()
        result=self.start()
        self.assertEqual(self.fake.creates[0],self.fake.creates[1])
        self.assertEqual(len(self.fake.commands),1)
        self.assertEqual(self.start()['job_id'],result['job_id'])
        self.assertEqual(len(self.fake.commands),1)
    def test_waiting_reply_same_ars_job_and_control_key(self):
        result=self.start();key=self.fake.commands[-1][2]
        self.fake.rows=[{'seq':1,'event':'answer','request_id':key,'session_id':'native-own','payload':{'text':'first answer'}},
            {'seq':2,'event':'turn/completed','request_id':key,'payload':{'status':'completed'}}]
        args={'job_id':result['job_id'],'control_key':result['control_key']}
        status=self.adapter.call('status',args)
        self.assertEqual(status['status'],'waiting');self.assertEqual(status['result'],'first answer')
        reply=self.adapter.call('reply',{**args,'reply':'second','idempotency_key':'second-reply'})
        self.assertEqual(reply['job_id'],result['job_id'])
        self.assertEqual(len(self.fake.creates),1)
        self.assertEqual(self.adapter.call('status',args)['status'],'running')
        with self.assertRaises(AdapterError):self.adapter.call('status',{**args,'control_key':'wrong'})
    def test_restart_pending_command_reuses_key(self):
        original=self.fake.command
        def lost(*args,**kwargs):
            original(*args,**kwargs)
            self.fake.command=original
            raise OSError('lost command response')
        self.fake.command=lost
        with self.assertRaises(AdapterError):self.start()
        first=self.fake.commands[0][2]
        reopened=ArsJobs(Path(self.temp.name)/'jobs.sqlite',self.config,lambda:self.fake)
        reopened.call('start',{'thread_id':'parent','task_key':'TEST-1','prompt':'first'})
        self.assertEqual(len(self.fake.commands),2)
        self.assertEqual(self.fake.commands[1][2],first)
        self.assertEqual(len({entry[2] for entry in self.fake.commands}),1)
    def test_parent_task_binding_and_unknown_capability_fail(self):
        self.start()
        with self.assertRaises(AdapterError):self.adapter.call('start',{'thread_id':'parent','task_key':'TEST-2','prompt':'first'})
        with self.assertRaises(AdapterError):self.adapter.call('update_repositories',{})
    def test_stop_does_not_report_success_on_uncertainty(self):
        result=self.start();args={'job_id':result['job_id'],'control_key':result['control_key']}
        self.fake.stop=lambda *a,**k: {'status':'indeterminate'}
        self.assertEqual(self.adapter.call('stop',args)['status'],'unknown')

    def test_failed_turn_keeps_parent_reserved_until_stop(self):
        result=self.start();key=self.fake.commands[-1][2]
        self.fake.rows=[{'seq':1,'event':'turn/completed','request_id':key,'payload':{'status':'failed'}}]
        args={'job_id':result['job_id'],'control_key':result['control_key']}
        self.assertEqual(self.adapter.call('status',args)['status'],'unknown')
        with self.assertRaises(AdapterError):self.adapter.call('start',{'thread_id':'parent','task_key':'TEST-1','prompt':'new'})
        self.assertEqual(self.adapter.call('stop',args)['status'],'stopped')
    def test_different_parents_same_task_not_coordinated(self):
        self.start()
        other=self.adapter.call('start',{'thread_id':'other','task_key':'TEST-1','prompt':'first'})
        self.assertTrue(other['job_id'])

    def test_confirmed_create_rejection_can_be_corrected(self):
        class Rejected(Exception):
            code='FORBIDDEN';status=403
        original=self.fake.create
        self.fake.create=lambda *args,**kwargs: (_ for _ in ()).throw(Rejected())
        with self.assertRaises(AdapterError): self.start()
        self.fake.create=original
        self.assertTrue(self.start()['job_id'])

    def test_reply_completed_replay_uses_durable_operation_key(self):
        result=self.start();args={'job_id':result['job_id'],'control_key':result['control_key']}
        def complete(text):
            key=self.fake.commands[-1][2];n=len(self.fake.rows)
            self.fake.rows.extend([{'seq':n+1,'event':'answer','request_id':key,'payload':{'text':text}},
                {'seq':n+2,'event':'turn/completed','request_id':key,'payload':{'status':'completed'}}])
        complete('first');self.adapter.call('status',args)
        with self.assertRaisesRegex(AdapterError,'IDEMPOTENCY_KEY_REQUIRED'):
            self.adapter.call('reply',{**args,'reply':'same'})
        reply={**args,'reply':'same','idempotency_key':'operation-1'}
        original=self.fake.command
        def lost(*a,**k):
            original(*a,**k);self.fake.command=original;raise OSError('lost reply acknowledgement')
        self.fake.command=lost
        with self.assertRaises(AdapterError):self.adapter.call('reply',reply)
        first={'job_id':result['job_id'],'status':'queued'}
        complete('second');self.adapter.call('status',args)
        reopened=ArsJobs(Path(self.temp.name)/'jobs.sqlite',self.config,lambda:self.fake)
        self.assertEqual(reopened.call('reply',reply),first)
        self.assertEqual(len({entry[2] for entry in self.fake.commands}),2)
        with self.assertRaisesRegex(AdapterError,'IDEMPOTENCY_CONFLICT'):
            reopened.call('reply',{**reply,'reply':'different'})
        reopened.call('reply',{**reply,'idempotency_key':'operation-2'})
        self.assertEqual(len({entry[2] for entry in self.fake.commands}),3)

    def test_worker_selects_ars_backend(self):
        from unittest.mock import patch
        import folder_worker
        worker=object.__new__(folder_worker.Worker)
        class Folder:
            def config(self):return {'arsJobs':{'bindings':{}}}
            def resolve_task_folder(self,key):return {'taskFolder':'fixture'}
        worker.folder=Folder();worker._ars_jobs=None
        with patch('ars_jobs.from_service',return_value=self.adapter):
            result=worker.job('start',{'thread_id':'parent','task_key':'TEST-1','prompt':'first'})
        self.assertTrue(result['job_id']);self.assertEqual(len(self.fake.creates),1)
        status=worker.job('status',{'job_id':result['job_id'],'control_key':result['control_key']})
        self.assertNotIn('events',status)
