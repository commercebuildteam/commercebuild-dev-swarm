"""Bounded local-server transport for staged attached-worker execution."""
import base64,json,os,queue,re,subprocess,threading,time,urllib.request,urllib.parse
class ServerTransport:
 def __init__(self,url,password,*,cwd,model,agent=None,hard_seconds=2700,idle_seconds=600,stop=None,owns_session=None,executable='opencode',log_path=None,on_start=None,on_event=None,skip_permissions=False):
  from local_api import validate_url
  validate_url(url)
  self.url=url.rstrip('/');self.password=password;self.cwd=cwd;self.model=model;self.agent=agent
  self.skip_permissions=skip_permissions
  self.hard_seconds=hard_seconds;self.idle_seconds=idle_seconds;self.stop=stop or threading.Event();self.owner=owns_session;self.executable=executable;self.log_path=log_path;self.client=None;self.on_start=on_start;self.on_event=on_event
 def api(self,path,method='GET',body=None):
  request=urllib.request.Request(self.url+path,method=method,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Basic '+base64.b64encode(('opencode:'+self.password).encode()).decode(),'Content-Type':'application/json'})
  from local_api import open_local
  with open_local(request,timeout=5) as response:
   data=response.read(2000000);return json.loads(data) if data else None
 def create(self,sid,worker):
  if not re.fullmatch(r'ses_[a-zA-Z0-9]+',sid):raise ValueError('Invalid session ID')
  self.sid=sid;self.worker=worker
  response=self.api('/api/session','POST',{'id':sid,'title':'swarm-worker: '+worker,'location':{'directory':str(self.cwd)}})
  if response['data']['id']!=sid:raise RuntimeError('Created session mismatch')
 def confirm_unstarted(self,sid):
  # Only called after create failed, before execute/Popen. Never cancel a session.
  if self.client is not None:return False
  import urllib.error
  for _ in range(2):
   try:
    session=self.api('/api/session/'+sid)
    if session.get('data',{}).get('id')!=sid:return False
   except urllib.error.HTTPError as exc:
    if exc.code!=404:return False
    data=json.loads(exc.read(4096))
    if data.get('_tag')!='SessionNotFoundError' or data.get('sessionID')!=sid:return False
   active=self.api('/api/session/active')
   if not isinstance(active.get('data'),dict) or sid in active['data']:return False
  return True
 def owns_session(self,worker,sid):
  return self.owner is not None and self.owner(worker,sid) and worker==self.worker and sid==self.sid
 def interrupt(self,sid):return self.api('/api/session/'+sid+'/interrupt','POST')
 def is_idle(self,sid):
  session=self.api('/api/session/'+sid);active=self.api('/api/session/active')
  if not isinstance(active.get('data'),dict):raise ValueError('Invalid active-session response')
  return session['data']['id']==sid and sid not in active['data']
 def wait_client(self):
  if self.client is None:return True
  try:self.client.wait(timeout=2);return True
  except subprocess.TimeoutExpired:return False
 def terminate_client(self):
  if self.client and self.client.poll() is None:self.client.terminate()
 def execute(self,sid,prompt):
  cmd=[self.executable,'run','--server',self.url,'--session',sid,'--format','json']
  if self.skip_permissions:cmd+=['--auto']
  if self.model:cmd+=['--model',self.model]
  if self.agent:cmd+=['--agent',self.agent]
  cmd.append(prompt)
  env=dict(os.environ);env['OPENCODE_SERVER_PASSWORD']=self.password
  self.client=subprocess.Popen(cmd,cwd=self.cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
  if self.on_start:self.on_start(self.client.pid,sid)
  lines=queue.Queue()
  def pump():
   try:
    for line in self.client.stdout:lines.put(line)
   finally:lines.put(None)
  threading.Thread(target=pump,daemon=True).start();start=last=time.monotonic();checkpoint='';eof=False
  while True:
   now=time.monotonic()
   if self.stop.is_set():return {'status':'killed','checkpoint':checkpoint}
   if now-start>=self.hard_seconds:return {'status':'timeout','checkpoint':checkpoint}
   if now-last>=self.idle_seconds:return {'status':'idle-timeout','checkpoint':checkpoint}
   if eof:
    code=self.client.poll()
    if code is not None:return {'status':'completed' if code==0 and checkpoint else 'failed','checkpoint':checkpoint}
    time.sleep(.02);continue  # EOF is not process exit; keep enforcing deadlines.
   try:line=lines.get(timeout=min(.2,max(.01,self.hard_seconds-(now-start)),max(.01,self.idle_seconds-(now-last))))
   except queue.Empty:continue
   if line is None:eof=True;continue
   last=time.monotonic()
   if self.log_path:
    with open(self.log_path,'a') as out:out.write(line.replace(self.password,'[REDACTED]'))
   try:event=json.loads(line)
   except ValueError:continue
   if self.on_event:self.on_event(event)
   if event.get('type')=='text':checkpoint=(checkpoint+'\n'+str((event.get('part') or {}).get('text','')))[-20000:]
