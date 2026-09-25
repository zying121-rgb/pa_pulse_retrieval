import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import pulse_service as p

NOW=1790341200
SOURCE=dict(sourceId='test-feed',publisherId='test-publisher',sourceName='Test Publisher',category='technology_ai',
            url='https://example.org/feed',refreshSeconds=21600,enabled=True,attribution='Test Publisher',rightsUrl='https://example.org/rights')


def rss(text='A factual public excerpt', guid='stable-1', title='Public test headline', link='https://example.org/story', date='Fri, 25 Sep 2026 00:00:00 GMT'):
    return f'<rss><channel><item><guid>{guid}</guid><title>{title}</title><link>{link}</link><description>{text}</description><pubDate>{date}</pubDate></item></channel></rss>'.encode()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder=Path('evidence/test-state')
        self.folder.mkdir(parents=True,exist_ok=True)
        self.counter=id(self)
        self.path=self.folder/f'{self._testMethodName}.json'
        if self.path.exists(): self.path.unlink()
        backup=self.path.with_suffix('.json.bak')
        if backup.exists(): backup.unlink()
        self.store=p.Store(self.path)

    def fetch_ok(self,source,state): return 200,{'ETag':'"one"'},rss()

    def test_canonical_contract(self):
        item=p.parse_feed(rss(),SOURCE,NOW)[0]
        self.assertEqual(set(item),set('itemKey title summary summaryOrigin category sourceName sourceUrl publishedAt retrievedAt sourceId sourceItemId sourceText contentScope contentHash schemaVersion'.split()))
        self.assertEqual(item['summaryOrigin'],'source_excerpt')

    def test_identity_stable_across_time_and_content(self):
        a=p.parse_feed(rss(),SOURCE,NOW)[0]
        b=p.parse_feed(rss('Changed factual excerpt'),SOURCE,NOW+100)[0]
        self.assertEqual(a['itemKey'],b['itemKey'])
        self.assertNotEqual(a['contentHash'],b['contentHash'])
        self.assertEqual(a['contentHash'],p.parse_feed(rss(),SOURCE,NOW+100)[0]['contentHash'])

    def test_url_fallback_not_title(self):
        a=p.parse_feed(rss(guid='',link='https://EXAMPLE.org/story?utm_source=x&amp;b=2&amp;a=1#part'),SOURCE,NOW)[0]
        b=p.parse_feed(rss(guid='',title='New headline',link='https://example.org/story?a=1&amp;b=2'),SOURCE,NOW)[0]
        self.assertEqual(a['itemKey'],b['itemKey'])
        c=p.parse_feed(rss(guid='',link='https://example.org/different'),SOURCE,NOW)[0]
        self.assertNotEqual(b['itemKey'],c['itemKey'])

    def test_duplicate_guid_and_cross_publisher_provenance(self):
        raw=rss().replace(b'</channel>',rss().split(b'<channel>')[1].split(b'</channel>')[0]+b'</channel>')
        self.assertEqual(len(p.parse_feed(raw,SOURCE,NOW)),1)
        other={**SOURCE,'publisherId':'other'}
        self.assertNotEqual(p.parse_feed(rss(),SOURCE,NOW)[0]['itemKey'],p.parse_feed(rss(),other,NOW)[0]['itemKey'])

    def test_atom_updated_not_publication(self):
        atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:stable</id><title>A</title><link href="https://example.org/a"/><updated>2026-09-24T00:00:00Z</updated><summary>Excerpt</summary></entry></feed>'
        item=p.parse_feed(atom,SOURCE,NOW)[0]
        self.assertIsNone(item['publishedAt'])
        self.assertEqual(item['sourceItemId'],'tag:stable')

    def test_missing_invalid_dates_not_fabricated(self):
        for value in ('','not a date','2026-09-24T00:00:00'):
            self.assertIsNone(p.parse_feed(rss(date=value),SOURCE,NOW)[0]['publishedAt'])

    def test_title_only_fallback_and_no_fulltext(self):
        raw=rss(text='').replace(b'</item>',b'<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">Do not copy full text</content:encoded></item>')
        item=p.parse_feed(raw,SOURCE,NOW)[0]
        self.assertEqual(item['contentScope'],'title_only')
        self.assertIsNone(item['summary'])
        self.assertEqual(item['sourceText'],'')

    def test_html_script_images_removed(self):
        item=p.parse_feed(rss(text='<![CDATA[<p>Public text</p><script>bad()</script><img src="https://tracker.invalid">]]>'),SOURCE,NOW)[0]
        self.assertEqual(item['sourceText'],'Public text')

    def test_external_entities_and_large_xml_rejected(self):
        for raw in (b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel>&x;</channel></rss>',b'x'*(p.MAX_FEED_BYTES+1)):
            with self.assertRaises(Exception): p.parse_feed(raw,SOURCE,NOW)

    def test_bad_item_does_not_remove_good_item(self):
        raw=rss().replace(b'</channel>',b'<item><title>Bad item</title><link>javascript:bad()</link></item></channel>')
        self.assertEqual(len(p.parse_feed(raw,SOURCE,NOW)),1)

    def test_failure_isolated_and_last_good_retained(self):
        other={**SOURCE,'sourceId':'failed','publisherId':'failed'}
        p.refresh(self.store,[SOURCE,other],NOW,self.fetch_ok)
        def fail_one(source,state):
            if source['sourceId']=='failed': raise ValueError('malformed')
            return 200,{},rss('Updated')
        p.refresh(self.store,[SOURCE,other],NOW+22000,fail_one)
        value=p.batch(self.store,[SOURCE,other],NOW+22000)
        self.assertEqual(len(value['items']),2)
        self.assertEqual(value['status'],'degraded')
        self.assertEqual(value['sources'][1]['status'],'stale')

    def test_all_failed_refresh_does_not_fake_generated_time(self):
        p.refresh(self.store,[SOURCE],NOW,self.fetch_ok)
        def fail(*args): raise TimeoutError()
        p.refresh(self.store,[SOURCE],NOW+22000,fail)
        self.assertEqual(p.batch(self.store,[SOURCE],NOW+22000)['generatedAt'],p.iso(NOW))

    def test_backoff_and_retry_after(self):
        def fail(*args): raise HTTPError(SOURCE['url'],429,'limited',{'Retry-After':'7200'},None)
        p.refresh(self.store,[SOURCE],NOW,fail)
        self.assertGreaterEqual(self.store.all()['test-feed']['nextAttempt'],NOW+7200)
        with patch('pulse_service.fetch') as mock:
            p.refresh(self.store,[SOURCE],NOW+60,mock)
            mock.assert_not_called()

    def test_304_preserves_items_and_validates_cache(self):
        p.refresh(self.store,[SOURCE],NOW,self.fetch_ok)
        old=self.store.all()['test-feed']['items']
        p.refresh(self.store,[SOURCE],NOW+22000,lambda *a:(304,{},b''))
        self.assertEqual(self.store.all()['test-feed']['items'],old)
        self.assertEqual(p.batch(self.store,[SOURCE],NOW+22000)['status'],'fresh')

    def test_restart_restores_cache_and_expiry_is_bounded(self):
        p.refresh(self.store,[SOURCE],NOW,self.fetch_ok)
        restored=p.Store(self.path)
        self.assertEqual(len(p.batch(restored,[SOURCE],NOW)['items']),1)
        self.assertEqual(p.batch(restored,[SOURCE],NOW+p.CACHE_LIFETIME+1)['items'],[])

    def test_batch_bounds_and_same_publisher_url_dedupe(self):
        sources=[]
        for n in range(20):
            source={**SOURCE,'sourceId':str(n),'publisherId':str(n)}
            sources.append(source)
            items=[p.parse_feed(rss(guid=str(i),link=f'https://example.org/{i}'),source,NOW)[0] for i in range(12)]
            self.store.put(str(n),{'items':items,'lastSuccess':NOW})
        value=p.batch(self.store,sources,NOW)
        self.assertEqual(len(value['items']),120)
        self.assertLessEqual(len(p.compact(value)),p.MAX_PAYLOAD_BYTES)

    def call_app(self,**overrides):
        env={'REQUEST_METHOD':'GET','PATH_INFO':'/v1/pulse','QUERY_STRING':'','CONTENT_LENGTH':'0',**overrides}
        result={}
        def start(status,headers): result.update(status=status,headers=dict(headers))
        result['body']=b''.join(p.make_app(self.store,[SOURCE])(env,start))
        return result

    def test_endpoint_rejects_user_inputs_without_echo(self):
        for env in ({'QUERY_STRING':'profile=PRIVATE_SENTINEL'},{'HTTP_AUTHORIZATION':'PRIVATE_SENTINEL'},
                    {'HTTP_COOKIE':'PRIVATE_SENTINEL'},{'CONTENT_LENGTH':'1'},{'REQUEST_METHOD':'POST'}):
            result=self.call_app(**env)
            self.assertNotEqual(result['status'],'200 OK')
            self.assertNotIn(b'PRIVATE_SENTINEL',result['body'])

    def test_outbound_request_has_only_fixed_public_headers(self):
        class Response:
            status=200; headers={}
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self,*a): return rss()
        class Opener:
            def open(self,request,timeout):
                self.request=request
                return Response()
        opener=Opener()
        with patch('pulse_service.urllib.request.build_opener',return_value=opener):
            p.fetch(SOURCE,{'etag':'"tag"','profile':'PRIVATE_SENTINEL'})
        self.assertEqual(opener.request.full_url,SOURCE['url'])
        self.assertIsNone(opener.request.data)
        self.assertEqual(set(k.lower() for k in opener.request.headers),{'user-agent','accept','if-none-match'})
        self.assertNotIn('PRIVATE_SENTINEL',str(opener.request.headers))

    def test_etag_and_cors_and_empty_response(self):
        self.assertEqual(self.call_app()['status'],'503 Service Unavailable')
        p.refresh(self.store,[SOURCE],p.time.time(),self.fetch_ok)
        first=self.call_app()
        self.assertEqual(first['status'],'200 OK')
        self.assertEqual(first['headers']['Access-Control-Allow-Origin'],'*')
        self.assertNotIn('Access-Control-Allow-Credentials',first['headers'])
        self.assertEqual(self.call_app(HTTP_IF_NONE_MATCH=first['headers']['ETag'])['status'],'304 Not Modified')

    def test_all_selected_live_snapshots_parse(self):
        sources=json.loads(Path('sources.json').read_text(encoding='utf-8'))
        self.assertEqual(len(sources),10)
        if any(not Path('evidence',s['sourceId']+'.xml').exists() for s in sources):
            self.skipTest('Run probe_sources.py first to enable live-snapshot integration test')
        for source in sources:
            raw=Path('evidence',source['sourceId']+'.xml').read_bytes()
            items=p.parse_feed(raw,source,NOW)
            self.assertGreater(len(items),0,source['sourceId'])
            self.assertLessEqual(len(items),12)

    def test_overlapping_feeds_dedupe_same_publisher_url(self):
        second={**SOURCE,'sourceId':'second-feed'}
        a=p.parse_feed(rss(guid='one'),SOURCE,NOW)
        b=p.parse_feed(rss(guid='different-id-same-link'),second,NOW)
        self.store.put(SOURCE['sourceId'],{'items':a,'lastSuccess':NOW})
        self.store.put(second['sourceId'],{'items':b,'lastSuccess':NOW})
        self.assertEqual(len(p.batch(self.store,[SOURCE,second],NOW)['items']),1)

    def test_interrupted_atomic_replace_preserves_cache(self):
        p.refresh(self.store,[SOURCE],NOW,self.fetch_ok)
        original=self.store.all()
        real_replace=p.os.replace
        def fail_primary(src,dest):
            if Path(dest)==self.path: raise OSError('simulated interrupted write')
            return real_replace(src,dest)
        with patch('pulse_service.os.replace',side_effect=fail_primary):
            with self.assertRaises(OSError): self.store.put('new',{'items':[]})
        self.assertEqual(p.Store(self.path).all(),original)
        self.assertEqual(list(self.folder.glob(self.path.name+'.*.tmp')),[])

    def test_corrupt_primary_recovers_previous_good_snapshot(self):
        p.refresh(self.store,[SOURCE],NOW,self.fetch_ok)
        self.path.write_text('{broken',encoding='utf-8')
        recovered=p.Store(self.path)
        self.assertEqual(len(recovered.all()[SOURCE['sourceId']]['items']),1)
        recovered.generated(NOW+1)
        self.assertEqual(json.loads(self.path.read_text())['version'],1)

    def test_concurrent_reader_writer_sees_valid_snapshots(self):
        errors=[]
        def write():
            try:
                for i in range(15): self.store.put('test',{'iteration':i})
            except Exception as ex: errors.append(ex)
        thread=p.threading.Thread(target=write)
        thread.start()
        for _ in range(30): self.assertIsInstance(self.store.all(),dict)
        thread.join()
        self.assertFalse(errors)


if __name__=='__main__': unittest.main()
