"""Read-only review discovery. Durable local queue; never launches reviewers or writes GitHub."""
import datetime, fcntl, html, json, os, pathlib, re, subprocess, tempfile, threading, time
DONAVON='donavon-commercebuild'
TARGETS={'bhalecb','commercebuild'}
# A bare mention of bhalecb is never sufficient: the comment must contain an
# explicit review request addressed to him, from donavon-commercebuild himself.
EXPLICIT_REQUEST=re.compile(r'(?i)\b(?:please\s+|kindly\s+|can\s+you\s+|could\s+you\s+|request(?:ing|ed)?\s+(?:a\s+)?)review\b')
COMMENT_SCAN_BUDGET=25
FRAGMENT='''... on PullRequest { url title updatedAt headRefOid isDraft author{login}
 assignees(first:30){nodes{login}} reviewRequests(first:30){nodes{requestedReviewer{... on User{login}}}}
 timelineItems(last:100,itemTypes:[REVIEW_REQUESTED_EVENT]){pageInfo{hasPreviousPage} nodes{
 ... on ReviewRequestedEvent{createdAt actor{login} requestedReviewer{... on User{login}}}}}}'''
def explicit_comment_requests(comments):
 """Evidence entries for donavon-authored explicit review requests in comments."""
 found=[]
 for comment in comments or []:
  login=((comment.get('user') or {}).get('login') or '')
  body=comment.get('body') or ''
  if login!=DONAVON or 'bhalecb' not in body.lower() or not EXPLICIT_REQUEST.search(body):
   continue
  found.append({'kind':'comment','id':str(comment.get('id') or ''),'created_at':comment.get('created_at') or ''})
 return found
def comments_for(url):
 """Bounded recent comment fetch for one PR; explicit request evidence only."""
 match=re.fullmatch(r'https://github\.com/(commercebuildteam/[\w.-]+)/pull/(\d+)',url or '')
 if not match:return []
 p=subprocess.run(['gh','api',f'repos/{match.group(1)}/issues/{match.group(2)}/comments',
                   '-f','per_page=50','--jq','[.[] | {id,user:.user.login,body,created_at}]'],
                  capture_output=True,text=True,timeout=25)
 if p.returncode:raise RuntimeError(f'comment fetch failed for {url}: exit {p.returncode}')
 return json.loads(p.stdout or '[]')
def classify(n,comment_evidence=None):
 current={a['login'] for a in n['assignees']['nodes']}|{(r.get('requestedReviewer') or {}).get('login') for r in n['reviewRequests']['nodes']}
 events=[]
 for e in n['timelineItems']['nodes']:
  if e.get('requestedReviewer',{}).get('login') not in TARGETS:continue
  events.append((e.get('createdAt',''),(e.get('actor') or {}).get('login'),e['requestedReviewer']['login'],str(e.get('id') or e.get('createdAt') or '')))
 events.sort()
 latest={who:(at,actor,eid) for at,actor,who,eid in events}
 direct=latest.get('commercebuild')
 delegated=bool(direct and direct[1] in {'bhalecb',DONAVON})
 # Donavon's formal request: his review-request event naming bhalecb, or an
 # explicit review request from him in a PR comment. Authorship of the PR and a
 # bare mention are not sufficient. HIGH is a floor, never a downgrade.
 donavon_event=any(actor==DONAVON and who=='bhalecb' for at,actor,who,_ in events) and 'bhalecb' in current
 donavon_comment=bool(comment_evidence)
 donavon=donavon_event or donavon_comment
 evidence=[]
 if donavon_event:
  at,actor,eid=max((at,actor,eid) for at,actor,who,eid in events if actor==DONAVON and who=='bhalecb')
  evidence.append({'kind':'review_requested_event','id':eid,'actor':actor,'requested_reviewer':'bhalecb','created_at':at})
 evidence.extend(comment_evidence or [])
 routed='bhalecb' in current
 if not (current&TARGETS):return None
 state='queued-for-delegated-review' if delegated else ('awaiting-bhalecb-delegation' if routed else 'requires-bhalecb-routing')
 if n['isDraft']:state='draft-held'
 return {'url':n['url'],'title':n['title'],'head':n['headRefOid'],'priority':'High' if donavon else None,'priority_reason':'Donavon review request: minimum High' if donavon else 'No requester priority override','state':state,'delegated':delegated,'donavon_request':donavon,'request_evidence':evidence,'updated_at':n.get('updatedAt'),'timeline_truncated':n['timelineItems']['pageInfo']['hasPreviousPage']}
def query(q):
 p=subprocess.run(['gh','api','graphql','-f','query='+q],capture_output=True,text=True,timeout=25)
 if p.returncode:raise RuntimeError('GitHub review intake query failed')
 d=json.loads(p.stdout)
 if d.get('errors'):raise RuntimeError('GitHub review intake returned errors')
 return d['data']['search']
def atomic(p,v):
 fd,t=tempfile.mkstemp(dir=p.parent)
 with os.fdopen(fd,'w') as f:json.dump(v,f,indent=2);f.flush();os.fsync(f.fileno())
 os.replace(t,p)
def snapshot(state):
 try:return json.loads((pathlib.Path(state)/'review-intake.json').read_text())
 except (OSError,ValueError):return {}
MAX_CONCURRENT_REVIEWS=2
def can_dispatch(issue,donavon_urls,running_reviews):
 """Donavon-requested review intake is independent of capacity: every request stays
 queued and visible, but at most MAX_CONCURRENT_REVIEWS run concurrently within the
 total worker cap and one worker per repository."""
 return not (issue in donavon_urls and running_reviews>=MAX_CONCURRENT_REVIEWS)
def refresh(state):
 state.mkdir(parents=True,exist_ok=True);path=state/'review-intake.json'
 with (state/'review-intake.lock').open('a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return
  now=datetime.datetime.now(datetime.timezone.utc).isoformat()
  try:old=json.loads(path.read_text())
  except (OSError,ValueError):old={}
  result={'checked_at':now,'interval_seconds':60,'max_concurrent_reviews':2,'execution_enabled':False,'items':[],'errors':[],'truncated':{}}
  try:
   nodes={}
   for term in ['review-requested:bhalecb','review-requested:commercebuild','assignee:bhalecb','assignee:commercebuild']:
    search='org:commercebuildteam is:pr is:open '+term;cursor=None
    for page in range(3):
     after=',after:'+json.dumps(cursor) if cursor else ''
     data=query('{search(query:'+json.dumps(search)+',type:ISSUE,first:100'+after+'){pageInfo{hasNextPage endCursor} nodes{'+FRAGMENT+'}}}')
     nodes.update({n['url']:n for n in data['nodes']});result['truncated'][term]=data['pageInfo']['hasNextPage']
     if not data['pageInfo']['hasNextPage']:break
     cursor=data['pageInfo']['endCursor']
   # Inherit known native issue priority; Donavon is a floor, never a downgrade of Urgent.
   try:priorities={i['url']:i.get('priority') for i in json.loads((state/'priority-snapshot.json').read_text())['items']}
   except (OSError,ValueError,KeyError):priorities={}
   ranks={'Urgent':1,'High':2,'Medium':3,'Low':4}
   budget=COMMENT_SCAN_BUDGET
   for n in nodes.values():
    comment_evidence=[]
    if budget>0:
     budget-=1
     try:comment_evidence=explicit_comment_requests(comments_for(n['url']))
     except (RuntimeError,subprocess.TimeoutExpired,ValueError) as ce:
      result['errors'].append(str(ce) if isinstance(ce,RuntimeError) else type(ce).__name__)
    item=classify(n,comment_evidence)
    if item:
     native=priorities.get(item['url'])
     if ranks.get(native,9)<ranks.get(item['priority'],9):item['priority']=native;item['priority_reason']='Linked issue priority'
     result['items'].append(item)
   result['items'].sort(key=lambda i:(ranks.get(i['priority'],9),i['updated_at'],i['url']))
   result['last_success_at']=now
  except (RuntimeError,subprocess.TimeoutExpired,ValueError,KeyError) as e:
   result['errors']=[str(e) if isinstance(e,RuntimeError) else type(e).__name__];result['items']=old.get('items',[]);result['last_success_at']=old.get('last_success_at')
  atomic(path,result)
def start(state):
 def run():
  while True:
   at=time.monotonic()
   try:refresh(state)
   except Exception:pass # refresh errors are persisted where possible; freshness exposes failures.
   time.sleep(max(1,60-(time.monotonic()-at)))
 threading.Thread(target=run,name='read-only-review-intake',daemon=True).start()
def render(data):
 esc=lambda v:html.escape(str(v or ''))
 rows=''.join('<tr><td>'+esc(i.get('priority') or 'Unset')+'</td><td><a href="'+esc(i['url'])+'">'+esc(i['title'])+'</a></td><td>'+esc(i['state'])+'</td><td>'+esc(i['priority_reason'])+'</td></tr>' for i in data.get('items',[]))
 return '<section><h2>Review intake</h2><p>Checked '+esc(data.get('checked_at'))+' | Last successful '+esc(data.get('last_success_at'))+' | Every 60 seconds while awake. All requests retained independently of developer capacity. Review execution is disabled pending your choice; intended limit: 2.</p><p>'+esc(data.get('errors') or '')+'</p><table><tr><th>Priority</th><th>PR</th><th>Routing</th><th>Reason</th></tr>'+rows+'</table></section>'
