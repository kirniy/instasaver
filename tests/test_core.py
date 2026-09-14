import asyncio
import json
from pathlib import Path
import pytest
from instasaver.net import validate_url, public_address
from instasaver.media import geometry, playback, run_process, cookie_path
from instasaver.store import Store
from instasaver.app import links

@pytest.mark.parametrize('url',['file:///etc/passwd','http://127.0.0.1','http://[::1]/','http://169.254.169.254/','ftp://example.com','https://localhost','https://x.local','https://user:pass@example.com','http://example.com:8080','http://10.0.0.1','http://100.100.100.100'])
def test_block_unsafe_urls(url):
    with pytest.raises(ValueError):validate_url(url)

def test_valid_url():
    assert validate_url('https://www.youtube.com/watch?v=123')

@pytest.mark.asyncio
async def test_dns_private_block(monkeypatch):
    async def resolve(*a,**kw):return [(2,1,6,'',('127.0.0.1',443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    with pytest.raises(ValueError):await public_address('example.com',443)

@pytest.mark.parametrize('stream,expected',[
    ({'width':1080,'height':1920},(1080,1920)),
    ({'width':1920,'height':1080,'side_data_list':[{'rotation':-90}]},(1080,1920)),
    ({'width':720,'height':576,'sample_aspect_ratio':'16:15'},(768,576)),
    ({'width':720,'height':576,'sample_aspect_ratio':'N/A'},(720,576)),
])
def test_geometry(stream,expected):assert geometry(stream)==expected

def test_links():
    assert links({'text':'📺 https://youtu.be/a.','entities':[{'type':'text_link','url':'https://instagram.com/reel/a/'}]})==['https://youtu.be/a','https://instagram.com/reel/a/']

def test_durable_queue(tmp_path):
    db=Store(tmp_path/'db');update={'update_id':123,'message':{'text':'x'}}
    db.ingest([update,update]);assert len(db.pending())==1;assert db.offset()==124
    msg={'chat':{'id':1},'from':{'id':1},'message_id':4}
    a=db.add(123,msg,'https://example.com');b=db.add(123,msg,'https://example.com');assert a['id']==b['id']
    db.execute("UPDATE jobs SET state='running'")
    db.execute('INSERT INTO sent VALUES(?,?,?,?,?)',(a['id'],'a.mp4','video','file1',7))
    db.db.close();db=Store(tmp_path/'db')
    assert db.db.execute('SELECT state FROM jobs').fetchone()[0]=='queued'
    assert db.db.execute('SELECT file_id FROM sent').fetchone()[0]=='file1'
    assert db.offset()==124

def test_cookie_domain_boundary(tmp_path):
    (tmp_path/'youtube.txt').touch()
    assert cookie_path('https://youtube.com.evil.com/a',tmp_path) is None
    assert cookie_path('https://www.youtube.com/a',tmp_path)

@pytest.mark.asyncio
async def test_playback_portrait(tmp_path):
    source=tmp_path/'source.mp4'
    code,_=await run_process(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=180x320:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(source)])
    assert code==0
    dest,info,thumb=await playback(source,tmp_path)
    assert (info['width'],info['height'])==(180,320)
    assert thumb and dest.exists() and source.exists()
    data=dest.read_bytes();assert data.index(b'moov')<data.index(b'mdat')

@pytest.mark.asyncio
async def test_playback_anamorphic(tmp_path):
    source=tmp_path/'source.mp4'
    code,_=await run_process(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=180x320:d=1','-vf','setsar=2','-c:v','libx264','-pix_fmt','yuv420p',str(source)])
    assert code==0
    dest,info,thumb=await playback(source,tmp_path)
    assert (info['width'],info['height'])==(360,320)
    assert info['stream']['sample_aspect_ratio']=='1:1'

@pytest.mark.asyncio
async def test_cancel_process():
    import sys
    task=asyncio.create_task(run_process([sys.executable,'-c','import time;time.sleep(100)']))
    await asyncio.sleep(.1);task.cancel()
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,8)

def test_rich_html_preserves_layout():
    from instasaver.telegram import rich_html
    assert rich_html('<b>Title</b>\n\nfirst\nsecond')=='<p><b>Title</b></p><p>first<br>second</p>'

@pytest.mark.asyncio
async def test_app_constructs_in_event_loop(tmp_path,monkeypatch):
    from instasaver.app import App
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('OWNER_ID','1');monkeypatch.setenv('BOT_TOKEN','test')
    app=App()
    assert app.store.offset()==0
    await app.tg.close()

@pytest.mark.asyncio
async def test_silent_child_disk_limit(tmp_path):
    import sys
    from instasaver.media import DownloadError
    with pytest.raises(DownloadError):
        await run_process([sys.executable,'-c',"import time;open('big','wb').write(b'x'*1000);time.sleep(20)"],cwd=tmp_path,max_disk=100,timeout=8)
