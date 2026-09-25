"""Public-only scheduled RSS/Atom cache. No profile inputs and no AI inference."""
import argparse
import hashlib
import html
import json
import os
import random
import re
import uuid
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from defusedxml import ElementTree as ET

MAX_FEED_BYTES = 2_000_000
MAX_PAYLOAD_BYTES = 512_000
MAX_ITEMS = 120
MAX_SOURCE_ITEMS = 12
CACHE_LIFETIME = 7 * 86400
ATOM = '{http://www.w3.org/2005/Atom}'
DC = '{http://purl.org/dc/elements/1.1/}'


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1
        if tag in ('p', 'br', 'li', 'div'): self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip: self.skip -= 1
        if tag in ('p', 'li', 'div'): self.parts.append(' ')

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def plain(value, limit):
    parser = PlainText()
    parser.feed(value[:100_000])
    text = re.sub(r'\s+', ' ', ''.join(parser.parts)).strip()
    if len(text) > limit: text = text[:limit].rsplit(' ', 1)[0].rstrip() + '…'
    return text


def clean_url(value, base=''):
    try:
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, html.unescape(value.strip())))
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        # Remove only known tracking fields; preserve meaningful query identity and path case.
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
        port = parsed.port
        host = parsed.hostname.lower()
        if port and not (parsed.scheme == 'https' and port == 443 or parsed.scheme == 'http' and port == 80):
            host += ':' + str(port)
        return urllib.parse.urlunsplit((parsed.scheme.lower(), host, parsed.path or '/', urllib.parse.urlencode(sorted(query)), ''))
    except ValueError:
        return ''


def date_value(value):
    if not value: return None
    try:
        try: dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError: dt = parsedate_to_datetime(value.strip())
        # No invented timezone for an ambiguous timestamp.
        if dt.tzinfo is None: return None
        return iso(dt.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def element_text(element):
    if element is None: return ''
    if len(element): return ET.tostring(element, encoding='unicode')
    return element.text or ''


def parse_feed(raw, source, now):
    if len(raw) > MAX_FEED_BYTES: raise ValueError('feed_too_large')
    root = ET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    atom = root.tag == ATOM + 'feed'
    if atom: entries = root.findall(ATOM + 'entry')
    elif root.tag == 'rss': entries = root.findall('./channel/item')
    else: raise ValueError('unsupported_feed')
    if not entries: raise ValueError('empty_feed')
    records, seen = [], set()
    for entry in entries[:500]:
        try:
            prefix = ATOM if atom else ''
            title = plain(element_text(entry.find(prefix + 'title')), 300)
            if atom:
                links = [n.get('href', '') for n in entry.findall(ATOM+'link')
                         if n.get('rel', 'alternate') == 'alternate' and n.get('type', 'text/html') in ('text/html', 'application/xhtml+xml')]
                link = links[0] if links else ''
            else: link = element_text(entry.find('link'))
            url = clean_url(link, source['url'])
            if not title or not url: continue
            sid = element_text(entry.find(ATOM+'id' if atom else 'guid')).strip()
            if len(sid) > 2048: continue
            sid = sid or url
            excerpt_raw = element_text(entry.find(ATOM+'summary' if atom else 'description'))
            # Do not pull full content:encoded / Atom content or article pages.
            if re.search(r'all rights reserved|©|copyright\s+(?!crown)', excerpt_raw, re.I): continue
            excerpt = plain(excerpt_raw, 800)
            published_raw = element_text(entry.find(ATOM+'published' if atom else 'pubDate'))
            if not published_raw and not atom: published_raw = element_text(entry.find(DC+'date'))
            published = date_value(published_raw)
            # A feed's updated timestamp is deliberately NOT treated as publication time.
            identity = source['publisherId'] + '\n' + sid
            key = source['publisherId'] + ':' + digest(identity)
            if key in seen: continue
            seen.add(key)
            record = dict(itemKey=key, title=title, summary=excerpt or None,
                          summaryOrigin='source_excerpt' if excerpt else 'none',
                          category=source['category'], sourceName=source['sourceName'], sourceUrl=url,
                          publishedAt=published, retrievedAt=iso(now), sourceId=source['publisherId'],
                          sourceItemId=sid, sourceText=excerpt, contentScope='excerpt' if excerpt else 'title_only',
                          schemaVersion=1)
            record['contentHash'] = digest(compact({k:v for k,v in record.items() if k!='retrievedAt'}).decode())
            records.append(record)
        except (ValueError, TypeError, AttributeError):
            continue
    if not records: raise ValueError('no_usable_items')
    records.sort(key=lambda i: (i['publishedAt'] or '', i['itemKey']), reverse=True)
    return records[:MAX_SOURCE_ITEMS]


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != 'https' or new.hostname != old.hostname:
            raise ValueError('unapproved_feed_redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(source, state):
    # This function has no access to client requests or app state.
    headers = {'User-Agent':'PA-Public-Pulse/1.0', 'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml'}
    for saved, header in [('etag','If-None-Match'), ('modified','If-Modified-Since')]:
        value = state.get(saved)
        if value and len(value)<1000 and '\r' not in value and '\n' not in value: headers[header]=value
    request = urllib.request.Request(source['url'], headers=headers)
    opener = urllib.request.build_opener(HTTPSOnlyRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(MAX_FEED_BYTES+1)
            if len(raw)>MAX_FEED_BYTES: raise ValueError('feed_too_large')
            return response.status, dict(response.headers), raw
    except urllib.error.HTTPError as error:
        if error.code == 304: return 304, dict(error.headers), b''
        raise


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup = self.path.with_suffix(self.path.suffix + '.bak')
        self.lock = threading.RLock()
        with self.lock:
            if not self.path.exists() and not self.backup.exists():
                self._atomic_write(self.path, compact({'version':1,'feeds':{},'generatedAt':None}))
            self._read()

    def _read(self):
        for candidate in (self.path,self.backup):
            try:
                value=json.loads(candidate.read_bytes())
                if value.get('version')!=1 or not isinstance(value.get('feeds'),dict):
                    raise ValueError('invalid_public_cache')
                return value
            except (OSError,ValueError,AttributeError):
                continue
        raise ValueError('no_valid_public_cache_snapshot')

    def _atomic_write(self,path,raw):
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('xb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary,path)
            # Persist the rename itself on Linux (Render). Windows lacks directory fsync.
            if os.name=='posix':
                descriptor=os.open(path.parent,os.O_RDONLY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
        finally:
            if temporary.exists(): temporary.unlink()

    def _save(self,previous,value):
        self._atomic_write(self.backup,compact(previous))
        self._atomic_write(self.path,compact(value))

    def all(self):
        with self.lock: return self._read()['feeds']

    def put(self, sid, state):
        with self.lock:
            previous=self._read()
            value=json.loads(json.dumps(previous))
            value['feeds'][sid]=state
            self._save(previous,value)

    def generated(self, now=None):
        with self.lock:
            previous=self._read()
            if now is None: return previous['generatedAt']
            value=json.loads(json.dumps(previous))
            value['generatedAt']=iso(now)
            self._save(previous,value)
            return value['generatedAt']


def refresh(store, sources, now=None, fetcher=fetch):
    now = time.time() if now is None else now
    states = store.all()
    changed = False
    for source in sources:
        if not source.get('enabled'): continue
        state = states.get(source['sourceId'], {})
        if state.get('nextAttempt',0)>now: continue
        try:
            status, headers, raw = fetcher(source,state)
            if status == 304:
                if not state.get('items'): raise ValueError('304_without_cache')
            elif status == 200:
                state['items'] = parse_feed(raw,source,now)
                lower = {k.lower():v for k,v in headers.items()}
                state['etag'],state['modified'] = lower.get('etag'),lower.get('last-modified')
            else: raise ValueError('unexpected_status')
            state.update(lastSuccess=now,lastAttempt=now,nextAttempt=now+source['refreshSeconds'],failures=0,error=None)
            changed=True
        except Exception as error:
            failures = state.get('failures',0)+1
            delay = min(86400,900 * 2**min(failures-1,7)) + random.randint(0,60)
            if isinstance(error, urllib.error.HTTPError):
                retry = error.headers.get('Retry-After','')
                try: retry_seconds = int(retry)
                except ValueError:
                    parsed = date_value(retry)
                    retry_seconds = int(datetime.fromisoformat(parsed.replace('Z','+00:00')).timestamp()-now) if parsed else 0
                delay=max(delay,min(7*86400,max(0,retry_seconds)))
            # Persist only an error category, never response text or request headers.
            state.update(lastAttempt=now,nextAttempt=now+delay,failures=failures,error=type(error).__name__)
        store.put(source['sourceId'],state)
    if changed: store.generated(now)


def batch(store, sources, now=None):
    now=time.time() if now is None else now
    states=store.all()
    pools=[]
    health=[]
    for source in sources:
        if not source.get('enabled'): continue
        state=states.get(source['sourceId'],{})
        success=state.get('lastSuccess')
        age=now-success if success is not None else float('inf')
        stale=age>source['refreshSeconds']+900 or bool(state.get('error'))
        usable=age<=CACHE_LIFETIME
        health.append(dict(feedId=source['sourceId'],sourceId=source['publisherId'],sourceName=source['sourceName'],
                           lastSuccessAt=iso(success) if success is not None else None,
                           status='unavailable' if not usable else ('stale' if stale else 'fresh'),
                           attribution=source['attribution'],rightsUrl=source['rightsUrl']))
        pools.append(list(state.get('items',[])) if usable else [])
    items,keys,urls=[],set(),set()
    # Equal per-feed opportunity, not personalization. Client retains the only relevance ranking.
    for offset in range(MAX_SOURCE_ITEMS):
        for pool in pools:
            if offset>=len(pool): continue
            item=pool[offset]
            urlkey=(item['sourceId'],item['sourceUrl'])
            if item['itemKey'] in keys or urlkey in urls: continue
            keys.add(item['itemKey']); urls.add(urlkey); items.append(item)
            if len(items)>=MAX_ITEMS: break
        if len(items)>=MAX_ITEMS: break
    result=dict(schemaVersion=1,generatedAt=store.generated(),cacheMaxAgeSeconds=CACHE_LIFETIME,
                status='empty' if not items else ('degraded' if any(s['status']!='fresh' for s in health) else 'fresh'),
                sources=health,items=items)
    while len(compact(result))>MAX_PAYLOAD_BYTES and items: items.pop()
    return result


def make_app(store,sources):
    def app(env,start_response):
        method=env.get('REQUEST_METHOD','GET')
        headers=[('Content-Type','application/json; charset=utf-8'),('Access-Control-Allow-Origin','*'),
                 ('Access-Control-Allow-Methods','GET, HEAD, OPTIONS'),('X-Content-Type-Options','nosniff'),
                 ('Referrer-Policy','no-referrer')]
        # No query/body/credentials are accepted, read, persisted or forwarded.
        if env.get('QUERY_STRING') or env.get('HTTP_AUTHORIZATION') or env.get('HTTP_COOKIE') or env.get('CONTENT_LENGTH','0') not in ('','0'):
            status='400 Bad Request'; payload={'error':'public_endpoint_accepts_no_user_inputs'}
        elif method=='OPTIONS':
            start_response('204 No Content',headers); return [b'']
        elif method not in ('GET','HEAD'):
            status='405 Method Not Allowed'; payload={'error':'read_only'}
        elif env.get('PATH_INFO')=='/healthz':
            payload=batch(store,sources); status='200 OK' if payload['items'] else '503 Service Unavailable'
            payload={k:payload[k] for k in ('status','generatedAt')}
        elif env.get('PATH_INFO')=='/v1/pulse':
            payload=batch(store,sources); status='200 OK' if payload['items'] else '503 Service Unavailable'
        else: status='404 Not Found'; payload={'error':'not_found'}
        body=compact(payload)
        etag='"'+hashlib.sha256(body).hexdigest()+'"'
        headers += [('Cache-Control','public, max-age=300' if status=='200 OK' else 'no-store'),('ETag',etag)]
        if status=='200 OK' and env.get('HTTP_IF_NONE_MATCH')==etag:
            start_response('304 Not Modified',headers); return [b'']
        headers.append(('Content-Length',str(len(body))))
        start_response(status,headers)
        return [b'' if method=='HEAD' else body]
    return app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--export')
    args=parser.parse_args()
    sources=json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
    store=Store(Path(os.environ.get('PULSE_DATA_DIR','data'))/'public-cache.json')
    if args.once:
        refresh(store,sources)
        value=batch(store,sources)
        if args.export: Path(args.export).write_bytes(compact(value))
        print(json.dumps({'items':len(value['items']),'sources':[(s['feedId'],s['status']) for s in value['sources']]}))
        return
    def scheduler():
        while True:
            try: refresh(store,sources)
            except Exception: print('Public cache refresh failed; retaining stored data.',flush=True)
            time.sleep(60)
    threading.Thread(target=scheduler,daemon=True).start()
    from waitress import serve
    serve(make_app(store,sources),host=os.environ.get('HOST','127.0.0.1'),port=int(os.environ.get('PORT','8080')),
          threads=4,max_request_body_size=1024,max_request_header_size=8192,channel_timeout=30)


if __name__=='__main__': main()
