"""Read public feeds only; no app or account inputs. Run locally, not in service."""
import concurrent.futures
import json
import urllib.request
from defusedxml import ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

def probe(c):
    sid,name,category,url,interval=c
    result=dict(sourceId=sid,sourceName=name,category=category,url=url,refreshSeconds=interval,
                checkedAt=datetime.now(timezone.utc).isoformat())
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'PA-Pulse-Feed-Verification/1.0','Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml'})
        with urllib.request.urlopen(req,timeout=25) as r:
            raw=r.read(2_000_001)
            result.update(httpStatus=r.status,finalUrl=r.url,bytes=len(raw),etagPresent=bool(r.headers.get('ETag')),lastModifiedPresent=bool(r.headers.get('Last-Modified')))
        if len(raw)>2_000_000: raise ValueError('feed too large')
        root=ET.fromstring(raw,forbid_dtd=True,forbid_entities=True,forbid_external=True)
        entries=root.findall('.//item') or root.findall('{http://www.w3.org/2005/Atom}entry')
        counts={key:0 for key in ['id','title','excerpt','published','url']}
        for e in entries:
            tags={n.tag.split('}')[-1]: n for n in e}
            for key,names in {'id':['guid','id'],'title':['title'],'excerpt':['description','summary'],'published':['pubDate','published','date'],'url':['link']}.items():
                if any(n in tags and (''.join(tags[n].itertext()).strip() or tags[n].get('href')) for n in names): counts[key]+=1
        result.update(feedType='Atom' if root.tag.endswith('feed') else 'RSS',items=len(entries),fieldCounts=counts,parseSuccess=bool(entries))
        Path('evidence').mkdir(exist_ok=True)
        Path('evidence',sid+'.xml').write_bytes(raw)
    except Exception as ex: result.update(parseSuccess=False,error=type(ex).__name__+': '+str(ex)[:240])
    return result

if __name__=='__main__':
    CANDIDATES=[(s['sourceId'],s['sourceName'],s['category'],s['url'],s['refreshSeconds']) for s in json.loads(Path('sources.json').read_text(encoding='utf-8')) if s.get('enabled')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(probe,CANDIDATES))
    Path('evidence').mkdir(exist_ok=True)
    Path('evidence/source-verification.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    for r in results: print(r['sourceId'],r.get('httpStatus'),r.get('items'),r.get('fieldCounts',r.get('error')))
