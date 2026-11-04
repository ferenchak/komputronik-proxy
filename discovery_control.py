"""Manual Google Sheets checkbox controller; worker runs only on a request."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from parser_local_v8 import get_sheets_client, SPREADSHEET_NAME
from discovery_runner import retry

TAB = 'Discovery_керування'


def now():
    return datetime.now(ZoneInfo('Europe/Kyiv')).strftime('%d.%m.%Y %H:%M:%S')


def requested(value):
    return value is True or str(value).strip().upper() == 'TRUE'


def progress(directory):
    result = {'products':0, 'indexes_pending':0, 'candidates':0, 'failed':0, 'run_state':''}
    catalog = directory/'catalog.sqlite3'
    candidates = directory/'candidates.sqlite3'
    if catalog.exists():
        with sqlite3.connect(catalog.as_uri()+'?mode=ro', uri=True, timeout=5) as db:
            rows = db.execute('SELECT kind,state,count(*) FROM jobs GROUP BY kind,state').fetchall()
            result['products'] = sum(n for kind,state,n in rows if kind=='product' and state=='done')
            result['indexes_pending'] = sum(n for kind,state,n in rows if kind=='index' and state in ('pending','running'))
            result['failed'] = sum(n for kind,state,n in rows if state=='failed')
            row = db.execute('SELECT value FROM status WHERE key="run_state"').fetchone()
            result['run_state'] = row[0] if row else ''
    if candidates.exists():
        with sqlite3.connect(candidates.as_uri()+'?mode=ro', uri=True, timeout=5) as db:
            result['candidates'] = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
    return result


class Controller:
    def __init__(self, book, directory, factory=subprocess.Popen):
        self.book, self.directory, self.factory = book, directory.resolve(), factory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sheet = retry(lambda: book.worksheet(TAB))
        self.path = self.directory/'control_state.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        # Resume an already requested scan after a container restart, never a
        # new daily pass. Idle restarts stay idle.
        self.state['pending'] = bool(self.state.get('pending') or self.state.get('running'))
        self.state['running'] = False
        self.process = None
        self.result_rows = 0
        self.last_result_read = 0
        self.persist()

    def persist(self):
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        temporary.replace(self.path)

    def finish(self, code):
        self.state.update(running=False, pending=False, last_end=now(), last_exit=code)
        self.process = None
        self.persist()

    def start(self):
        self.state.update(running=True, pending=False, last_start=now(), last_exit=None)
        self.persist()
        try:
            self.process = self.factory([sys.executable, '-u', str(Path(__file__).with_name('catalog_worker.py'))])
        except Exception:
            self.state.update(running=False, pending=True)
            self.persist()
            raise
        print(json.dumps({'manual_scan_started':True}), flush=True)

    def tick(self):
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                self.finish(code)
        signal = retry(lambda: self.sheet.acell('B3').value)
        if requested(signal):
            if self.process is None and not self.state.get('pending'):
                self.state['pending'] = True
                self.persist()  # durable before acknowledging the checkbox
            retry(lambda: self.sheet.update(range_name='B3', values=[[False]], value_input_option='RAW'))
        if self.state.get('pending') and self.process is None:
            self.start()
        stats = progress(self.directory)
        if time.monotonic()-self.last_result_read>=60:
            results = retry(lambda: self.book.worksheet('product_discovery').col_values(1))
            self.result_rows = max(0,len(results)-1)
            self.last_result_read = time.monotonic()
        if self.process is not None:
            status = 'Сканування триває'
        elif self.state.get('last_exit') == 3:
            status = 'Сайт обмежив доступ. Сканування зупинено.'
        elif self.state.get('last_exit') == 0:
            status = 'Завершено з помилками сторінок' if stats['failed'] else 'Сканування завершено'
        elif self.state.get('last_exit') is not None:
            status = 'Помилка сканування. Можна повторити запуск.'
        else:
            status = 'Очікує запуску'
        values = [[v] for v in (status,now(),stats['products'],stats['indexes_pending'],
                  stats['candidates'],self.result_rows,stats['failed'],
                  self.state.get('last_start',''),self.state.get('last_end',''))]
        retry(lambda: self.sheet.update(range_name='B4:B12', values=values, value_input_option='RAW'))

    def close(self):
        if self.process is not None and self.process.poll() is not None:
            self.finish(self.process.poll())
        elif self.process is not None:
            # Keep running=True on disk so the same request resumes on restart.
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()


def main():
    client = get_sheets_client()
    key = os.environ.get('SPREADSHEET_ID')
    book = retry(lambda: client.open_by_key(key) if key else client.open(SPREADSHEET_NAME))
    controller = Controller(book,Path(os.environ.get('DISCOVERY_DATA_DIR','/data')))
    print(json.dumps({'controller_ready':True,'manual_only':True,'poll_seconds':20}),flush=True)
    try:
        while True:
            try:
                controller.tick()
            except Exception as error:
                print(json.dumps({'controller_error':type(error).__name__}),flush=True)
            time.sleep(20)
    finally:
        controller.close()


if __name__=='__main__':
    main()
