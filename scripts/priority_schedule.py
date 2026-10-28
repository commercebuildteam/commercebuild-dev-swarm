"""Minute refresh and stable priority ordering; never modifies running work."""
import json,threading,time
import promotion_source
import review_intake
from priority_source import collect,RANK
_lock=threading.Lock()
_last=0.0

def snapshot(state):
 try:return json.loads((state/'priority-snapshot.json').read_text())
 except (OSError,ValueError):return {}

def refresh(state,background=True):
 global _last
 if not _lock.acquire(False):return snapshot(state)
 if time.monotonic()-_last<60:
  _lock.release();return snapshot(state)
 _last=time.monotonic()
 def run():
  try:
   old=snapshot(state);new=collect(state)
   # Promotion discovery has its own 60-second loop in the launcher so a slow ticket
   # refresh never delays it; merged_snapshot() reads its snapshot for ordering.
   if new.get('errors') and old.get('items'):
    # Preserve last-known ordering on transient API errors; show freshness/error.
    new['items']=old['items'];new['last_success_at']=old.get('last_success_at',old.get('captured_at'))
    tmp=state/'priority-snapshot.retry.tmp';tmp.write_text(json.dumps(new));tmp.replace(state/'priority-snapshot.json')
  finally:_lock.release()
 if background:threading.Thread(target=run,daemon=True).start()
 else:run()
 return snapshot(state)

# Scheduling tiers: verified promotions first, then verified Donavon review
# requests, then the ordinary ticket priority. A request goes ahead of every
# ticket priority, including critical and security work. The total worker cap and
# one worker per repository still bound how many run at once: there is no separate
# review cap.
PROMOTION_TIER=0
REQUEST_TIER=1
TICKET_TIER=2

def tier(item):
 """Scheduling tier for one row. Unverified promotion stubs keep the bottom tier
 with unset work, so they never rank above verified rows."""
 if item.get('priority')=='Promotion':return PROMOTION_TIER
 if item.get('donavon_request'):return REQUEST_TIER
 return TICKET_TIER+RANK.get(item.get('priority'),5)

def rank_map(state):
 items=merge_items(state)
 return {x['url']:tier(x) for x in items}

def effective_priority(native, floor):
 """Apply the Donavon HIGH floor over a linked-ticket priority without downgrading
 Urgent: the effective value is the higher-ranked of the two (Urgent stays above
 HIGH; Medium or Low linked tickets never lower the request floor)."""
 if not floor:
  return native
 if not native:
  return floor
 if native not in RANK or floor not in RANK:
  return native
 return native if RANK[native] < RANK[floor] else floor

def merged_snapshot(state):
 """Full scheduling evidence: ticket snapshot plus Donavon-requested reviews and
 promotion rows, so the dashboard and ordering both see every tier."""
 data=dict(snapshot(state))
 rows={x['url']:x for x in data.get('items') or []}
 try:
  reviews={i['url']:i for i in review_intake.snapshot(state).get('items',[]) if i.get('donavon_request')}
 except (OSError,ValueError):pass
 else:
  for url,item in reviews.items():
   # The request supplies a HIGH floor over the linked-ticket priority: a Medium
   # or Low linked ticket never lowers it below HIGH, and Urgent stays above.
   # Ordering doesn't rely on that floor: tier() schedules every verified request
   # below promotions and above all ticket priorities.
   floor=item.get('priority')
   existing=rows.get(url)
   merged=dict(item)
   if existing:
    merged['priority']=effective_priority(existing.get('priority'),floor)
    merged['priority_reason']=('Donavon review request floor over linked-ticket priority'
        if merged['priority']==floor and floor and floor!=existing.get('priority')
        else existing.get('priority_reason') or item.get('priority_reason'))
   rows[url]=merged
 # Verified promotion evidence supersedes everything for its URL: promotions outrank
 # all. An unverified-lineage promotion or an unclassified discovery candidate only
 # fills a URL that has no row yet, so it never lowers a ticket or review priority.
 for i in promotion_source.items(state):
  if not i.get('unverified') or i['url'] not in rows:rows[i['url']]=i
 for i in promotion_source.candidates(state):
  rows.setdefault(i['url'],i)
 data['items']=list(rows.values())
 return data

def merge_items(state):
 return merged_snapshot(state)['items']

def order(rows,state):
 ranks=rank_map(state)
 default=tier({})  # no snapshot row: bottom tier, same as unset work
 # Python's stable sort preserves existing age/recovery fairness within a tier.
 # Promotions order above every Donavon review request, and a request orders above
 # every ticket priority.
 return sorted(rows,key=lambda r:min(ranks.get(r.get('url'),default),ranks.get(r.get('issue'),default)))
