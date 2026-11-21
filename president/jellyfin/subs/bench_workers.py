#!/usr/bin/env python3
"""Measure subtitle-fetch throughput per --workers value, one stage at a time.

Used on 2026-11-20 to size `fetch_subs.py`'s worker pool. It processes real items
(they are downloaded and appended to attempted.txt, so nothing is redone) but
writes no log of its own; it prints one line per run:

    workers=24 refresh=False n=36 wall=47.3s  45.6 items/min  per-item=29.8s
        search_med=29.79s dl_med=0.06s refresh_med=nan  {'ok': 26, 'nosub': 10}

`--verify` re-reads each downloaded item through /Users/{uid}/Items/{id} and
counts the ones that now advertise an external subtitle stream -- that is the
honest signal, not the 204 from the download call.

Run it while the fetch loop is stopped (see README, "Speeding it up"). Each arm
of ~36 items takes about a minute; the whole measurement is ~5 min.
"""
import argparse,json,os,sqlite3,subprocess,threading,time,urllib.error,urllib.parse,urllib.request,statistics
from concurrent.futures import ThreadPoolExecutor
DB="/srv/apps/jellyfin/config/data/jellyfin.db"
ATT="/srv/apps/jellyfin/subs/attempted.txt"
UID="1d2df1519e0943a2a2ff09ebfd1c1267"
KEY=open("/tmp/buffy.key").read().strip()
ips=subprocess.check_output(["docker","inspect","-f","{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}","jellyfin"]).decode().split()
BASE="http://"+ips[0]+":8096"
lock=threading.Lock()
def api(path,method="GET",timeout=120):
    h={"Authorization":'MediaBrowser Token="%s"'%KEY}
    if method=="POST": h["Content-Length"]="0"
    req=urllib.request.Request(BASE+path,method=method,headers=h)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: return r.status,r.read()
    except urllib.error.HTTPError as e: return e.code,e.read()[:300]
    except Exception as e: return 0,str(e)[:300].encode()
READY="""and (
    (b.Type like '%TV.Episode' and exists (select 1 from BaseItemProviders p where p.ItemId=b.SeriesId and p.ProviderId='Imdb'))
 or (b.Type like '%Movies.Movie' and exists (select 1 from BaseItemProviders p where p.ItemId=b.Id and p.ProviderId='Imdb')))
and not exists (select 1 from MediaStreamInfos m where m.ItemId=b.Id and m.StreamType=3 and m.IsExternal=1)"""
def has_sub(iid):
    st,b=api("/Users/%s/Items/%s"%(UID,iid))
    if st!=200: return None
    try: d=json.loads(b)
    except Exception: return None
    return any(s.get("Type")=="Subtitle" for m in d.get("MediaSources",[]) for s in m.get("MediaStreams",[]))
def process(iid,refresh):
    t={}; t0=time.time()
    st,body=api("/Items/%s/RemoteSearch/Subtitles/eng"%iid); t["search"]=time.time()-t0
    if st!=200: return iid,"err",t
    try: subs=json.loads(body)
    except Exception: subs=[]
    c=[s for s in subs if "subf2m" in (s.get("Id") or "").lower()]
    if not c: return iid,"nosub",t
    t0=time.time()
    st,_=api("/Items/%s/RemoteSearch/Subtitles/%s"%(iid,urllib.parse.quote(c[0]["Id"],safe="")),"POST"); t["dl"]=time.time()-t0
    if st not in (200,204): return iid,"err",t
    if refresh:
        t0=time.time()
        api("/Items/%s/Refresh?metadataRefreshMode=ValidationOnly&imageRefreshMode=ValidationOnly&replaceAllMetadata=false&replaceAllImages=false"%iid,"POST",60)
        t["refresh"]=time.time()-t0
    return iid,"ok",t
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--workers",type=int,required=True); ap.add_argument("--limit",type=int,default=18)
    ap.add_argument("--refresh",action="store_true"); ap.add_argument("--verify",action="store_true")
    a=ap.parse_args()
    done=set(l.strip() for l in open(ATT)) if os.path.exists(ATT) else set()
    c=sqlite3.connect("file:"+DB+"?mode=ro",uri=True)
    rows=[r[0] for r in c.execute("select b.Id from BaseItems b where (b.Type like '%TV.Episode' or b.Type like '%Movies.Movie') "+READY+" order by b.Id").fetchall()]
    rows=[r for r in rows if r not in done][:a.limit]
    t0=time.time(); res=[]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(lambda i: process(i,a.refresh), rows): res.append(r)
    wall=time.time()-t0
    with lock: open(ATT,"a").write("".join(i+"\n" for i,_,_ in res))
    cnt={}
    for _,s,_ in res:
        key=s+(("+" if a.refresh else "") if s=="ok" else ""); cnt[key]=cnt.get(key,0)+1
    def med(k):
        v=[t[k] for _,_,t in res if k in t]; return statistics.median(v) if v else float("nan")
    dur=[sum(t.values()) for _,_,t in res]
    print("workers=%d refresh=%s n=%d wall=%.1fs  %.1f items/min  per-item=%.1fs  "
          "search_med=%.2fs dl_med=%.2fs refresh_med=%.2fs  %s"
          %(a.workers,a.refresh,len(res),wall,len(res)/wall*60,statistics.median(dur),med("search"),med("dl"),med("refresh"),cnt))
    if a.verify:
        time.sleep(3)
        ok=[i for i,s,_ in res if s=="ok"]
        print("  verified external subtitle present: %d/%d"%(sum(1 for i in ok if has_sub(i)),len(ok)))
if __name__=="__main__": main()
