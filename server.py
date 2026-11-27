#!/usr/bin/env python3
"""Local-first SQLite dashboard; writes allowed only from loopback."""
import argparse, json, os, sqlite3, threading, ipaddress, math
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parent
DB=ROOT/'data'/'finance.sqlite3'
LOCK=threading.RLock()
MAX_BYTES=5_000_000
# Deliberately fixed historical adjustment. It is appended on every read rather
# than stored in SQLite, so imports, restores, and clear operations cannot alter it.
WEBULL_ADJUSTMENT={'date':'2026-08-18','account':'w','action':'adjustment','kind':'BALANCE','symbol':'WEBULL','expiry':'','strike':'','amount':-1037.00}

def now():return datetime.now(timezone.utc).isoformat()
def connect():
    c=sqlite3.connect(DB,timeout=15)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS transactions (position INTEGER PRIMARY KEY, date TEXT NOT NULL, account TEXT NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT NOT NULL, expiry TEXT NOT NULL, strike TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1, amount REAL NOT NULL)')
    if 'quantity' not in {row[1] for row in c.execute('PRAGMA table_info(transactions)')}:
        c.execute('ALTER TABLE transactions ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1')
    c.execute('CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS income_inputs (period TEXT NOT NULL, account TEXT NOT NULL, field TEXT NOT NULL, amount REAL NOT NULL, PRIMARY KEY (period,account,field))')
    c.execute('CREATE TABLE IF NOT EXISTS income_tracker_entries (id TEXT PRIMARY KEY, category TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL, date TEXT NOT NULL, tag TEXT NOT NULL DEFAULT \'\')')
    if 'tag' not in {row[1] for row in c.execute('PRAGMA table_info(income_tracker_entries)')}:
        c.execute("ALTER TABLE income_tracker_entries ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
    c.execute('CREATE TABLE IF NOT EXISTS income_tracker_subscriptions (id TEXT PRIMARY KEY, cadence TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL, due_date TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS trading_workspace_settings (name TEXT PRIMARY KEY, amount REAL NOT NULL, updated_at TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS trading_workspace_adjustments (id INTEGER PRIMARY KEY AUTOINCREMENT, previous_amount REAL NOT NULL, updated_amount REAL NOT NULL, difference REAL NOT NULL, modified_at TEXT NOT NULL)')
    return c

def validate(rows):
    if not isinstance(rows,list) or len(rows)>100000:raise ValueError('Invalid transaction list')
    for x in rows:
        if not isinstance(x,dict) or not all(k in x for k in ('date','account','action','kind','symbol','expiry','strike','amount')):raise ValueError('Invalid transaction')
        if x['account'] not in ('i','r') or x['action'] not in ('buy','sell') or x['kind'] not in ('PUT','CALL'):raise ValueError('Invalid transaction attributes')
        if not isinstance(x['amount'],(int,float)) or not -1e9<float(x['amount'])<1e9:raise ValueError('Invalid amount')
        if 'quantity' in x and (not isinstance(x['quantity'],int) or isinstance(x['quantity'],bool) or not 0<x['quantity']<=100000):raise ValueError('Invalid contract quantity')
        if not isinstance(x['date'],str) or len(x['date'])!=10:raise ValueError('Invalid date')
        datetime.strptime(x['date'],'%Y-%m-%d')
        if any(not isinstance(x[k],str) or len(x[k])>80 for k in ('symbol','expiry','strike')):raise ValueError('Invalid contract')

def read_state(c):
    entries=[dict(zip(('date','account','action','kind','symbol','expiry','strike','quantity','amount'),r)) for r in c.execute('SELECT date,account,action,kind,symbol,expiry,strike,quantity,amount FROM transactions ORDER BY position')]
    entries.append(WEBULL_ADJUSTMENT.copy())
    meta=dict(c.execute('SELECT name,value FROM meta'))
    return {'entries':entries,'revision':int(meta.get('revision','0')),'updated_at':meta.get('updated_at')}

def write_state(c,entries,revision):
    c.execute('DELETE FROM transactions')
    c.executemany('INSERT INTO transactions(position,date,account,action,kind,symbol,expiry,strike,quantity,amount) VALUES (?,?,?,?,?,?,?,?,?,?)',[(i,x['date'],x['account'],x['action'],x['kind'],x['symbol'],x['expiry'],x['strike'],x.get('quantity',1),x['amount']) for i,x in enumerate(entries)])
    c.execute("INSERT OR REPLACE INTO meta(name,value) VALUES ('revision',?)",(str(revision),))
    c.execute("INSERT OR REPLACE INTO meta(name,value) VALUES ('updated_at',?)",(now(),))

def read_income_inputs(c):
    values={}
    for period,account,field,amount in c.execute('SELECT period,account,field,amount FROM income_inputs ORDER BY period,account,field'):
        values.setdefault(period,{}).setdefault(account,{})[field]=amount
    return {'values':values}

def validate_income_inputs(data):
    if not isinstance(data,dict) or not isinstance(data.get('period'),str) or not isinstance(data.get('rows'),dict):raise ValueError('Invalid income input data')
    datetime.strptime(data['period'],'%Y-%m')
    if len(data['rows'])>20:raise ValueError('Too many income rows')
    accounts={'Net Income','GAS','Net checking','Subscription','NET HYSA','RothIRA','Mom','Holiday'}
    fields={'planned_first','actual_first','planned_second','actual_second'}
    for account,row in data['rows'].items():
        if account not in accounts or not isinstance(row,dict) or len(row)>len(fields):raise ValueError('Invalid income row')
        for field,amount in row.items():
            if field not in fields or not isinstance(amount,(int,float)) or not -1e9<float(amount)<1e9:raise ValueError('Invalid income amount')

def write_income_inputs(c,data):
    c.execute('DELETE FROM income_inputs WHERE period=?',(data['period'],))
    c.executemany('INSERT INTO income_inputs(period,account,field,amount) VALUES (?,?,?,?)',[(data['period'],account,field,amount) for account,row in data['rows'].items() for field,amount in row.items()])

def read_income_tracker(c):
    entries=[dict(zip(('id','category','description','amount','date','tag'),row)) for row in c.execute('SELECT id,category,description,amount,date,tag FROM income_tracker_entries ORDER BY date DESC,id DESC')]
    for entry in entries:
        if entry['category']=='Spent' and not entry['tag'] and ('gas' in entry['description'].lower() or 'fuel' in entry['description'].lower()):
            entry['tag']='Gas'
    subscriptions=[dict(zip(('id','cadence','description','amount','due_date'),row)) for row in c.execute('SELECT id,cadence,description,amount,due_date FROM income_tracker_subscriptions ORDER BY due_date,id')]
    return {'entries':entries,'subscriptions':subscriptions}

def validate_income_tracker(data):
    if not isinstance(data,dict) or not isinstance(data.get('entries'),list) or not isinstance(data.get('subscriptions'),list):raise ValueError('Invalid income tracker data')
    if len(data['entries'])>10000 or len(data['subscriptions'])>1000:raise ValueError('Too many income tracker records')
    categories={'WTDC','Gift','Trading Payout','Other','Spent','GasBonus'}
    ids=set(); gas_bonus_months=set()
    for entry in data['entries']:
        if not isinstance(entry,dict) or not {'id','category','description','amount','date'}<=set(entry)<= {'id','category','description','amount','date','tag'}:raise ValueError('Invalid income entry')
        if not isinstance(entry['id'],str) or not entry['id'] or len(entry['id'])>80 or entry['id'] in ids:raise ValueError('Invalid income entry ID')
        ids.add(entry['id'])
        if entry['category'] not in categories or not isinstance(entry['description'],str) or len(entry['description'])>200:raise ValueError('Invalid income entry details')
        if entry['category']=='Spent' and not entry.get('tag','') and ('gas' in entry['description'].lower() or 'fuel' in entry['description'].lower()):entry['tag']='Gas'
        if entry.get('tag','') not in {'','Mom','Day Trading','Gas'}:raise ValueError('Invalid income entry tag')
        if entry['category']=='Spent' and not entry['description'].strip():raise ValueError('A spending description is required')
        if not isinstance(entry['amount'],(int,float)) or not 0<=float(entry['amount'])<1e9:raise ValueError('Invalid income entry amount')
        if not isinstance(entry['date'],str) or len(entry['date'])!=10:raise ValueError('Invalid income entry date')
        datetime.strptime(entry['date'],'%Y-%m-%d')
        if entry['category']=='GasBonus':
            month=entry['date'][:7]
            if month in gas_bonus_months:raise ValueError('Only one GasBonus entry is allowed per month')
            gas_bonus_months.add(month)
    ids=set()
    for subscription in data['subscriptions']:
        if not isinstance(subscription,dict) or set(subscription)!={'id','cadence','description','amount','due_date'}:raise ValueError('Invalid subscription')
        if not isinstance(subscription['id'],str) or not subscription['id'] or len(subscription['id'])>80 or subscription['id'] in ids:raise ValueError('Invalid subscription ID')
        ids.add(subscription['id'])
        if subscription['cadence'] not in {'Monthly','Yearly'} or not isinstance(subscription['description'],str) or not subscription['description'].strip() or len(subscription['description'])>200:raise ValueError('Invalid subscription details')
        if not isinstance(subscription['amount'],(int,float)) or not 0<=float(subscription['amount'])<1e9:raise ValueError('Invalid subscription amount')
        if not isinstance(subscription['due_date'],str) or len(subscription['due_date'])!=10:raise ValueError('Invalid subscription due date')
        datetime.strptime(subscription['due_date'],'%Y-%m-%d')

def write_income_tracker(c,data):
    c.execute('DELETE FROM income_tracker_entries')
    c.execute('DELETE FROM income_tracker_subscriptions')
    c.executemany('INSERT INTO income_tracker_entries(id,category,description,amount,date,tag) VALUES (?,?,?,?,?,?)',[(x['id'],x['category'],x['description'],x['amount'],x['date'],x.get('tag','')) for x in data['entries']])
    c.executemany('INSERT INTO income_tracker_subscriptions(id,cadence,description,amount,due_date) VALUES (?,?,?,?,?)',[(x['id'],x['cadence'],x['description'],x['amount'],x['due_date']) for x in data['subscriptions']])

def workspace_state(c):
    row=c.execute("SELECT amount,updated_at FROM trading_workspace_settings WHERE name='hysa_deposit'").fetchone()
    history=[dict(zip(('previous_amount','updated_amount','difference','modified_at'),record)) for record in c.execute('SELECT previous_amount,updated_amount,difference,modified_at FROM trading_workspace_adjustments ORDER BY id DESC')]
    day_trading_expenses=c.execute("SELECT COALESCE(SUM(amount),0) FROM income_tracker_entries WHERE category='Spent' AND tag='Day Trading'").fetchone()[0]
    return {'hysa_deposit_amount':row[0] if row else 0,'updated_at':row[1] if row else None,'adjustment_history':history,'day_trading_expenses':day_trading_expenses}

def validate_workspace_amount(amount):
    if not isinstance(amount,(int,float)) or isinstance(amount,bool) or not math.isfinite(amount) or not 0<=float(amount)<1e9:raise ValueError('HYSA deposit must be a valid non-negative amount')

def update_workspace_deposit(c,amount,action):
    validate_workspace_amount(amount)
    if action not in ('deposit','withdraw'):raise ValueError('Invalid HYSA adjustment type')
    current=workspace_state(c)['hysa_deposit_amount']
    amount=round(float(amount),2)
    if amount<=0:raise ValueError('Enter an amount greater than zero')
    updated=round(current+amount if action=='deposit' else current-amount,2)
    if updated<0:raise ValueError('Withdrawal cannot exceed the saved HYSA balance')
    if updated!=current:
        timestamp=now()
        c.execute("INSERT OR REPLACE INTO trading_workspace_settings(name,amount,updated_at) VALUES ('hysa_deposit',?,?)",(updated,timestamp))
        c.execute('INSERT INTO trading_workspace_adjustments(previous_amount,updated_amount,difference,modified_at) VALUES (?,?,?,?)',(current,updated,round(updated-current,2),timestamp))
    return workspace_state(c)

def restore_workspace(c,data):
    if not isinstance(data,dict) or set(data)!={'hysa_deposit_amount','adjustment_history'}:raise ValueError('Invalid trading workspace backup')
    validate_workspace_amount(data['hysa_deposit_amount'])
    history=data['adjustment_history']
    if not isinstance(history,list) or len(history)>10000:raise ValueError('Invalid adjustment history')
    parsed=[]
    for item in history:
        if not isinstance(item,dict) or set(item)!={'previous_amount','updated_amount','difference','modified_at'}:raise ValueError('Invalid adjustment record')
        for field in ('previous_amount','updated_amount','difference'):validate_workspace_amount(abs(item[field]))
        if not isinstance(item['modified_at'],str) or len(item['modified_at'])>64:raise ValueError('Invalid adjustment timestamp')
        parsed.append((item['previous_amount'],item['updated_amount'],item['difference'],item['modified_at']))
    timestamp=now()
    c.execute("INSERT OR REPLACE INTO trading_workspace_settings(name,amount,updated_at) VALUES ('hysa_deposit',?,?)",(round(float(data['hysa_deposit_amount']),2),timestamp))
    c.execute('DELETE FROM trading_workspace_adjustments')
    c.executemany('INSERT INTO trading_workspace_adjustments(previous_amount,updated_amount,difference,modified_at) VALUES (?,?,?,?)',parsed[::-1])
    return workspace_state(c)

def clear_workspace_history(c):
    c.execute('DELETE FROM trading_workspace_adjustments')
    c.execute("DELETE FROM trading_workspace_settings WHERE name='hysa_deposit'")
    return workspace_state(c)

def initialize():
    DB.parent.mkdir(parents=True,exist_ok=True)
    with LOCK,connect() as c:
        if c.execute('SELECT COUNT(*) FROM meta').fetchone()[0]==0:
            # baseline.json is optional and intentionally ignored by Git so a
            # public clone never requires or publishes a private trade history.
            baseline=ROOT/'baseline.json'
            base=json.loads(baseline.read_text()) if baseline.exists() else []
            validate(base);write_state(c,base,1)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT/'web'),**kw)
    def loopback(self):return self.server.admin and ipaddress.ip_address(self.client_address[0]).is_loopback
    def send_json(self,status,obj):
        raw=json.dumps(obj,separators=(',',':')).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if urlsplit(self.path).path=='/api/state':
            with LOCK,connect() as c:s=read_state(c)
            s['can_edit']=self.loopback();return self.send_json(200,s)
        if urlsplit(self.path).path=='/api/income-inputs':
            with LOCK,connect() as c:return self.send_json(200,read_income_inputs(c))
        if urlsplit(self.path).path=='/api/income-tracker':
            with LOCK,connect() as c:data=read_income_tracker(c)
            data['can_edit']=self.loopback();return self.send_json(200,data)
        if urlsplit(self.path).path=='/api/trading-workspace':
            with LOCK,connect() as c:data=workspace_state(c)
            data['can_edit']=self.loopback();return self.send_json(200,data)
        if urlsplit(self.path).path=='/api/health':return self.send_json(200,{'status':'ok'})
        return super().do_GET()
    def do_PUT(self):
        path=urlsplit(self.path).path
        if path not in ('/api/state','/api/income-inputs','/api/income-tracker','/api/trading-workspace','/api/trading-workspace/restore','/api/trading-workspace/history/clear'):return self.send_json(404,{'error':'Not found'})
        if not self.loopback():return self.send_json(403,{'error':'Remote access is read-only'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<1 or length>MAX_BYTES:return self.send_json(413,{'error':'Invalid payload size'})
            data=json.loads(self.rfile.read(length))
            if path=='/api/income-inputs':
                validate_income_inputs(data)
                with LOCK,connect() as c:
                    write_income_inputs(c,data)
                    return self.send_json(200,{'updated_at':now()})
            if path=='/api/income-tracker':
                validate_income_tracker(data)
                with LOCK,connect() as c:
                    write_income_tracker(c,data)
                    return self.send_json(200,{'updated_at':now()})
            if path=='/api/trading-workspace':
                with LOCK,connect() as c:return self.send_json(200,update_workspace_deposit(c,data.get('amount'),data.get('action')))
            if path=='/api/trading-workspace/restore':
                with LOCK,connect() as c:return self.send_json(200,restore_workspace(c,data))
            if path=='/api/trading-workspace/history/clear':
                with LOCK,connect() as c:return self.send_json(200,clear_workspace_history(c))
            rows=data['entries'];validate(rows)
            with LOCK,connect() as c:
                current=read_state(c)['revision']
                if data.get('expected_revision')!=current:return self.send_json(409,{'error':'Revision conflict; refresh and retry','revision':current})
                write_state(c,rows,current+1)
                return self.send_json(200,{'revision':current+1,'updated_at':now()})
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:return self.send_json(400,{'error':str(e)})
    def do_POST(self):return self.send_json(405,{'error':'Method not allowed'})
    def do_DELETE(self):return self.send_json(405,{'error':'Method not allowed'})

def main():
    p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1',help='Bind to 127.0.0.1 (default) or your Tailscale IP');p.add_argument('--port',type=int,default=8765);p.add_argument('--phone-port',type=int,default=8766);a=p.parse_args()
    initialize();server=ThreadingHTTPServer((a.host,a.port),Handler);server.admin=True
    phone=ThreadingHTTPServer(('127.0.0.1',a.phone_port),Handler);phone.admin=False
    thread=threading.Thread(target=phone.serve_forever,daemon=True);thread.start()
    print(f'Mac admin: http://{a.host}:{a.port} | Phone read-only proxy target: http://127.0.0.1:{a.phone_port} | Database: {DB}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();phone.shutdown();phone.server_close()
if __name__=='__main__':main()
