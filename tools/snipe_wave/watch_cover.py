import json,glob,time,urllib.request,os
os.chdir('/root/TWB/worlds/nl116')
seen=set()
def groups():
    inc=[json.load(open(f)) for f in glob.glob('cache/incomings/*.json')]
    g={}
    for tid in {d['target_id'] for d in inc}:
        gid=0
        for d in sorted([d for d in inc if d['target_id']==tid],key=lambda d:d['arrival_ms']):
            if (d['game_label'] or '').startswith('Edel'): g[(tid,d['arrival_ms'])]=gid
            else: gid+=1
    return g
while True:
    try:
        S=json.load(open('cache/snipes.json'))['commands']
        hits=[s for s in S if s['status']=='done' and s.get('hit_ms') and 'could NOT be recalled' not in (s.get('result') or '') and s['id'] not in seen]
        if hits:
            g=groups()
            for h in hits:
                seen.add(h['id']); tid=h['target_village_id']; hm=int(h['hit_ms']); gid=g.get((tid,hm))
                for s in S:
                    if s['status']!='armed' or s['target_village_id']!=tid or not s.get('hit_ms'): continue
                    sm=int(s['hit_ms'])
                    same=[k for (t2,k),g2 in g.items() if t2==tid and g2==gid]
                    first_in_group=gid is not None and hm==min(same)
                    # a hit before the train's first noble only covers that noble:
                    # a front-loaded first noble can kill it. Behind it, it covers the train.
                    if sm==hm or (gid is not None and not first_in_group and g.get((tid,sm))==gid):
                        r=json.load(urllib.request.urlopen(f"http://127.0.0.1:5000/app/snipe/cancel?world=nl116&id={s['id']}",timeout=20))
                        print(time.strftime('%T'),'hit by',h['village_name'][:3],'on',h['target_name'][:3],'.%03d'%(hm%1000),'-> cancelled',s['village_name'][:3],'.%03d'%(sm%1000),r.get('state'),flush=True)
    except Exception as e:
        print(time.strftime('%T'),'error',e,flush=True)
    time.sleep(10)
