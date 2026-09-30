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

    def fake_whisper(path, model_size="small", language=None):
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


def test_tts_chunks_joins_and_retries(tmp_path):
    import urllib.error
    from reuploader.story import tts
    text = ("Первое предложение истории. " * 60).strip()
    parts = tts.chunks(text, limit=300)
    assert len(parts) > 1 and all(len(p) <= 300 for p in parts)
    calls, sleeps = [], []

    def fake(part, voice, style, model, key):
        calls.append((part, voice, style, key))
        if len(calls) == 1:
            raise urllib.error.HTTPError("u", 429, "limit", {}, None)
        return _wav_bytes(0.5)
    out = tts.synthesize(text, tmp_path / "v.wav", "KEY", voice="Kore", request=fake, sleep=sleeps.append)
    import wave
    with wave.open(str(out)) as w:
        n = len(tts.chunks(text))                            # synthesize режет по MAX_CHARS
        assert n > 1 and w.getframerate() == 24000 and w.getnframes() >= 24000 * 0.75 * n
    assert sleeps == [20] and calls[-1][1] == "Kore" and "рассказчик" in calls[-1][2]
    with pytest.raises(tts.TTSError):
        tts.synthesize("текст", tmp_path / "x.wav", "")
    raw_pcm = tts.synthesize("Hello there.", tmp_path / "p.wav", "K", lang="en",
                             request=lambda *a: b"\x00\x00" * 2400)          # «голый» PCM тоже понимаем
    assert raw_pcm.exists()


def test_google_voice_instead_of_own(bot_env, media, monkeypatch):
    e = bot_env
    from reuploader.bot import stories
    e.s.gemini_api_key = "KEY"
    spoken = []

    def fake_synth(text, path, key, voice, lang):
        spoken.append((text, voice, key))
        shutil.copy(media[0] / "voice.ogg", path)        # «голос Google»
        return path
    monkeypatch.setattr(stories.gtts, "synthesize", fake_synth)
    sid = e.new_story(text=" ".join(w.text for w in e.voice_words))
    asyncio.run(e.w.run(e.db.story(sid)))
    item = e.db.story_scripts(sid)[0]
    assert e.tg.sent[-1][1][0][0]["callback_data"] == f"st_tts:{item['id']}"   # кнопка под сценарием
    ok, _ = e.w.request_tts(item, "Kore")
    assert ok and e.db.story_script(item["id"])["status"] == "queued"
    asyncio.run(e.w.render(e.db.next_story_script()))
    done = e.db.story_script(item["id"])
    assert done["status"] == "done" and spoken[0][1] == "Kore" and spoken[0][2] == "KEY"
    assert probe(done["out_path"]).height == 1920


def test_auto_tts_after_scripts(bot_env, monkeypatch):
    e = bot_env
    e.s.gemini_api_key = "KEY"
    sid = e.new_story(text="Смотри что сейчас будет. Это важно запомнить.")
    e.db.update_story(sid, tts=1, tts_voice="Charon")
    asyncio.run(e.w.run(e.db.story(sid)))
    item = e.db.story_scripts(sid)[0]
    assert item["status"] == "queued" and item["voice_src"] == "tts:Charon"
    e.s.gemini_api_key = ""
    ok, text = e.w.request_tts(item)
    assert not ok and "GEMINI_API_KEY" in text


def test_voice_preview_is_cached(bot_env, monkeypatch):
    e = bot_env
    from reuploader.bot import stories
    from reuploader.bot.web import WebApp
    from tests.test_e2e_helpers import init_data
    calls = []

    def fake_synth(text, path, key, voice, lang):
        calls.append((voice, lang))
        with open(path, "wb") as f:
            f.write(_wav_bytes(0.3))
        return path
    monkeypatch.setattr(stories.gtts, "synthesize", fake_synth)
    bot = SimpleNamespace(owner_id=777, has_access=lambda u: u == 777, app_key="k", cut=e.w.bot.cut, stories=e.w)

    async def go():
        async with TestClient(TestServer(WebApp(e.db, e.s, SimpleNamespace(poke=lambda: None), bot).build())) as c:
            h = {"X-Init-Data": init_data(777)}
            e.s.gemini_api_key = ""
            nokey = (await c.get("/api/tts/preview?voice=Puck", headers=h)).status
            e.s.gemini_api_key = "KEY"
            r1 = await c.get("/api/tts/preview?voice=Fenrir&lang=ru", headers=h)
            body = await r1.read()
            r2 = await c.get("/api/tts/preview?voice=Fenrir&lang=ru", headers=h)
            bad = (await c.get("/api/tts/preview?voice=Hacker", headers=h)).status
            lst = await (await c.get("/api/stories", headers=h)).json()
            return nokey, r1.status, r1.headers["Content-Type"], body[:4], r2.status, bad, lst
    nokey, s1, ctype, head, s2, bad, lst = asyncio.run(go())
    assert nokey == 400 and s1 == 200 and ctype == "audio/wav" and head == b"RIFF" and s2 == 200
    assert calls == [("Fenrir", "ru")]                       # второй раз — из кэша, лимит Google не тратится
    assert bad == 400 and len(lst["voices"]) == 30 and lst["voice_info"]["Charon"].startswith("информ")
