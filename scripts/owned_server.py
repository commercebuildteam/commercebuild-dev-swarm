"""Staged owned-child server lifecycle. No adoption or signaling of other PIDs."""
import base64,json,queue,re,subprocess,threading,time,urllib.request
class OwnedServer:
 def __init__(self,command,url,*,cwd,env):
  from server_transport import ServerTransport
  ServerTransport(url,'validation-only',cwd=cwd,model=None)
  self.command=command;self.url=url.rstrip('/');self.cwd=cwd;self.env=env
  self.process=None;self.password=None;self.lines=queue.Queue()
 def start(self,timeout=10):
  if self.process is not None:raise RuntimeError('Server already started')
  self.process=subprocess.Popen(self.command,cwd=self.cwd,env=self.env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
  def drain():
   for line in self.process.stdout:
    match=re.search(r'server password (\S+)',line)
    if match:self.lines.put(match.group(1))
   self.lines.put(None)
  threading.Thread(target=drain,daemon=True).start()
  try:
   self.password=self.lines.get(timeout=timeout)
   if not self.password or self.process.poll() is not None:raise RuntimeError('Server startup failed')
   self.active_sessions() # authenticated health, not just an open port
   return self
  except Exception:
   self._terminate_child();raise RuntimeError('Owned server failed authenticated startup') from None
 def active_sessions(self):
  if not self.process or self.process.poll() is not None:raise RuntimeError('Owned server unavailable')
  req=urllib.request.Request(self.url+'/api/session/active',headers={'Authorization':'Basic '+base64.b64encode(('opencode:'+self.password).encode()).decode()})
  from local_api import open_local
  with open_local(req,timeout=5) as response:data=json.loads(response.read(2000000))
  if not isinstance(data.get('data'),dict):raise RuntimeError('Invalid active session response')
  return data['data']
 def stop(self,*,unresolved):
  # Caller holds ownership lock. Unknown sessions or guards keep the child alive.
  if unresolved():return False
  try:
   if self.active_sessions() or unresolved():return False
  except Exception:return False
  self._terminate_child();return True
 def _terminate_child(self):
  if self.process and self.process.poll() is None:
   self.process.terminate()
   try:self.process.wait(timeout=5)
   except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
  if self.process and self.process.stdout:self.process.stdout.close()
  self.password=None
