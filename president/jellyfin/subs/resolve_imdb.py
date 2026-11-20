#!/usr/bin/env python3
"""Fill missing IMDb provider ids for movies+series using IMDb's keyless suggestion API."""
import json,re,sqlite3,threading,time,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor
DB="/srv/apps/jellyfin/config/data/jellyfin.db"
LOG="/srv/apps/jellyfin/subs/resolve.log"
lock=threading.Lock(); local=threading.local(); stats={}
def log(m):
    with lock:
        open(LOG,"a").write(time.strftime("%F %T ")+m+"\n"); print(m,flush=True)
def conn():
    c=getattr(local,"c",None)
    if c is None:
        c=sqlite3.connect(DB,timeout=90); local.c=c
    return c
def norm(s): return re.sub(r"[^a-z0-9]+","",(s or "").lower())
def get(u):
    for a in range(4):
        try:
            d=json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=25))
            if "d" in d: return d
        except Exception: pass
        time.sleep(3*(a+1))
    return None
def lookup(name,year):
    d=get("https://v3.sg.media-imdb.com/suggestion/x/"+urllib.parse.quote(name)+".json")
    if not d: return None,"http_fail"
    n=norm(name); best=None
    for x in d.get("d",[]):
        i=x.get("id","")
        if not i.startswith("tt"): continue
        y=x.get("y")
        if year and y and abs(y-year)>1: continue
        t=norm(x.get("l"))
        if t==n: return i,"exact"
        if best is None and (n in t or t in n): best=i
    return (best,"fuzzy") if best else (None,"none")
def main():
    c=sqlite3.connect("file:"+DB+"?mode=ro",uri=True)
    rows=c.execute("""select b.Id,b.Name,b.ProductionYear from BaseItems b
        where (b.Type like '%Movies.Movie' or b.Type like '%TV.Series')
        and not exists (select 1 from BaseItemProviders p where p.ItemId=b.Id and p.ProviderId='Imdb')""").fetchall()
    log("resolve start: %d items"%len(rows))
    def work(r):
        iid,name,year=r
        imdb,how=lookup(name,year)
        time.sleep(0.2)
        if imdb:
            cc=conn()
            with lock:
                cc.execute("delete from BaseItemProviders where ItemId=? and ProviderId='Imdb'",(iid,))
                cc.execute("insert into BaseItemProviders(ItemId,ProviderId,ProviderValue) values(?,?,?)",(iid,"Imdb",imdb))
                cc.commit()
        if how=="fuzzy" and imdb:
            log("fuzzy %s [%s] -> %s"%(name,year,imdb))
        with lock:
            stats[how]=stats.get(how,0)+1
    done=0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for _ in ex.map(work,rows):
            done+=1
            if done%250==0: log("progress %d/%d %s"%(done,len(rows),stats))
    log("resolve done: %s"%stats)
if __name__=="__main__": main()
