"""Conservative stale-owner reconciliation. Caller holds server-owner.lock."""
import datetime,json,os,uuid

def process_alive(pid):
 if type(pid) is not int or pid<=0:return None
 try:os.kill(pid,0);return True
 except ProcessLookupError:return False
 except (PermissionError,OSError):return None

def reconcile_dead_owner(state,alive=process_alive):
 receipt=state/'server-owner.json'
 if not receipt.exists():return 'absent'
 try:record=json.loads(receipt.read_text())
 except (OSError,ValueError):return 'blocked-invalid-receipt'
 if not isinstance(record,dict):return 'blocked-invalid-receipt'
 if any((state/'quarantine').glob('*.json')):return 'blocked-quarantine'
 # Missing PID after a start-time crash cannot prove no server was created.
 if alive(record.get('launcher_pid')) is not False:return 'blocked-launcher-unconfirmed'
 if alive(record.get('server_pid')) is not False:return 'blocked-server-unconfirmed'
 archive=state/'server-owner-history';archive.mkdir(exist_ok=True)
 target=archive/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex+'.json')
 os.replace(receipt,target)
 return 'archived-dead-owner'
