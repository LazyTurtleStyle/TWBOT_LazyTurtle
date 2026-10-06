import json,glob,time,math,collections,sys
DRY='--arm' not in sys.argv
tm=json.load(open('cache/troops_moving.json'))['by_village']
cfg=json.load(open('config.json')); vt=json.load(open('cache/world/villages_txt.json'))
def nm(v):
    try: return json.load(open(f'cache/villages/{v}.json'))['name'][:3]
    except Exception: return v
inc=[json.load(open(f)) for f in glob.glob('cache/incomings/*.json')]
S=json.load(open('cache/snipes.json'))['commands']
live=[s for s in S if s['status'] in ('armed','running')]
kept={s['hit_ms'] for s in S if s['status']=='done'}
targets={d['target_id'] for d in inc if (d['game_label'] or '').startswith('Edel')}
# cover groups: nobles on a target with no non-noble command between them
groups={}
for tid in targets:
    cmds=sorted([d for d in inc if d['target_id']==tid],key=lambda d:d['arrival_ms'])
    g=[];gid=0
    for d in cmds:
        if (d['game_label'] or '').startswith('Edel'): groups[d['arrival_ms']]=(tid,gid)
        else: gid+=1
gfirst={}
for (h,(tid_,gid_)) in [(h,v) for h,v in groups.items()]:
    gfirst[(tid_,gid_)]=min(gfirst.get((tid_,gid_),h),h)
# a train counts as covered only by a hit behind its first noble
covered_groups={groups[h] for h in kept if h in groups and (h!=gfirst[groups[h]] or sum(1 for g in groups.values() if g==groups[h])==1)}
nobles=[]
for d in sorted(inc,key=lambda d:d['arrival_ms']):
    l=d['game_label'] or ''
    if not l.startswith('Edel') or 'C-SNIPE' in l or 'SNIPE OK' in l: continue
    h=d['arrival_ms']
    if groups[h] in covered_groups: continue
    if h in kept: continue
    tid=d['target_id']
    prev=max([x['arrival_ms'] for x in inc if x['target_id']==tid and x['arrival_ms']<h] or [h-10**6])
    lo,hi=(prev+1 if h-prev<1500 else h-100),h-1
    aim=(lo+hi)//2; keep=max(1,min(50,(hi-lo)//2))
    first=not any(groups.get(x['arrival_ms'])==groups[h] and x['arrival_ms']<h for x in inc if x['target_id']==tid)
    if keep<=3: continue
    order=sorted(k for k,v in groups.items() if v==groups[h]).index(h)
    rank={1:0,2:1,0:2}.get(order,3)
    nobles.append(dict(rank=rank,tagged='SNIPE THIS' in l,tid=tid,cid=d['command_id'],hit=h,aim=aim,keep=keep,first=first,n=sum(1 for s in live if s.get('hit_ms')==h)))
kept_t={s['target_village_id'] for s in S if s['status']=='done'}
now=time.time()
sends=[s['send_est_ts'] for s in live]
vsends=collections.defaultdict(list)
grp=lambda u:'hc' if u.get('heavy') else 'inf'
for s in live: vsends[(s['village_id'],grp(s.get('units') or {}))].append(s['send_est_ts'])
away={s['village_id'] for s in S if s['status']=='done' and ((s.get('units_sent') or s['units']).get('spear') or (s.get('units_sent') or s['units']).get('sword'))}
hc_away={s['village_id'] for s in S if s['status']=='done' and (s.get('units_sent') or s['units']).get('heavy')}
pool=[v for v in cfg['villages'] if v not in targets and vt.get(v)]
def own(v): return (tm.get(v) or {}).get('own') or {}
def options(v):
    o=own(v); out=[]
    if v not in away:
        sw=min(o.get('sword',0),1000); sp2=min(o.get('spear',0),2000-sw)
        out.append(('sword',22,'inf',{'spear':sp2,'sword':sw}))
        out.append(('spear',18,'inf',{'spear':min(o.get('spear',0),2000)}))
        if o.get('catapult',0)>=1:
            out.append(('catapult',30,'inf',{'spear':min(o.get('spear',0),1000),'sword':min(o.get('sword',0),1000),'catapult':1}))
    if o.get('heavy',0)>=250 and v not in hc_away:
        out.append(('heavy',11,'hc',{'heavy':min(o.get('heavy',0),1000)}))
    return out
CAP=10
out=[]
for rnd in range(CAP):
    for nb in sorted(nobles,key=lambda n:(not n['tagged'],n['tid'] in kept_t,n['rank'],-n['keep'],n['hit'])):
        if nb['n']>=CAP: continue
        if rnd>=CAP: continue
        t=vt[nb['tid']]; best=None
        for v in pool:
            d=math.hypot(vt[v]['x']-t['x'],vt[v]['y']-t['y'])
            for pace,mpf,g,u in options(v):
                foot=sum(n for k,n in u.items() if k!='catapult')
                if g=='inf' and foot<800: continue
                s=nb['aim']/1000-d*mpf*60
                if s-now<300: continue
                if any(abs(s-x)<20 for x in sends) or any(abs(s-x)<120 for x in vsends[(v,g)]): continue
                if best is None: best=(v,pace,s,u,g)
                break
            if best: break
        if not best: continue
        v,pace,s,u,g=best
        sends.append(s); vsends[(v,g)].append(s); nb['n']+=1
        out.append(dict(nb,v=v,pace=pace,s=s,units=u))
summary=collections.defaultdict(list)
for o in out: summary[(o['tid'],o['hit'])].append('%s%s@%s'%(nm(o['v']),{'spear':'(sp)','heavy':'(HC)','catapult':'(cat)'}.get(o['pace'],''),time.strftime('%H:%M',time.localtime(o['s']))))
for nb in sorted(nobles,key=lambda n:n['hit']):
    print(nm(nb['tid']),'.%03d'%(nb['hit']%1000),'aim .%03d ±%d'%(nb['aim']%1000,nb['keep']),'first' if nb['first'] else '     ','total',nb['n'],'new',summary[(nb['tid'],nb['hit'])])
print(len(out),'new snipes')
if not DRY:
    import urllib.request
    g=collections.defaultdict(list)
    for o in out: g[(o['tid'],o['cid'],o['hit'],o['aim'],o['keep'])].append({'village_id':o['v'],'units':o['units'],'pace_unit':o['pace']})
    for (tid,cid,hit,aim,keep),opts in g.items():
        body={'incoming_id':cid,'target_village_id':tid,'tribe_target_id':None,'land_ms':aim,'hit_ms':hit,'options':opts,'shortfall':'scale','min_pct':80,'boost':1.0,'max_delta_ms':keep}
        r=json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:5000/app/snipe/arm?world=nl116',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=30))
        print(nm(tid),'.%03d'%(hit%1000),'armed',r.get('armed'),r.get('errors') or '')
