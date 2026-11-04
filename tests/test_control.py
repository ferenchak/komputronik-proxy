import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from discovery_control import Controller


class Sheet:
    def __init__(self):
        self.signal='FALSE'
        self.status=[]
    def acell(self, address):
        assert address=='B3'
        return type('Cell',(),{'value':self.signal})()
    def update(self, *, range_name, values, **kwargs):
        if range_name=='B3':
            self.signal=values[0][0]
        else:
            self.status=values
    def col_values(self, column):
        return ['url','product']


class Book:
    def __init__(self):
        self.sheet=Sheet()
    def worksheet(self, title):
        return self.sheet


class Process:
    def __init__(self):
        self.code=None
    def poll(self):
        return self.code


def test_checkbox_runs_once_and_ignores_repeat_while_active(tmp_path):
    book=Book(); starts=[]
    def factory(args):
        starts.append(args)
        return Process()
    controller=Controller(book,tmp_path,factory)
    controller.tick()
    assert not starts
    book.sheet.signal='TRUE'
    controller.tick()
    assert len(starts)==1 and book.sheet.signal is False
    book.sheet.signal=True
    controller.tick()
    assert len(starts)==1 and book.sheet.signal is False
    controller.process.code=0
    controller.tick()
    assert controller.process is None and not controller.state['running']
    assert book.sheet.status[0][0]=='Сканування завершено'


def test_controller_restart_resumes_only_an_existing_request(tmp_path):
    state={'running':True,'pending':False,'last_start':'old'}
    (tmp_path/'control_state.json').write_text(json.dumps(state))
    starts=[]
    controller=Controller(Book(),tmp_path,lambda args: starts.append(args) or Process())
    controller.tick()
    assert len(starts)==1
    assert json.loads((tmp_path/'control_state.json').read_text())['running'] is True


def test_idle_controller_restart_does_not_start_a_scan(tmp_path):
    (tmp_path/'control_state.json').write_text(json.dumps({'running':False,'last_exit':0}))
    starts=[]
    controller=Controller(Book(),tmp_path,lambda args: starts.append(args) or Process())
    controller.tick()
    assert not starts


def test_failed_spawn_preserves_pending_request(tmp_path):
    book=Book(); book.sheet.signal=True
    def fail(args):
        raise OSError('temporary spawn error')
    controller=Controller(book,tmp_path,fail)
    try:
        controller.tick()
    except OSError:
        pass
    assert json.loads((tmp_path/'control_state.json').read_text())['pending'] is True
    assert book.sheet.signal is False
