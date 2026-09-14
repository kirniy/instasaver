import asyncio
from unittest.mock import AsyncMock
import pytest
from instasaver.telegram import Telegram,TelegramError

class Response:
    status=200
    def __init__(self,data):self.data=data
    async def __aenter__(self):return self
    async def __aexit__(self,*a):pass
    async def json(self,**kw):return self.data

class Session:
    def __init__(self,*responses):self.responses=list(responses);self.calls=[]
    def post(self,url,**kw):
        self.calls.append((url,kw));return Response(self.responses.pop(0))

@pytest.mark.asyncio
async def test_retry_after_is_respected(monkeypatch):
    bot=object.__new__(Telegram);bot.url='http://test/';bot.session=Session({'ok':False,'error_code':429,'parameters':{'retry_after':7}},{'ok':True,'result':True})
    sleep=AsyncMock();monkeypatch.setattr(asyncio,'sleep',sleep)
    assert await bot.call('setMessageReaction',chat_id=1,message_id=2)
    assert sleep.await_args.args[0]>=7
    assert len(bot.session.calls)==2

@pytest.mark.asyncio
async def test_rich_fallback(monkeypatch):
    bot=object.__new__(Telegram);bot.rich=True
    bot.call=AsyncMock(side_effect=[TelegramError(404,'unsupported'),{'message_id':1}])
    result=await bot.message(1,'<b>Hi</b>\n\nthere')
    assert result['message_id']==1 and not bot.rich
    assert bot.call.await_args_list[0].kwargs['rich_message']=={'html':'<p><b>Hi</b></p><p>there</p>'}
    assert bot.call.await_args_list[1].args[0]=='sendMessage'

@pytest.mark.asyncio
async def test_ambiguous_upload_does_not_retry():
    import aiohttp
    bot=object.__new__(Telegram);bot.url='http://test/'
    class Broken:
        calls=0
        def post(self,*a,**kw):
            self.calls+=1;raise aiohttp.ClientConnectionError()
    bot.session=Broken()
    with pytest.raises(TelegramError) as e:await bot.call('sendDocument',chat_id=1,document='file://a')
    assert e.value.code==0 and bot.session.calls==1

@pytest.mark.asyncio
async def test_upload_checkpoint_prevents_duplicate(tmp_path,monkeypatch):
    from instasaver.app import App
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('OWNER_ID','1');monkeypatch.setenv('BOT_TOKEN','test')
    app=App();app.tg.call=AsyncMock(return_value={'message_id':42,'document':{'file_id':'file123'}})
    job={'id':1,'chat':1}
    await app.send_once(job,'file.mp4','document',tmp_path/'file.mp4')
    await app.send_once(job,'file.mp4','document',tmp_path/'file.mp4')
    assert app.tg.call.await_count==1
    await app.tg.close()

@pytest.mark.asyncio
async def test_cancel_checks_job_owner(tmp_path,monkeypatch):
    from instasaver.app import App
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('OWNER_ID','1');monkeypatch.setenv('BOT_TOKEN','test')
    app=App();job=app.store.add(1,{'chat':{'id':1},'from':{'id':1},'message_id':1},'https://example.com')
    await app.cancel(job['id'],2)
    assert app.store.db.execute('SELECT state FROM jobs').fetchone()[0]=='queued'
    await app.tg.close()
