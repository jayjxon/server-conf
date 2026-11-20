#!/usr/bin/env python3
"""Fetch the posters jellyfin never downloaded for VOD movies and series."""
import argparse,json,os,subprocess,threading,time,urllib.error,urllib.request
from concurrent.futures import ThreadPoolExecutor
BASE_DIR="/srv/apps/jellyfin/images"
LOG=os.path.join(BASE_DIR,"posters.log"); DONE=os.path.join(BASE_DIR,"done.txt")
KEY=open("/tmp/buffy.key").read().strip()
lock=threading.Lock(); counters={"ok":0,"noimage":0,"err":0}
def base_url():
    ips=subprocess.check_output(["docker","inspect","-f","{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}","jellyfin"]).decode().split()
    if not ips: raise SystemExit("jellyfin has no container IP")
    return "http://"+ips[0]+":8096"
BASE=base_url()
def log(m):
    with lock:
        open(LOG,"a").write(time.strftime("%F %T ")+m+"\n"); print(m,flush=True)
def api(path,method="GET",timeout=120):
    h={"Authorization":'MediaBrowser Token="%s"'%KEY}
    if method=="POST": h["Content-Length"]="0"
    req=urllib.request.Request(BASE+path,method=method,headers=h)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: return r.status,r.read()
    except urllib.error.HTTPError as e: return e.code,e.read()[:300]
    except Exception as e: return 0,str(e)[:300].encode()
def mark(iid,status):
    with lock:
        open(DONE,"a").write(iid+"\n"); counters[status]+=1
def wanted():
    out=[]; start=0
    while True:
        st,body=api("/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=ImageTags,ProviderIds&Limit=1000&StartIndex=%d"%start)
        if st!=200: raise SystemExit("listing failed: %s %s"%(st,body))
        d=json.loads(body); items=d["Items"]
        for it in items:
            tags=it.get("ImageTags") or {}; prov=it.get("ProviderIds") or {}
            if not tags.get("Primary") and (prov.get("Tmdb") or prov.get("Imdb")):
                out.append((it["Id"],it.get("Type"),it.get("Name")))
        start+=len(items)
        if not items or start>=d["TotalRecordCount"]: break
    return out
def process(row):
    iid,_,_=row
    st,_=api("/Items/%s/Refresh?metadataRefreshMode=ValidationOnly&imageRefreshMode=FullRefresh&replaceAllImages=false&replaceAllMetadata=false"%iid,"POST")
    if st not in (200,204): return mark(iid,"err")
    st,body=api("/Items/%s?Fields=ImageTags"%iid)
    try: tags=json.loads(body).get("ImageTags") or {}
    except Exception: tags={}
    mark(iid,"ok" if tags.get("Primary") else "noimage")
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--limit",type=int,default=0); ap.add_argument("--workers",type=int,default=2)
    a=ap.parse_args()
    os.makedirs(BASE_DIR,exist_ok=True)
    done=set(l.strip() for l in open(DONE)) if os.path.exists(DONE) else set()
    rows=[r for r in wanted() if r[0] not in done]
    if a.limit: rows=rows[:a.limit]
    log("posters start: %d items need one, workers=%d"%(len(rows),a.workers))
    n=0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for _ in ex.map(process,rows):
            n+=1
            if n%25==0: log("progress %d/%d %s"%(n,len(rows),counters))
    log("posters done: %s"%counters)
if __name__=="__main__": main()
