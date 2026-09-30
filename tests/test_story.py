"""Пересказы и теории: сценарии, привязка голоса, сборка ролика, путь через бота и API."""
import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from reuploader.ffmpeg_path import ffmpeg_exe
from reuploader.smartcut.analyze import Word
from reuploader.smartcut.media import probe
from reuploader.story import script as sc
from reuploader.story.assemble import align, build_story, plan_clips
from tests.synth import make_video
from tests.test_smartcut import SCENES


def W(a, b, t):
    return Word(a, b, t)


# ---------- сценарии ----------

def test_lines_and_manual_script_match_scenes():
    words = [W(0, .5, "Эльза"), W(.5, 1, "строит"), W(1, 1.5, "ледяной"), W(1.5, 2, "замок."),
             W(10, 10.5, "Анна"), W(10.5, 11, "ищет"), W(11, 11.5, "сестру"), W(11.5, 12, "в"), W(12, 12.5, "горах.")]
    lines = sc.lines_from_words(words)
    assert [t for _, _, t in lines] == ["Эльза строит ледяной замок.", "Анна ищет сестру в горах."]
    s = sc.manual_script("Анна отправляется искать сестру в горах. А Эльза строит ледяной замок!", lines, 60)
    assert [round(x["from"]) for x in s["lines"]] == [10, 0]          # фразы привязаны к своим сценам
    with pytest.raises(ValueError):
        sc.manual_script("   ", lines, 60)


def test_clean_scripts_clamps_ranges():
    got = sc.clean_scripts([{"title": "t", "kind": "theory", "overlay": "o", "why": "w",
                             "lines": [{"text": "a", "from": -5, "to": 3}, {"text": " ", "from": 1, "to": 2},
                                       {"text": "b", "from": 500, "to": 900}]}], duration=100)
    assert got[0]["kind"] == "theory" and len(got[0]["lines"]) == 2
    assert got[0]["lines"][0]["from"] == 0 and got[0]["lines"][1]["from"] <= 99 and got[0]["lines"][1]["to"] <= 100


def test_estimate_is_local_and_priced():
    lines = [(i * 3.0, i * 3.0 + 2, "какая-то реплика героя номер " + str(i)) for i in range(300)]
    e = sc.estimate(lines, 3, 60, "ru")
    assert e["model"] == sc.DEFAULT_MODEL and e["input_tokens"] > 3000 and 0 < e["usd"] < 1


class FakeClient:
    def __init__(self, payload, stop="end_turn"):
        self.calls = 0
        self.payload, self.stop = payload, stop
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls += 1
        assert kw["model"] == sc.DEFAULT_MODEL and kw["output_config"]["effort"] == "medium"
        assert kw["extra_body"] == {"fallbacks": "default"}
        usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        return SimpleNamespace(model=kw["model"], usage=usage, stop_reason=self.stop,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_write_scripts_parses_and_logs_cost():
    payload = {"scripts": [{"title": "Эльза не злодейка", "kind": "theory", "overlay": "А ЕСЛИ?", "why": "интрига",
                            "lines": [{"text": "Смотрите.", "from": 1, "to": 4}]}]}
    logged = []
    scripts, note = sc.write_scripts(FakeClient(payload), [(0, 2, "реплика")], "auto", "ru", 1, 60, 100,
                                     usage_log=logged.append)
    assert note is None and scripts[0]["title"] == "Эльза не злодейка" and logged and logged[0]["usd"] > 0
    scripts, note = sc.write_scripts(FakeClient(payload, stop="refusal"), [(0, 2, "x")], "auto", "ru", 1, 60, 100)
    assert scripts == [] and "отказ" in note


# ---------- голос и сборка ----------

def test_align_follows_voice_even_with_deviation():
    lines = [{"text": "Эльза строит замок", "from": 0, "to": 3}, {"text": "Анна ищет сестру", "from": 5, "to": 8},
             {"text": "И тут появляется Олаф", "from": 9, "to": 12}]
    words = [W(.2, .6, "Эльза"), W(.6, 1, "строит"), W(1, 1.4, "огромный"), W(1.4, 2, "замок"),
             W(3, 3.4, "Анна"), W(3.4, 3.8, "ищет"), W(3.8, 4.4, "сестру"),
             W(6, 6.3, "тут"), W(6.3, 7, "появляется"), W(7, 7.6, "Олаф")]
    spans = align(lines, words, 8.0)
    assert spans[0][0] == 0 and abs(spans[1][0] - 3.0) < 0.01 and abs(spans[2][0] - 6.0) < 0.01
    assert spans[-1][1] == 8.0
    clips = plan_clips(lines, spans, src_dur=10)
    assert clips[2][0] + clips[2][1] <= 10                       # не вылезаем за конец мультфильма


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("story")
    _, _, src_words, _ = make_video(d / "movie.mp4", SCENES, size="640x360")
    _, _, voice_words, _ = make_video(d / "voice.mp4", SCENES[:2], size="160x120")
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(d / "voice.mp4"), "-vn",
                    "-c:a", "libopus", str(d / "voice.ogg")], check=True)
    return d, src_words, voice_words


def test_build_story_renders_vertical_video(media, tmp_path):
    d, src_words, voice_words = media
    lines = sc.lines_from_words(src_words)
    script = sc.manual_script(" ".join(w.text for w in voice_words), lines, probe(d / "movie.mp4").duration)
    script["overlay"] = "Что было дальше?"
    rep = build_story(d / "movie.mp4", d / "voice.ogg", script, tmp_path / "out.mp4",
                      lambda p: list(voice_words), tmp_path / "w")
    info = probe(tmp_path / "out.mp4")
    assert (info.width, info.height) == (1080, 1920) and info.audio_streams == 1
    assert info.duration == pytest.approx(probe(d / "voice.ogg").duration, abs=0.3)
    assert "ЧТО БЫЛО ДАЛЬШЕ" in (tmp_path / "w" / "story.ass").read_text(encoding="utf-8")
    assert rep["lines"] == len(script["lines"])


# ---------- через бота ----------

class TG:
    def __init__(self, voice_src=None):
        self.sent, self.videos, self.voice_src, self.mid = [], [], voice_src, 100

    async def send(self, chat, text, buttons=None):
        self.mid += 1
        self.sent.append((text, buttons))
        return {"message_id": self.mid}

    async def send_video(self, chat, path, caption, w=None, h=None):
        self.videos.append(path)

    async def download(self, file_id, dst):
        shutil.copy(self.voice_src, dst)
        return dst


@pytest.fixture
def bot_env(tmp_path, monkeypatch, media):
    d, src_words, voice_words = media
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")        # тем же токеном подписан init_data в тестах API
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OWNER_ID", "777")
    from reuploader.bot import stories
    from reuploader.bot.db import DB
    from reuploader.bot.settings import load_settings

    def fake_whisper(path, model_size="small", language=None, progress=None):
        if progress:
            progress(0.5)
        return list(voice_words) if str(path).endswith((".ogg", ".oga")) else list(src_words)
    monkeypatch.setattr(stories, "whisper_transcribe", fake_whisper)
    s = load_settings()
    db = DB(s.db_path)
    tg = TG(d / "voice.ogg")
    allowed = {"ai": True, "money": True}
    cut = SimpleNamespace(ai_allowed=lambda uid: allowed["ai"] and uid == 777,
                          balance=SimpleNamespace(can_afford=lambda usd: (allowed["money"], {"remaining": 5.0})))
    bot = SimpleNamespace(tg=tg, cut=cut, public_url="https://x", owner_id=777)
    w = stories.StoryWorker(db, s, bot)
    bot.stories = w

    def new_story(kind="manual", text=""):
        sid = db.create_story(777, "frozen.mp4", 1)
        dd = stories.story_dir(s, sid)
        dd.mkdir(parents=True)
        shutil.copy(d / "movie.mp4", dd / "src.mp4")
        db.update_story(sid, src_path=str(dd / "src.mp4"), status="queued", kind=kind, manual_text=text,
                        duration=probe(d / "movie.mp4").duration, count=2, seconds=45)
        return sid
    return SimpleNamespace(db=db, s=s, tg=tg, w=w, new_story=new_story, allowed=allowed, voice_words=voice_words,
                           )


def test_manual_story_then_voice_reply_makes_video(bot_env):
    e = bot_env
    sid = e.new_story(text=" ".join(w.text for w in e.voice_words))
    asyncio.run(e.w.run(e.db.story(sid)))
    assert e.db.story(sid)["status"] == "scripts"
    item = e.db.story_scripts(sid)[0]
    assert item["tg_message_id"] and "Ответь на это сообщение голосовым" in e.tg.sent[-1][0]
    # голосовое, но НЕ ответом на сценарий — не наше
    assert not asyncio.run(e.w.on_voice({"chat": {"id": 777}, "voice": {"file_id": "f"}}))
    msg = {"chat": {"id": 777}, "voice": {"file_id": "f", "file_size": 1000},
           "reply_to_message": {"message_id": item["tg_message_id"]}}
    assert asyncio.run(e.w.on_voice(msg))
    assert e.db.story_script(item["id"])["status"] == "queued"
    asyncio.run(e.w.render(e.db.next_story_script()))
    done = e.db.story_script(item["id"])
    assert done["status"] == "done" and done["dl_token"] and e.tg.videos
    assert probe(done["out_path"]).height == 1920


def test_ai_is_not_called_without_yes(bot_env, monkeypatch):
    e = bot_env
    from reuploader.bot import stories
    calls = []
    monkeypatch.setattr(stories.sc, "write_scripts", lambda *a, **k: calls.append(1) or (
        [{"title": "T", "kind": "recap", "overlay": "", "why": "w", "lines": [{"text": "x", "from": 0, "to": 3}]}] * 2,
        None))
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: object())
    sid = e.new_story(kind="auto")
    asyncio.run(e.w.run(e.db.story(sid)))                      # расшифровка + оценка цены
    st = e.db.story(sid)
    assert st["status"] == "confirm" and not calls
    assert e.tg.sent[-1][1][0][0]["callback_data"] == f"st_yes:{sid}"
    ok, _ = e.w.decide(st, False)                              # «Нет» — ничего не тратим
    assert ok and e.db.story(sid)["status"] == "uploaded" and not calls
    e.db.update_story(sid, status="queued", ai_state=None)
    asyncio.run(e.w.run(e.db.story(sid)))
    ok, _ = e.w.decide(e.db.story(sid), True)
    assert ok and e.db.story(sid)["status"] == "queued"
    asyncio.run(e.w.run(e.db.story(sid)))
    assert calls == [1] and len(e.db.story_scripts(sid)) == 2 and e.db.story(sid)["status"] == "scripts"


def test_no_money_no_request(bot_env):
    e = bot_env
    e.allowed["money"] = False
    sid = e.new_story(kind="recap")
    asyncio.run(e.w.run(e.db.story(sid)))
    assert e.db.story(sid)["status"] == "failed" and "средств" in e.db.story(sid)["error"]


def test_api_upload_and_run(bot_env, tmp_path, media):
    e = bot_env
    d = media[0]
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    data = (d / "movie.mp4").read_bytes()
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u in (777, 555), app_key="k", cut=e.w.bot.cut,
                          stories=e.w)
    e.w.bot.cut = bot.cut

    async def go():
        async with TestClient(TestServer(WebApp(e.db, e.s, SimpleNamespace(poke=lambda: None), bot).build())) as c:
            h = {"X-Init-Data": init_data(777)}
            r = await c.post("/api/stories", json={"filename": "Frozen.mp4", "size": len(data)}, headers=h)
            st = await r.json()
            r = await c.put(f"/api/stories/{st['id']}/chunk?offset=0", data=data, headers=h)
            up = await r.json()
            r = await c.post(f"/api/stories/{st['id']}/run", json={"kind": "manual", "text": "коротко"}, headers=h)
            short = r.status
            r = await c.post(f"/api/stories/{st['id']}/run", json={"kind": "recap", "count": 9, "seconds": 5}, headers=h)
            ran = await r.json()
            other = await c.get(f"/api/stories/{st['id']}", headers={"X-Init-Data": init_data(555)})
            return up, short, ran, other.status
    up, short, ran, other = asyncio.run(go())
    assert up["status"] == "uploaded" and up["duration"] > 10
    assert short == 400
    assert ran["status"] == "queued" and ran["count"] == 5 and ran["seconds"] == 30
    assert other == 404                                         # чужой мультфильм не виден


# ---------- без API: задание для чата Claude ----------

def test_parse_chat_answer_timed_and_plain():
    lines = [(0, 3, "Эльза строит ледяной замок."), (10, 13, "Анна ищет сестру в горах.")]
    answer = """Вот сценарии:
### Эльза не злодейка
Надпись: А ЕСЛИ ОНА ЗНАЛА?
[0-3] Все думают, что Эльза сбежала.
[10.5–13] Но Анна идёт за ней в горы.

### **Холодное сердце за минуту**
Анна ищет сестру в горах. Эльза строит ледяной замок!"""
    got = sc.parse_chat_answer(answer, lines, 60)
    assert [g["title"] for g in got][1:] == ["Эльза не злодейка", "Холодное сердце за минуту"]
    assert got[0]["lines"][0]["text"] == "Вот сценарии:"                     # вводная строка — отдельно, не ломает
    t = got[1]
    assert t["overlay"] == "А ЕСЛИ ОНА ЗНАЛА?" and [l["from"] for l in t["lines"]] == [0, 10.5]
    assert [round(l["from"]) for l in got[2]["lines"]] == [10, 0]            # без таймкодов — по словам
    with pytest.raises(ValueError):
        sc.parse_chat_answer("   \n\n", lines, 60)


def test_chat_mode_needs_no_api(bot_env, media):
    e = bot_env
    e.allowed["ai"] = False                                     # AI недоступен — чат-режим всё равно работает
    sid = e.new_story(kind="chat")
    asyncio.run(e.w.run(e.db.story(sid)))
    st = e.db.story(sid)
    assert st["status"] == "chat" and "задание" in e.tg.sent[-1][0]
    from reuploader.bot import stories
    assert (stories.story_dir(e.s, sid) / "words.json").exists()
    answer = "### Тест\n[1-4] Смотри что сейчас будет.\n[5-9] Это важно запомнить."
    e.db.update_story(sid, status="queued", kind="manual", manual_text=answer)
    asyncio.run(e.w.run(e.db.story(sid)))
    assert e.db.story(sid)["status"] == "scripts"
    body = json.loads(e.db.story_scripts(sid)[0]["body"])
    assert body["title"] == "Тест" and body["lines"][1]["from"] == 5


# ---------- бесплатная озвучка Google (Gemini TTS) ----------

def _wav_bytes(seconds=1.0, rate=24000):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds))
    return buf.getvalue()


KEY = "sk_" + "a1" * 24


def test_eleven_chunks_joins_and_retries(tmp_path):
    import wave
    from reuploader.story import eleven
    text = ("Первое предложение истории. " * 300).strip()
    parts = eleven.chunks(text)
    assert len(parts) > 1 and all(len(p) <= eleven.MAX_CHARS for p in parts)
    calls, sleeps = [], []

    def fake(method, path, key, body=None):
        calls.append((method, path, key, body))
        if len(calls) == 1:
            raise eleven.TTSError("ElevenLabs 429: too_many_concurrent_requests")
        return b"\x01\x00" * 12000
    out = eleven.synthesize(text, tmp_path / "v.wav", KEY, "voice123", request=fake, sleep=sleeps.append)
    with wave.open(str(out)) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 12000 * len(parts)
    assert sleeps == [5] and calls[-1][1] == "/v1/text-to-speech/voice123?output_format=pcm_24000"
    assert calls[-1][2] == KEY and calls[-1][3]["model_id"] == "eleven_multilingual_v2"
    assert "previous_text" in calls[-1][3] and "next_text" in calls[1][3]
    with pytest.raises(eleven.TTSError):
        eleven.synthesize("текст", tmp_path / "x.wav", "", "voice123")
    e = eleven._error(401, '{"detail": {"status": "quota_exceeded", "message": "x"}}')
    assert "символы" in str(e)
    assert "ключ" in str(eleven._error(401, '{"detail": {"status": "invalid_api_key"}}'))
    assert eleven.mask(KEY) == "sk_…a1a1" and KEY not in eleven.mask(KEY)


def _fake_voices(key, request=None):
    from reuploader.story import eleven
    if key != KEY:
        raise eleven.TTSError("ElevenLabs отклонил ключ")
    return [{"id": "voiceAAA1", "name": "Adam", "info": "male, deep", "preview": "https://x/a.mp3"},
            {"id": "voiceBBB2", "name": "Rachel", "info": "female", "preview": "https://x/r.mp3"}]


def test_eleven_voice_instead_of_own(bot_env, media, monkeypatch):
    e = bot_env
    from reuploader.bot import stories
    spoken = []

    def fake_synth(text, path, key, voice):
        spoken.append((text, voice, key))
        shutil.copy(media[0] / "voice.ogg", path)        # «голос ElevenLabs»
        return path
    monkeypatch.setattr(stories.eleven, "synthesize", fake_synth)
    sid = e.new_story(text=" ".join(w.text for w in e.voice_words))
    asyncio.run(e.w.run(e.db.story(sid)))
    item = e.db.story_scripts(sid)[0]
    assert e.tg.sent[-1][1] is None                       # нет своего ключа — нет кнопки
    ok, text = e.w.request_tts(item, "voiceAAA1")
    assert not ok and "ElevenLabs" in text
    e.db.set_eleven(777, key=KEY, voice="voiceBBB2")
    e.db.set_eleven(555, key="sk_" + "zz" * 24, voice="other")   # ключ другого пользователя не трогаем
    asyncio.run(e.w.send_script(item, 777))
    assert e.tg.sent[-1][1][0][0]["callback_data"] == f"st_tts:{item['id']}"   # кнопка под сценарием
    ok, _ = e.w.request_tts(item, "voiceAAA1")
    assert ok and e.db.story_script(item["id"])["status"] == "queued"
    asyncio.run(e.w.render(e.db.next_story_script()))
    done = e.db.story_script(item["id"])
    assert done["status"] == "done" and spoken[0][1] == "voiceAAA1" and spoken[0][2] == KEY
    assert probe(done["out_path"]).height == 1920


def test_auto_tts_after_scripts(bot_env, monkeypatch):
    e = bot_env
    e.db.set_eleven(777, key=KEY, voice="voiceBBB2")
    sid = e.new_story(text="Смотри что сейчас будет. Это важно запомнить.")
    e.db.update_story(sid, tts=1)
    asyncio.run(e.w.run(e.db.story(sid)))
    item = e.db.story_scripts(sid)[0]
    assert item["status"] == "queued" and item["voice_src"] == "tts:voiceBBB2"   # голос из настроек
    e.db.set_eleven(777, key="", voice="")
    ok, text = e.w.request_tts(item)
    assert not ok and "ключ" in text


def test_own_eleven_key_api(bot_env, monkeypatch):
    e = bot_env
    from reuploader.bot import stories
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    monkeypatch.setattr(stories.eleven, "voices", _fake_voices)
    monkeypatch.setattr(stories.eleven, "quota", lambda key: (1200, 10000))
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u in (777, 555), app_key="k", cut=e.w.bot.cut,
                          stories=e.w)

    async def go():
        async with TestClient(TestServer(WebApp(e.db, e.s, SimpleNamespace(poke=lambda: None), bot).build())) as c:
            me, friend = {"X-Init-Data": init_data(777)}, {"X-Init-Data": init_data(555)}
            r = {"empty": await (await c.get("/api/eleven", headers=me)).json()}
            r["bad"] = (await c.put("/api/eleven", json={"key": "sk_" + "b" * 40}, headers=me)).status
            r["short"] = (await c.put("/api/eleven", json={"key": "abc"}, headers=me)).status
            resp = await c.put("/api/eleven", json={"key": KEY}, headers=me)
            r["saved"], r["saved_raw"] = await resp.json(), await resp.text()
            r["voice"] = await (await c.put("/api/eleven", json={"voice": "voiceBBB2"}, headers=me)).json()
            r["hack"] = (await c.put("/api/eleven", json={"voice": "../../x"}, headers=me)).status
            r["friend"] = await (await c.get("/api/eleven", headers=friend)).json()
            r["list"] = await (await c.get("/api/stories", headers=me)).json()
            r["flist"] = await (await c.get("/api/stories", headers=friend)).json()
            r["del"] = await (await c.delete("/api/eleven", headers=me)).json()
            return r
    r = asyncio.run(go())
    assert r["empty"]["has_key"] is False and r["bad"] == 400 and r["short"] == 400
    assert r["saved"]["has_key"] and r["saved"]["masked"] == "sk_…a1a1" and KEY not in r["saved_raw"]
    assert r["saved"]["voice"] == "voiceAAA1" and len(r["saved"]["voices"]) == 2   # первый голос по умолчанию
    assert r["saved"]["quota"] == {"used": 1200, "limit": 10000}
    assert r["voice"]["voice"] == "voiceBBB2" and r["hack"] == 400
    assert r["friend"]["has_key"] is False                  # друг ключом владельца не пользуется
    assert r["list"]["tts_available"] is True and r["flist"]["tts_available"] is False
    assert r["del"]["has_key"] is False and e.db.eleven(777) == ("", "")
