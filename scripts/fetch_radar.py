#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""風向雷達資料抓取器（每天跑一次）
來源：Google 台灣熱搜 RSS ＋ IG 盯梢名單 ＋ YouTube 盯梢頻道 RSS
產出：data/radar.json（App 讀這份）、data/history.json（歷史快照，算「第幾天／爆量」用）
純標準庫，不用裝任何套件。單一來源掛掉不影響其他來源。
"""
import json, os, re, ssl, sys, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')
TPE = timezone(timedelta(hours=8))
TODAY = datetime.now(TPE).strftime('%Y-%m-%d')
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15'

def fetch(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError as e:
        # 本機 Python 沒裝憑證時的備援（抓的都是公開資料）
        if 'CERTIFICATE_VERIFY_FAILED' not in str(e): raise
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read()

def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except Exception:
        return default

history = load_json(os.path.join(DATA, 'history.json'), {'days': {}})
watch = load_json(os.path.join(DATA, 'watchlist.json'), {'ig': [], 'yt': []})
today_snap = {'keywords': {}, 'posts': {}, 'ytvids': {}}
errors = []

# ── 1. Google 台灣熱搜 ──────────────────────────────
def get_keywords():
    ns = {'ht': 'https://trends.google.com/trending/rss'}
    root = ET.fromstring(fetch('https://trends.google.com/trending/rss?geo=TW'))
    out = []
    for item in root.iter('item'):
        kw = (item.findtext('title') or '').strip()
        if not kw: continue
        traffic = (item.findtext('ht:approx_traffic', default='', namespaces=ns) or '').strip()
        news = item.find('ht:news_item', ns)
        n = None
        if news is not None:
            n = {'title': (news.findtext('ht:news_item_title', default='', namespaces=ns) or '').strip(),
                 'url': (news.findtext('ht:news_item_url', default='', namespaces=ns) or '').strip(),
                 'source': (news.findtext('ht:news_item_source', default='', namespaces=ns) or '').strip(),
                 'img': (news.findtext('ht:news_item_picture', default='', namespaces=ns) or '').strip()}
        pic = (item.findtext('ht:picture', default='', namespaces=ns) or '').strip()
        # 第一次出現是哪天（往回翻歷史）
        first = TODAY
        for d in sorted(history['days'], reverse=True):
            if kw in history['days'][d].get('keywords', {}): first = d
        out.append({'kw': kw, 'traffic': traffic, 'firstSeen': first,
                    'isNew': first == TODAY, 'news': n, 'img': pic})
        today_snap['keywords'][kw] = traffic
    return out

# ── 2. IG 盯梢名單 ──────────────────────────────────
def get_ig():
    out = []
    for username in watch.get('ig', []):
        try:
            raw = fetch('https://i.instagram.com/api/v1/users/web_profile_info/?username=' + username,
                        headers={'x-ig-app-id': '936619743392459'})
            u = json.loads(raw)['data']['user']
            posts = []
            views_list = []
            for e in u.get('edge_owner_to_timeline_media', {}).get('edges', []):
                n = e['node']
                cap = ''
                try: cap = n['edge_media_to_caption']['edges'][0]['node']['text']
                except Exception: pass
                views = n.get('video_view_count')
                likes = (n.get('edge_liked_by') or {}).get('count')
                p = {'code': n.get('shortcode'), 'isVideo': bool(n.get('is_video')),
                     'views': views, 'likes': likes,
                     'ts': n.get('taken_at_timestamp'),
                     'caption': cap[:60]}
                posts.append(p)
                if p['isVideo'] and views: views_list.append(views)
                key = 'ig:' + (p['code'] or '')
                today_snap['posts'][key] = views if views is not None else likes
            # 爆量判斷：該帳號影片觀看中位數的 3 倍以上、且 7 天內發布
            med = sorted(views_list)[len(views_list)//2] if views_list else 0
            now = time.time()
            for p in posts:
                metric = p['views'] if p['isVideo'] else p['likes']
                prev = None
                for d in sorted(history['days'], reverse=True):
                    if d == TODAY: continue
                    prev = history['days'][d].get('posts', {}).get('ig:' + (p['code'] or ''))
                    if prev is not None: break
                p['prev'] = prev
                p['growth'] = round((metric - prev) / prev * 100) if (prev and metric) else None
                fresh = p['ts'] and (now - p['ts'] < 7 * 86400)
                p['boom'] = bool(p['isVideo'] and p['views'] and med and fresh and p['views'] >= 3 * med)
                p['rising'] = bool(p['growth'] and p['growth'] >= 80 and fresh)
            out.append({'username': username, 'name': u.get('full_name') or username,
                        'followers': u['edge_followed_by']['count'], 'posts': posts, 'ok': True})
        except Exception as ex:
            errors.append('IG @%s: %s' % (username, ex))
            out.append({'username': username, 'ok': False})
        time.sleep(3)
    return out

# ── 3. YouTube 盯梢頻道 ─────────────────────────────
def get_yt():
    ns = {'a': 'http://www.w3.org/2005/Atom', 'm': 'http://search.yahoo.com/mrss/',
          'yt': 'http://www.youtube.com/xml/schemas/2015'}
    out = []
    for ch in watch.get('yt', []):
        try:
            root = ET.fromstring(fetch('https://www.youtube.com/feeds/videos.xml?channel_id=' + ch['id']))
            vids = []
            views_list = []
            for e in root.findall('a:entry', ns)[:10]:
                vid = e.findtext('yt:videoId', default='', namespaces=ns)
                st = e.find('.//m:statistics', ns)
                views = int(st.get('views')) if st is not None else None
                pub = e.findtext('a:published', default='', namespaces=ns)
                v = {'id': vid, 'title': (e.findtext('a:title', default='', namespaces=ns) or '')[:60],
                     'views': views, 'published': pub}
                vids.append(v)
                if views: views_list.append(views)
                today_snap['ytvids']['yt:' + vid] = views
            med = sorted(views_list)[len(views_list)//2] if views_list else 0
            for v in vids:
                prev = None
                for d in sorted(history['days'], reverse=True):
                    if d == TODAY: continue
                    prev = history['days'][d].get('ytvids', {}).get('yt:' + v['id'])
                    if prev is not None: break
                v['prev'] = prev
                v['growth'] = round((v['views'] - prev) / prev * 100) if (prev and v['views']) else None
                try:
                    fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(v['published'])).days < 7
                except Exception:
                    fresh = False
                v['boom'] = bool(v['views'] and med and fresh and v['views'] >= 3 * med)
                v['fresh'] = fresh
            out.append({'id': ch['id'], 'name': ch.get('name') or ch['id'], 'videos': vids, 'ok': True})
        except Exception as ex:
            errors.append('YT %s: %s' % (ch.get('name', ch['id']), ex))
            out.append({'id': ch['id'], 'name': ch.get('name'), 'ok': False})
        time.sleep(1)
    return out

radar = {'updated': datetime.now(TPE).strftime('%Y-%m-%d %H:%M'),
         'keywords': [], 'ig': [], 'yt': [], 'errors': errors}
try: radar['keywords'] = get_keywords()
except Exception as ex: errors.append('keywords: %s' % ex)
radar['ig'] = get_ig()
radar['yt'] = get_yt()

# 歷史快照：留 30 天
history['days'][TODAY] = today_snap
for d in sorted(history['days'])[:-30]:
    del history['days'][d]

os.makedirs(DATA, exist_ok=True)
with open(os.path.join(DATA, 'radar.json'), 'w', encoding='utf-8') as f:
    json.dump(radar, f, ensure_ascii=False, separators=(',', ':'))
with open(os.path.join(DATA, 'history.json'), 'w', encoding='utf-8') as f:
    json.dump(history, f, ensure_ascii=False, separators=(',', ':'))

print('OK 關鍵字 %d 筆 / IG %d 帳號(成功 %d) / YT %d 頻道(成功 %d)' % (
    len(radar['keywords']), len(radar['ig']), sum(1 for a in radar['ig'] if a.get('ok')),
    len(radar['yt']), sum(1 for c in radar['yt'] if c.get('ok'))))
if errors: print('部分失敗：', '; '.join(errors), file=sys.stderr)
