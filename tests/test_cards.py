import json
from instasaver.media import post_info, nice_name, rename_files
from instasaver.app import url_key, card_parts, card_buttons, short, clock
from instasaver.store import Store


def write(folder,data,name='x [id].info.json'):
    (folder/name).write_text(json.dumps(data))


def test_instagram_name_uses_handle_and_caption(tmp_path):
    write(tmp_path,{'title':'Video by atlanta.night','uploader':'ATLANTA NIGHT','channel':'atlanta.night','uploader_id':'46372692342',
        'description':'HORRORLAND ATLANTA x BUSHIDO ZHO🎃\nСанкт-Петербург\n\n31 октября','like_count':77,'comment_count':5,'upload_date':'20261014','id':'Deei'})
    info=post_info(tmp_path,'https://www.instagram.com/reel/Deei/?igsh=1')
    assert info['creator']=='@atlanta.night' and info['name']=='ATLANTA NIGHT'
    assert info['profile']=='https://www.instagram.com/atlanta.night/'
    assert nice_name(info)=='atlanta.night — HORRORLAND ATLANTA x BUSHIDO ZHO🎃'
    assert info['date']=='14.10.2026'


def test_twitter_title_strips_author_prefix(tmp_path):
    write(tmp_path,{'title':'Nigroll - Мелани Филипс очень доходчиво объясняет удивите','uploader':'Nigroll','uploader_id':'nigroll',
        'description':'Мелани Филипс очень доходчиво объясняет удивительное превращение левых либералов в бесчеловечную фашистоидную дрянь. https://t.co/x'})
    name=nice_name(post_info(tmp_path,'https://x.com/nigroll/status/1'))
    assert name.startswith('nigroll — Мелани Филипс очень доходчиво') and name.endswith('…')
    assert '/' not in name and len(name.encode())<=160


def test_youtube_keeps_title(tmp_path):
    write(tmp_path,{'title':'Me at the zoo','channel':'jawed','uploader':'jawed','channel_url':'https://www.youtube.com/channel/x','description':'long'})
    info=post_info(tmp_path,'https://youtu.be/jNQXAC9IVRw')
    assert nice_name(info)=='jawed — Me at the zoo' and info['profile'].startswith('https://www.youtube.com/')


def test_name_fallbacks_and_unsafe_chars(tmp_path):
    assert nice_name(post_info(tmp_path,'https://example.com/a.mp4'))=='Instasaver'
    assert '/' not in nice_name({'creator':'a/b','title':'../../etc:passwd?','id':''})


def test_gallery_metadata(tmp_path):
    g=tmp_path/'gallery';g.mkdir()
    write(g,{'username':'vnvnc_spb','fullname':'VNVNC','description':'Пятница 🔥','likes':10,'date':'2026-10-10 20:00:00'},'01.jpg.json')
    info=post_info(tmp_path,'https://instagram.com/p/abc/')
    assert info['creator']=='@vnvnc_spb' and info['likes']==10 and info['date']=='10.10.2026'


def test_rename_numbering(tmp_path):
    files=[tmp_path/'a.JPG',tmp_path/'b.mp4']
    for f in files:f.write_bytes(b'x')
    out=rename_files(files,'me — post')
    assert [f.name for f in out]==['me — post 01.jpg','me — post 02.mp4'] and all(f.exists() for f in out)


def test_url_key_ignores_share_junk():
    assert url_key('https://www.instagram.com/reel/Deei-MXRzhz/?vrfl=abc')==url_key('https://instagram.com/reels/Deei-MXRzhz?igsh=1')
    assert url_key('https://youtu.be/abc?si=x')==url_key('https://www.youtube.com/watch?v=abc&feature=share')
    assert url_key('https://twitter.com/a/status/1?s=20')==url_key('https://x.com/a/status/1')
    assert url_key('https://example.com/v?id=1')!=url_key('https://example.com/v?id=2')


def test_card_is_separate_and_escaped():
    info={'site':'instagram','creator':'@a<b','name':None,'profile':'https://www.instagram.com/a/','title':'x','likes':12345,
        'views':None,'comments':3,'duration':158,'date':'14.10.2026'}
    parts=card_parts(info,'<script>'+'я'*5000,'https://instagram.com/reel/x')
    assert len(parts)==2 and '&lt;script&gt;' in parts[0] and '@a&lt;b' in parts[0]
    assert 'blockquote expandable' in parts[1] and '❤️ 12,3 тыс' in parts[0] and '⏱ 2:38' in parts[0]
    assert card_buttons({'id':5,'url':'https://x','mode':'video'},info)['inline_keyboard'][0][1]['callback_data']=='audio:5'
    assert len(card_buttons({'id':5,'url':'https://x','mode':'audio'},info)['inline_keyboard'][0])==1


def test_numbers():
    assert short(999)=='999' and short(1500)=='1,5 тыс' and short(444051952)=='444 млн' and clock(3725)=='1:02:05'


def test_cache_privacy(tmp_path):
    db=Store(tmp_path/'db')
    db.remember('k','video',1,True,{'items':[1]})
    assert db.cached('k','video',1)=={'items':[1]} and db.cached('k','video',2) is None
    db.remember('k','video',1,False,{'items':[2]})
    assert db.cached('k','video',2)=={'items':[2]}
    db.forget('k','video');assert db.cached('k','video',1) is None
