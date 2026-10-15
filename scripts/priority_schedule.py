"""Minute refresh and stable priority ordering; never modifies running work."""
import json,threading,time
import promotion_source
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
   # Promotions ride the same 60-second cadence and merge into the shared snapshot
   # so ordering sees them as the top tier without a second lock or file race.
   promotion_source.collect(state)
   if new.get('errors') and old.get('items'):
    # Preserve last-known ordering on transient API errors; show freshness/error.
    new['items']=old['items'];new['last_success_at']=old.get('last_success_at',old.get('captured_at'))
    tmp=state/'priority-snapshot.retry.tmp';tmp.write_text(json.dumps(new));tmp.replace(state/'priority-snapshot.json')
  finally:_lock.release()
 if background:threading.Thread(target=run,daemon=True).start()
 else:run()
 return snapshot(state)

def rank_map(state):
 items=merge_items(state)
 return {x['url']:RANK.get(x.get('priority'),5) for x in items}

def merge_items(state):
 # Promotion evidence is merged into ordering; a URL already carrying a ticket
 # priority is superseded by its promotion classification (promotions outrank all).
 base={x['url']:x for x in snapshot(state).get('items',[])}
 for promo in promotion_source.items(state):
  base[promo['url']]=promo
 return list(base.values())

def order(rows,state):
 ranks=rank_map(state)
 # Python's stable sort preserves existing age/recovery fairness within a tier.
 # Promotions (rank 0) order above every ticket priority and the oldest-work lanes.
 return sorted(rows,key=lambda r:min(ranks.get(r.get('url'),5),ranks.get(r.get('issue'),5)))
