"""Read-only priority visibility, using the scheduler's actual cached evidence."""
import html
from datetime import datetime,timezone
import priority_schedule
def esc(x):return html.escape(str(x or ''),quote=True)
def status(item,entries,live,claims,owners,paused):
 url=item['url'];related={url,*item.get('linked_issues',[])}
 if item.get('kind')=='promotion-candidate':  # unclassified: visible, never queued
  text='Promotion discovery deferred - '+('; '.join(item.get('blocked') or []))
  if item.get('recheck_condition'):text+='<br><small>Recheck: '+esc(item['recheck_condition'])+'</small>'
  return text
 if item.get('next_owner'):  # promotion row: visible owner, hold evidence, recheck condition
  text='Promotion: next owner '+item['next_owner']
  if item.get('blocked'):text+=' - '+('; '.join(item['blocked']))
  if item.get('recheck_condition'):text+='<br><small>Recheck: '+esc(item['recheck_condition'])+'</small>'
  return text
 rows=[e for e in entries if e.get('issue') in related]
 # An issue can be serviced through a PR rather than its own queue row.
 for e in rows:
  if e.get('worker_id') in live:return 'Working now'
 if paused:return 'Dispatch paused'
 for e in rows:
  if e.get('state')=='pending':return 'Queued for an available slot'
 info=(owners.get('prs') or {}).get(url) or {}
 if info.get('owner') and info['owner']!='commercebuild':return 'Waiting on '+str(info['owner'])+': '+str(info.get('reason') or 'next action')
 for e in rows:
  state=e.get('state')
  if state in ('blocked','quarantined','awaiting-review','awaiting-merge','awaiting-assessment','incomplete','timeout','idle-timeout'):
   return state.replace('-',' ').capitalize()+(' - '+str(e['reason']) if e.get('reason') else '')
 repo='/'.join(url.split('/')[3:5])
 if repo in claims:return 'Waiting for repository claim'
 return 'Discovered; awaiting eligibility assessment'
def render(snapshot,entries,live,claims,owners,paused):
 items=snapshot.get('items',[]);known=[];unknown=[]
 # Reverse explicit links so issue rows reflect their active PR worker too.
 expanded=list(entries)
 for item in items:
  if item.get('kind')=='pr':
   for e in entries:
    if e.get('issue')==item['url']:
     expanded.extend(dict(e,issue=u) for u in item.get('linked_issues',[]))
 for item in sorted(items,key=lambda x:(priority_schedule.tier(x),x['url'])):
  level=item.get('priority') or 'Unset / unknown';url=item['url'];source=item.get('source');inherit=''
  if item.get('donavon_request'):inherit+='<br><small>Donavon review request: above ticket priorities</small>'
  if source and source!=url:inherit='<br><small>From <a href="'+esc(source)+'">'+esc(source.split('/')[-1])+'</a></small>'+inherit
  row='<tr><td><strong>'+esc(level)+'</strong>'+inherit+'</td><td><a href="'+esc(url)+'">'+esc('/'.join(url.split('/')[4:]))+'</a><br>'+esc(item.get('title'))+'</td><td>'+esc(status(item,expanded,live,claims,owners,paused))+'</td></tr>'
  (known if item.get('priority') else unknown).append(row)
 captured=snapshot.get('captured_at');fresh=False
 try:fresh=(datetime.now(timezone.utc)-datetime.fromisoformat(captured)).total_seconds()<120
 except (TypeError,ValueError):pass
 health='Current' if fresh and not snapshot.get('errors') else 'Stale or unavailable: ordering may use older values'
 if any(snapshot.get('truncated',{}).values()):health+='; coverage incomplete'
 header='<tr><th>Priority</th><th>Ticket / PR</th><th>Scheduling status</th></tr>'
 return '<section id="priority-work"><h2>Priority work</h2><p>'+esc(health)+' | Last priority fetch: '+esc(captured or 'not yet available')+'</p><p>Promotions first, then verified Donavon review requests, then Urgent &gt; High &gt; Medium &gt; Low &gt; unset. Priorities refresh about every minute. Active work and repository ownership are preserved. Priority is not a completion guarantee.</p><table>'+header+(''.join(known) or '<tr><td colspan="3">No explicit priorities recorded.</td></tr>')+'</table><details><summary>Unset / unknown priority ('+str(len(unknown))+')</summary><table>'+header+''.join(unknown)+'</table></details></section>'
