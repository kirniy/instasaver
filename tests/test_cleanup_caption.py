import json
import os
import time
from instasaver.cleanup import cleanup_jobs
from instasaver.media import instagram_caption
from instasaver.store import Store


def test_cleanup_preserves_active_and_resumable(tmp_path):
    store=Store(tmp_path/'db');base=tmp_path/'downloads';base.mkdir()
    ids=[]
    for n in range(4):
        j=store.add(n,{'chat':{'id':1},'from':{'id':1},'message_id':n},f'https://example.com/{n}')
        ids.append(j['id']);folder=base/str(j['id']);folder.mkdir();(folder/'media.mp4').write_bytes(b'data')
    store.execute("UPDATE jobs SET state='done' WHERE id=?",(ids[0],))
    store.execute("UPDATE jobs SET state='running' WHERE id=?",(ids[1],))
    store.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-90000,ids[3]))
    unrelated=tmp_path/'other';unrelated.mkdir();(unrelated/'keep').touch()
    assert cleanup_jobs(store,tmp_path,active={ids[1]})==2
    assert not (base/str(ids[0])).exists()
    assert (base/str(ids[1])).exists()
    assert (base/str(ids[2])).exists()
    assert not (base/str(ids[3])).exists()
    assert (unrelated/'keep').exists()


def test_orphans_expire(tmp_path):
    store=Store(tmp_path/'db');folder=tmp_path/'downloads'/'999';folder.mkdir(parents=True)
    os.utime(folder,(0,0))
    assert cleanup_jobs(store,tmp_path)==1
    assert not folder.exists()


def test_ytdlp_caption(tmp_path):
    text='Первая строка 🎬\n\nОписание <не HTML> & #тег'
    (tmp_path/'video.info.json').write_text(json.dumps({'description':text}))
    assert instagram_caption(tmp_path,'https://www.instagram.com/reel/abc/')==text
    assert instagram_caption(tmp_path,'https://youtube.com/watch?v=abc')==''


def test_gallery_caption(tmp_path):
    folder=tmp_path/'gallery';folder.mkdir()
    (folder/'photo.jpg.json').write_text(json.dumps({'description':'Полная подпись'}))
    assert instagram_caption(tmp_path,'https://instagram.com/p/abc/')=='Полная подпись'


def test_no_caption(tmp_path):
    (tmp_path/'manifest.json').write_text('[]')
    assert instagram_caption(tmp_path,'https://instagram.com/p/abc/')==''
