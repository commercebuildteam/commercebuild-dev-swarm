"""Read GitHub organization issue Priority; evidence only, no queue writes."""
import datetime,json,pathlib,re,subprocess
RANK={'Promotion':0,'Urgent':1,'High':2,'Medium':3,'Low':4}
FIELDS='issueFieldValues(first:30){nodes{... on IssueFieldSingleSelectValue{name field{... on IssueFieldSingleSelect{name}}}}}'
LABELS='labels(first:10){nodes{name}}'
LABEL_PRIORITY=re.compile(r'^(?:p(?:riority)?[:_ -]*)?(urgent|high|medium|low)$',re.I)
def priority(node):
 values=[v['name'] for v in node.get('issueFieldValues',{}).get('nodes',[]) if v.get('field',{}).get('name')=='Priority']
 values+=[m.group(1).capitalize() for m in (LABEL_PRIORITY.match(n.get('name','')) for n in node.get('labels',{}).get('nodes',[])) if m]
 return min(values,key=lambda v:RANK.get(v,99)) if values else None
def query(q):
 p=subprocess.run(['gh','api','graphql','-f','query='+q],capture_output=True,text=True,timeout=25)
 if p.returncode:raise RuntimeError('GitHub priority query failed (authentication, schema, or transport)')
 d=json.loads(p.stdout)
 if d.get('errors'):raise RuntimeError('GitHub priority response contains errors')
 return d['data']
def collect(base):
 path=pathlib.Path(base)/'priority-snapshot.json';now=datetime.datetime.now(datetime.timezone.utc)
 if path.exists():
  old=json.loads(path.read_text())
  if (now-datetime.datetime.fromisoformat(old['captured_at'])).total_seconds()<60:return old
 result={'captured_at':now.isoformat(),'scope':'Open assigned commercebuild issues and open commercebuild- or donavon-commercebuild-authored PRs plus any PR requested for bhalecb or commercebuild review; maximum 300 each. Linked issues may be assigned to another owner. Evidence only.','items':[],'errors':[],'truncated':{}}
 issues={};prs=[]
 try:
  for kind,search,fragment in [
   ('issues','org:commercebuildteam is:issue is:open assignee:commercebuild','... on Issue{url title updatedAt repository{nameWithOwner} '+FIELDS+' '+LABELS+'}'),
   ('prs','org:commercebuildteam is:pr is:open author:commercebuild','... on PullRequest{url title body reviewDecision isDraft closingIssuesReferences(first:20){nodes{url '+FIELDS+' '+LABELS+'}}}'),
   ('prs','org:commercebuildteam is:pr is:open author:donavon-commercebuild','... on PullRequest{url title body reviewDecision isDraft closingIssuesReferences(first:20){nodes{url '+FIELDS+' '+LABELS+'}}}'),
   # A Donavon-requested review can be authored by anyone; PRs requested for review
   # carry linked-ticket priority regardless of author.
   ('prs','org:commercebuildteam is:pr is:open review-requested:bhalecb','... on PullRequest{url title body reviewDecision isDraft closingIssuesReferences(first:20){nodes{url '+FIELDS+' '+LABELS+'}}}'),
   ('prs','org:commercebuildteam is:pr is:open review-requested:commercebuild','... on PullRequest{url title body reviewDecision isDraft closingIssuesReferences(first:20){nodes{url '+FIELDS+' '+LABELS+'}}}')]:
   cursor=None
   for page in range(3):
    after=',after:'+json.dumps(cursor) if cursor else ''
    data=query('{search(query:'+json.dumps(search)+',type:ISSUE,first:100'+after+'){pageInfo{hasNextPage endCursor} nodes{'+fragment+'}}}')['search']
    for n in data['nodes']:
     if kind=='issues':issues[n['url']]=n
     elif n['url'] not in {x['url'] for x in prs}:prs.append(n)
    result['truncated'][kind]=data['pageInfo']['hasNextPage']
    if not data['pageInfo']['hasNextPage']:break
    cursor=data['pageInfo']['endCursor']
  for n in issues.values():result['items'].append({'url':n['url'],'title':n['title'],'priority':priority(n),'source':n['url'],'kind':'issue','updatedAt':n.get('updatedAt'),'repository':n.get('repository')})
  # Resolve explicit non-closing issue references even when assigned to a reviewer.
  missing=set()
  for n in prs:
   body=n.get('body') or '';repo=n['url'].split('/pull/')[0]
   urls=set(re.findall(r'https://github\.com/commercebuildteam/[\w.-]+/issues/\d+',body))
   urls.update(repo+'/issues/'+v for v in re.findall(r'(?im)\b(?:refs?|references)\s+#(\d+)\b',body))
   missing.update(urls-set(issues))
  missing=sorted(missing);result['truncated']['linked_issues']=len(missing)>300
  for offset in range(0,min(len(missing),300),40):
   batch=missing[offset:offset+40];parts=[]
   for i,url in enumerate(batch):
    repo,number=url.split('/')[4],int(url.rsplit('/',1)[1])
    parts.append('r'+str(i)+':repository(owner:"commercebuildteam",name:'+json.dumps(repo)+'){issue(number:'+str(number)+'){url '+FIELDS+'}}')
   data=query('{'+''.join(parts)+'}')
   for value in data.values():
    if value and value.get('issue'):issues[value['issue']['url']]=value['issue']
  for n in prs:
   links={x['url']:x for x in n['closingIssuesReferences']['nodes']}
   # Non-closing explicit references are common in this swarm. Only known issue
   # records can contribute a value; never infer priority from a PR title.
   body=n.get('body') or '';repo=n['url'].split('/pull/')[0]
   for number in re.findall(r'(?im)\b(?:refs?|references)\s+#(\d+)\b',body):
    url=repo+'/issues/'+number
    if url in issues:links[url]=issues[url]
   for url in re.findall(r'https://github\.com/commercebuildteam/[\w.-]+/issues/\d+',body):
    if url in issues:links[url]=issues[url]
   valued=[x for x in links.values() if priority(x) in RANK]
   winner=min(valued,key=lambda x:RANK[priority(x)]) if valued else None
   result['items'].append({'url':n['url'],'title':n['title'],'priority':priority(winner) if winner else None,'source':winner['url'] if winner else None,'kind':'pr','linked_issues':list(links),'review_decision':n.get('reviewDecision'),'draft':n['isDraft']})
 except (RuntimeError,subprocess.TimeoutExpired,ValueError) as exc:result['errors'].append(str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__)
 result['items'].sort(key=lambda x:(RANK.get(x['priority'],99),x['url']))
 result['unset_count']=sum(x['priority'] is None for x in result['items'])
 result['limitations']='No priority inferred from security severity or text. Project and priority-label values are honored. Non-closing references to issues outside assigned scan can remain unknown. Unknown never means Low. Ranking does not grant assignment or override review, runtime, repository locks, or stop markers.'
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(result,indent=2));tmp.replace(path)
 return result
if __name__=='__main__':
 d=collect(pathlib.Path(__file__).parent);print(json.dumps({k:v for k,v in d.items() if k!='items'}));print(json.dumps([x for x in d['items'] if x['priority']],indent=2))
