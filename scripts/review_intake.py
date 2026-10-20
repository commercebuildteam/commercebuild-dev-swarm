"""Read-only review discovery. Durable local queue; never launches reviewers or writes GitHub."""
import concurrent.futures, datetime, fcntl, html, json, os, pathlib, re, subprocess, tempfile, threading, time
DONAVON='donavon-commercebuild'
TARGETS={'bhalecb','commercebuild'}
# A bare mention of bhalecb is never sufficient: the comment must contain an
# explicit review request addressed to him, from donavon-commercebuild himself.
EXPLICIT_REQUEST=re.compile(r'(?i)\b(?:please\s+|kindly\s+|can\s+you\s+|could\s+you\s+|request(?:ing|ed)?\s+(?:a\s+)?)review\b')
DISCOVERY_SECONDS=40
# Linked issues beyond the closing references: full URLs and bare #N references
# in the PR body are also explicit links whose comments must be scanned.
BODY_REF_URL=re.compile(r'(?i)commercebuildteam/([\w.\-]+)/issues/(\d+)')
BODY_REF_NUM=re.compile(r'(?<![\w/])#(\d+)\b')
LABEL_PRIORITY=re.compile(r'^(?:p(?:riority)?[:_ -]*)?(urgent|high|medium|low)$',re.I)
FRAGMENT='''... on PullRequest { url title body updatedAt headRefOid isDraft author{login}
 assignees(first:30){nodes{login}} reviewRequests(first:30){nodes{requestedReviewer{... on User{login}}}}
 closingIssuesReferences(first:20){nodes{url}}
 timelineItems(last:100,itemTypes:[REVIEW_REQUESTED_EVENT]){pageInfo{hasPreviousPage} nodes{
 ... on ReviewRequestedEvent{createdAt actor{login} requestedReviewer{... on User{login}}}}}}'''
def explicit_comment_requests(comments):
 """Evidence entries for donavon-authored explicit review requests in comments."""
 found=[]
 for comment in comments or []:
  if not isinstance(comment,dict):continue
  login=comment.get('user') or ''
  if isinstance(login,dict):login=login.get('login') or ''  # raw API shape fallback
  body=comment.get('body') or ''
  if login!=DONAVON or 'bhalecb' not in body.lower() or not EXPLICIT_REQUEST.search(body):
   continue
  found.append({'kind':'comment','id':str(comment.get('id') or ''),'created_at':comment.get('created_at') or ''})
 return found
def call_timeout(deadline=None):
 remaining=deadline-time.monotonic() if deadline is not None else 25
 if remaining<=0:raise subprocess.TimeoutExpired('gh review scan',0)
 return min(25,remaining)
def comments_for(url,deadline=None):
 """Bounded recent comment fetch for one PR or issue; explicit request evidence only."""
 match=re.fullmatch(r'https://github\.com/(commercebuildteam/[\w.-]+)/(?:pull|issues)/(\d+)',url or '')
 if not match:return []
 p=subprocess.run(['gh','api','--method','GET',f'repos/{match.group(1)}/issues/{match.group(2)}/comments',
                   '-f','per_page=50','--jq','[.[] | {id,user:.user.login,body,created_at}]'],
                  capture_output=True,text=True,timeout=call_timeout(deadline))
 if p.returncode:raise RuntimeError(f'comment fetch failed for {url}: exit {p.returncode}')
 data=json.loads(p.stdout or '[]')
 if not isinstance(data,list) or not all(isinstance(c,dict) for c in data):
  raise RuntimeError(f'comment fetch for {url} returned an unexpected body')
 return data
def linked_issue_urls(node):
    """Verified linked issues for one PR: closing references plus explicit links
    parsed from the PR body (full URLs and bare #N references)."""
    urls=[(r.get('url') or '') for r in (node.get('closingIssuesReferences') or {}).get('nodes') or []]
    repo_base=(node.get('url') or '').rsplit('/pull/',1)[0]
    body=node.get('body') or ''
    for m in BODY_REF_URL.finditer(body):
        urls.append(f'https://github.com/commercebuildteam/{m.group(1)}/issues/{m.group(2)}')
    for m in BODY_REF_NUM.finditer(body):
        if repo_base:urls.append(f'{repo_base}/issues/{m.group(1)}')
    return [u for u in dict.fromkeys(urls) if re.fullmatch(r'https://github\.com/commercebuildteam/[\w.-]+/issues/\d+',u or '')]
def issue_priority(url,deadline=None):
    """Current priority of one linked issue from its own labels, independent of
    the coordinator's priority snapshot."""
    match=re.fullmatch(r'https://github\.com/(commercebuildteam/[\w.-]+)/issues/(\d+)',url or '')
    if not match:return None
    p=subprocess.run(['gh','api','--method','GET',f'repos/{match.group(1)}/issues/{match.group(2)}',
                      '--jq','[.labels[].name]'],capture_output=True,text=True,timeout=call_timeout(deadline))
    if p.returncode:raise RuntimeError(f'linked issue priority fetch failed for {url}: exit {p.returncode}')
    for name in json.loads(p.stdout or '[]'):
        m=LABEL_PRIORITY.match(name or '')
        if m:return m.group(1).capitalize()
    return None
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
 if not ((current&TARGETS) or comment_evidence):return None
 state='queued-for-delegated-review' if delegated else ('awaiting-bhalecb-delegation' if routed else 'requires-bhalecb-routing')
 if n['isDraft']:state='draft-held'
 return {'url':n['url'],'title':n['title'],'head':n['headRefOid'],'priority':'High' if donavon else None,'priority_reason':'Donavon review request: minimum High' if donavon else 'No requester priority override','state':state,'delegated':delegated,'donavon_request':donavon,'request_evidence':evidence,'updated_at':n.get('updatedAt'),'timeline_truncated':n['timelineItems']['pageInfo']['hasPreviousPage']}
def query(q,deadline=None):
 p=subprocess.run(['gh','api','graphql','-f','query='+q],capture_output=True,text=True,timeout=call_timeout(deadline))
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
def inspect_candidate(n,deadline,priorities,old_items):
 """Inspect one PR without mutating the snapshot; callers retain completed work."""
 errors=[];comment_evidence=[];linked_issue_priorities=[];failed=False
 try:comment_evidence=explicit_comment_requests(comments_for(n['url'],deadline))
 except (RuntimeError,subprocess.TimeoutExpired,ValueError) as e:
  errors.append(str(e) if isinstance(e,RuntimeError) else type(e).__name__);failed=True
 for issue_url in linked_issue_urls(n):
  if time.monotonic()>=deadline:
   errors.append('linked issue scan deadline');failed=True;break
  try:comment_evidence.extend(explicit_comment_requests(comments_for(issue_url,deadline)))
  except (RuntimeError,subprocess.TimeoutExpired,ValueError) as e:
   errors.append(str(e) if isinstance(e,RuntimeError) else type(e).__name__);failed=True
  try:priority=issue_priority(issue_url,deadline)
  except (RuntimeError,subprocess.TimeoutExpired,ValueError) as e:
   errors.append(str(e) if isinstance(e,RuntimeError) else type(e).__name__);priority=None;failed=True
  if priority:linked_issue_priorities.append(priority)
 item=classify(n,comment_evidence)
 ranks={'Urgent':1,'High':2,'Medium':3,'Low':4}
 if item:
  native=[priorities.get(item['url'])]+linked_issue_priorities
  best=min([p for p in native if p],key=lambda p:ranks.get(p,9),default=None)
  if ranks.get(best,9)<ranks.get(item['priority'],9):
   item['priority']=best;item['priority_reason']='Linked issue priority'
  if failed and n['url'] in old_items:
   previous=old_items[n['url']]
   if ranks.get(previous.get('priority'),9)<ranks.get(item.get('priority'),9):
    item['priority']=previous['priority'];item['priority_reason']='Last known priority; linked issue lookup failed'
 elif failed:item=old_items.get(n['url'])
 return item,errors

def search_candidates(term,deadline):
 nodes=[];truncated=False;error=None
 search='org:commercebuildteam is:pr is:open '+term;cursor=None
 for page in range(3):
  after=',after:'+json.dumps(cursor) if cursor else ''
  try:
   data=query('{search(query:'+json.dumps(search)+',type:ISSUE,first:100'+after+'){pageInfo{hasNextPage endCursor} nodes{'+FRAGMENT+'}}}',deadline)
   nodes.extend(data['nodes']);truncated=data['pageInfo']['hasNextPage']
   if not truncated:break
   cursor=data['pageInfo']['endCursor']
  except (RuntimeError,subprocess.TimeoutExpired,ValueError,KeyError) as e:
   error=str(e);break
 return nodes,truncated,error
def refresh(state):
 state.mkdir(parents=True,exist_ok=True);path=state/'review-intake.json'
 previous=snapshot(state)
 lock_deadline=time.monotonic()+DISCOVERY_SECONDS
 with (state/'review-intake.lock').open('a') as lock:
  waited=False
  while True:
   try:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    break
   except BlockingIOError:
    waited=True
    if time.monotonic()>=lock_deadline:
     return {'errors':['review scan lock busy beyond deadline']}
    time.sleep(min(0.1,lock_deadline-time.monotonic()))
  if waited:
   # Another process completed this scan while we waited. Use its fresh result
   # for queue insertion instead of running a second scan or reading old rows.
   result=snapshot(state)
   if result.get('checked_at')==previous.get('checked_at') or not result:
    return {'errors':['concurrent review scan produced no new snapshot']}
   return result
  now=datetime.datetime.now(datetime.timezone.utc).isoformat()
  try:old=json.loads(path.read_text())
  except (OSError,ValueError):old={}
  result={'checked_at':now,'interval_seconds':60,'max_concurrent_reviews':2,'execution_enabled':True,'items':[],'errors':[],'truncated':{}}
  deadline=time.monotonic()+DISCOVERY_SECONDS
  try:priorities={i['url']:i.get('priority') for i in json.loads((state/'priority-snapshot.json').read_text())['items']}
  except (OSError,ValueError,KeyError):priorities={}
  old_items={i.get('url'):i for i in old.get('items',[]) if isinstance(i,dict) and i.get('url')}
  previous_deferred=set(old.get('deferred_urls') or [])
  ranks={'Urgent':1,'High':2,'Medium':3,'Low':4}
  items={};seen=set();pending={};deferred=[]
  searches=['commenter:'+DONAVON,'review-requested:bhalecb','review-requested:commercebuild',
            'assignee:bhalecb','assignee:commercebuild','author:'+DONAVON]
  search_pool=concurrent.futures.ThreadPoolExecutor(max_workers=len(searches))
  detail_pool=concurrent.futures.ThreadPoolExecutor(max_workers=16)
  def collect_search(future):
   term=search_jobs[future]
   try:
    response=future.result()
    nodes,truncated=response[:2]
    error=response[2] if len(response)>2 else None
   except (RuntimeError,subprocess.TimeoutExpired,ValueError,KeyError) as e:
    result['errors'].append(f'{term}: {e}');return
   result['truncated'][term]=truncated
   if error:result['errors'].append(f'{term}: {error}')
   if truncated and not error:result['errors'].append(f'{term}: search page cap reached')
   for n in sorted(nodes,key=lambda row:row.get('url') not in previous_deferred):
    url=n.get('url')
    if not url or url in seen:continue
    seen.add(url)
    pending[detail_pool.submit(inspect_candidate,n,deadline,priorities,old_items)]=n
  try:
   search_jobs={search_pool.submit(search_candidates,term,deadline):term for term in searches}
   for future in concurrent.futures.as_completed(search_jobs,timeout=max(0,deadline-time.monotonic())):
    collect_search(future)
    search_jobs.pop(future)
  except concurrent.futures.TimeoutError:
   result['errors'].append('review search deadline reached')
  finally:
   for future in list(search_jobs):
    if future.done():collect_search(future)
   search_pool.shutdown(wait=False,cancel_futures=True)
  # Gather every finished inspection, including those completed while another
  # search timed out. Remaining candidates are explicit deferred work.
  try:
   for future in concurrent.futures.as_completed(pending,timeout=max(0,deadline-time.monotonic())):
    n=pending.pop(future)
    try:item,errors=future.result()
    except (RuntimeError,subprocess.TimeoutExpired,ValueError,KeyError) as e:
     item=old_items.get(n['url']);errors=[str(e)]
    result['errors'].extend(errors)
    if item:items[n['url']]=item
  except concurrent.futures.TimeoutError:
   pass
  finally:
   for future,n in list(pending.items()):
    if future.done():
     pending.pop(future)
     try:item,errors=future.result()
     except (RuntimeError,subprocess.TimeoutExpired,ValueError,KeyError) as e:
      item=old_items.get(n['url']);errors=[str(e)]
     result['errors'].extend(errors)
     if item:items[n['url']]=item
    else:deferred.append(n['url'])
   detail_pool.shutdown(wait=False,cancel_futures=True)
  if deferred:result['errors'].append(f'{len(deferred)} candidate inspections deferred by deadline')
  result['deferred_urls']=deferred
  if result['errors']:
   items.update({url:item for url,item in old_items.items() if url not in items})
   result['partial']=True;result['last_success_at']=old.get('last_success_at')
  else:result['last_success_at']=now
  result['items']=sorted(items.values(),key=lambda i:(ranks.get(i.get('priority'),9),i.get('updated_at') or '',i['url']))
  atomic(path,result)
  return result
_POLLING=False
def start(state):
 # Idempotent: the polling thread starts once, whichever entry point runs first
 # (dashboard serve, or the dispatch loop's own startup).
 global _POLLING
 if _POLLING:return
 _POLLING=True
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
 return '<section><h2>Review intake</h2><p>Checked '+esc(data.get('checked_at'))+' | Last successful '+esc(data.get('last_success_at'))+' | Every 60 seconds while awake. All requests retained independently of developer capacity. Queued Donavon-requested reviews run as reviewer jobs: at most 2 concurrently within the developer worker cap, one per repository.</p><p>'+esc(data.get('errors') or '')+'</p><table><tr><th>Priority</th><th>PR</th><th>Routing</th><th>Reason</th></tr>'+rows+'</table></section>'
