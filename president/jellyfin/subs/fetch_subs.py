#!/usr/bin/env python3
"""Download free (Subf2m) English subtitles through Jellyfin's own subtitle downloader."""
import argparse,json,os,sqlite3,subprocess,threading,time,urllib.error,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor
DB="/srv/apps/jellyfin/config/data/jellyfin.db"
BASE_DIR="/srv/apps/jellyfin/subs"
LOG=os.path.join(BASE_DIR,"fetch.log"); ATTEMPTED=os.path.join(BASE_DIR,"attempted.txt")
KEY=open("/tmp/buffy.key").read().strip()
def base_url():
    ips=subprocess.check_output(["docker","inspect","-f","{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}","jellyfin"]).decode().split()
    if not ips: raise SystemExit("jellyfin has no container IP")
    return "http://"+ips[0]+":8096"
BASE=base_url(); lock=threading.Lock(); counters={"ok":0,"nosub":0,"err":0}
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
        open(ATTEMPTED,"a").write(iid+"\n"); counters[status]+=1
def process(row):
    iid,name=row
    st,body=api("/Items/%s/RemoteSearch/Subtitles/eng"%iid)
    if st!=200: return mark(iid,"err")
    try: subs=json.loads(body)
    except Exception: subs=[]
    cands=[s for s in subs if "subf2m" in (s.get("Id") or "").lower()]
    if not cands: return mark(iid,"nosub")
    st,_=api("/Items/%s/RemoteSearch/Subtitles/%s"%(iid,urllib.parse.quote(cands[0]["Id"],safe="")),"POST")
    if st not in (200,204): return mark(iid,"err")
    api("/Items/%s/Refresh?metadataRefreshMode=ValidationOnly&imageRefreshMode=ValidationOnly&replaceAllMetadata=false&replaceAllImages=false"%iid,"POST",60)
    mark(iid,"ok")
READY="""and (
    (b.Type like '%TV.Episode' and exists (select 1 from BaseItemProviders p where p.ItemId=b.SeriesId and p.ProviderId='Imdb'))
 or (b.Type like '%Movies.Movie' and exists (select 1 from BaseItemProviders p where p.ItemId=b.Id and p.ProviderId='Imdb')))
and not exists (select 1 from MediaStreamInfos m where m.ItemId=b.Id and m.StreamType=3 and m.IsExternal=1)"""
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--series"); ap.add_argument("--limit",type=int,default=0); ap.add_argument("--workers",type=int,default=3)
    a=ap.parse_args()
    done=set(l.strip() for l in open(ATTEMPTED)) if os.path.exists(ATTEMPTED) else set()
    c=sqlite3.connect("file:"+DB+"?mode=ro",uri=True)
    if a.series:
        rows=c.execute("select b.Id,b.Name from BaseItems b where b.Type like '%TV.Episode' and b.SeriesId=? and not exists (select 1 from MediaStreamInfos m where m.ItemId=b.Id and m.StreamType=3 and m.IsExternal=1) order by b.Id",(a.series,)).fetchall()
    else:
        rows=c.execute("select b.Id,b.Name from BaseItems b where (b.Type like '%TV.Episode' or b.Type like '%Movies.Movie') "+READY+" order by b.Id").fetchall()
    rows=[r for r in rows if r[0] not in done]
    if a.limit: rows=rows[:a.limit]
    log("fetch start: %d ready items, workers=%d"%(len(rows),a.workers))
    n=0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for _ in ex.map(process,rows):
            n+=1
            if n%25==0: log("progress %d/%d %s"%(n,len(rows),counters))
    log("fetch done: %s"%counters)
if __name__=="__main__": main()
