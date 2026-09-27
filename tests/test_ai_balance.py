import asyncio
import json
import logging
import socket
from types import SimpleNamespace

import pytest

from reuploader.smartcut import ai, pricing
from reuploader.smartcut.beats import Beat
from reuploader.smartcut.analyze import Word


def beats3():
    bs = [Beat(0, 3, [Word(0.1, 1, "смотри"), Word(1.1, 2.5, "сюда.")]),
          Beat(3, 7, [Word(3.1, 6, "вода вода.")]),
          Beat(7, 10, [Word(7.1, 9, "итог.")])]
    return bs


class FakeClient:
    """Имитация anthropic.Anthropic: считает вызовы, возвращает JSON по схеме."""
    def __init__(self, payload=None, model="claude-opus-5", stop="end_turn"):
        self.calls = []
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                text = json.dumps(payload or {"beats": [
                    {"i": 0, "score": 9, "tag": "hook", "why": "цепляет", "refs": []},
                    {"i": 1, "score": 1, "tag": "filler", "why": "вода", "refs": []},
                    {"i": 2, "score": 8, "tag": "payoff", "why": "итог", "refs": [0]}]})
                return SimpleNamespace(model=model, stop_reason=stop,
                                       content=[SimpleNamespace(type="text", text=text)],
                                       usage=SimpleNamespace(input_tokens=1000, output_tokens=2000,
                                                             cache_creation_input_tokens=100,
                                                             cache_read_input_tokens=400))
        self.beta = SimpleNamespace(messages=Messages())


def test_estimate_is_offline_and_uses_config(monkeypatch, tmp_path):
    def blocked(*a, **k):
        raise AssertionError("сеть при оценке стоимости!")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    est = ai.estimate(beats3(), "claude-opus-5")
    assert est["input_tokens"] > 0 and est["output_tokens"] > 0 and est["usd"] > 0
    # цены берутся из конфига: вдвое дороже в конфиге — вдвое дороже оценка
    p = pricing.load()
    p["models"]["claude-opus-5"] = {k: v * 2 for k, v in p["models"]["claude-opus-5"].items()}
    assert ai.estimate(beats3(), "claude-opus-5", p)["usd"] == pytest.approx(est["usd"] * 2, rel=0.01)


def test_usage_cost_includes_cache_tokens():
    p = pricing.load()
    u = SimpleNamespace(input_tokens=1_000_000, output_tokens=0, cache_creation_input_tokens=1_000_000,
                        cache_read_input_tokens=1_000_000)
    _, usd = ai.usage_cost(p, "claude-opus-5", u)
    m = p["models"]["claude-opus-5"]
    assert usd == pytest.approx(m["input"] + m["cache_write"] + m["cache_read"])


def test_scorer_applies_scores_and_logs_cost():
    client, logged = FakeClient(), []
    scorer = ai.make_scorer(client, "claude-opus-5", usage_log=logged.append)
    bs = scorer(beats3(), None)
    assert [b.score for b in bs] == [9, 1, 8] and bs[2].refs == [0] and "развязка" in bs[2].why
    kw = client.calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["model"] == "claude-opus-5"
    assert logged and logged[0]["usd"] == pytest.approx(scorer.last_cost, abs=1e-4) and logged[0]["cache_read"] == 400


def test_refusal_keeps_heuristics():
    scorer = ai.make_scorer(FakeClient(stop="refusal"), "claude-opus-5")
    bs = beats3()
    for b in bs:
        b.score = 5
    scorer(bs, None)
    assert [b.score for b in bs] == [5, 5, 5] and scorer.note


# ---------- бот: без «Да» платных запросов нет ----------

@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OWNER_ID", "777")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-не-настоящий")
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    from reuploader.bot.cutjobs import CutWorker
    from reuploader.bot.db import DB
    from reuploader.bot.settings import load_settings
    s = load_settings()
    db = DB(s.db_path)
    sent = []

    class TG:
        async def send(self, chat, text, buttons=None):
            sent.append((text, buttons)); return {"message_id": 1}

        async def send_video(self, *a, **k):
            sent.append(("<video>", None))

        async def call(self, *a, **k):
            return {}
    bot = SimpleNamespace(tg=TG(), owner_id=777, public_url="https://x")
    w = CutWorker(db, s, bot)
    return db, s, w, sent


def _job(db, s, tmp_path, clip_src):
    import shutil
    from reuploader.bot.cutjobs import job_dir
    jid = db.create_cut_job(777, "clip.mp4", 1, status="queued")
    d = job_dir(s, jid); d.mkdir(parents=True)
    shutil.copy(clip_src, d / "src.mp4")
    db.update_cut_job(jid, src_path=str(d / "src.mp4"), target=30, mode="ai")
    return jid


@pytest.fixture(scope="module")
def clip_src(tmp_path_factory):
    from tests.synth import make_video
    from tests.test_smartcut import SCENES
    p = tmp_path_factory.mktemp("c") / "src.mp4"
    make_video(p, SCENES)
    return p


def test_ai_not_called_without_confirmation(bot_env, tmp_path, clip_src, monkeypatch):
    import anthropic
    db, s, w, sent = bot_env
    created = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: created.append(1) or FakeClient())
    jid = _job(db, s, tmp_path, clip_src)
    asyncio.run(w.run(db.cut_job(jid)))                      # шаг оценки
    job = db.cut_job(jid)
    assert job["status"] == "confirm" and job["ai_state"] == "awaiting" and not created
    assert "Продолжить?" in sent[-1][0] and sent[-1][1][0][0]["callback_data"] == f"ai_yes:{jid}"
    ok, _ = w.decide_ai(job, yes=False)                      # «Нет, бесплатный режим»
    asyncio.run(w.run(db.cut_job(jid)))
    assert ok and db.cut_job(jid)["status"] == "done" and not created
    assert db.ai_spent() == 0


def test_ai_called_once_after_yes_and_cost_logged(bot_env, tmp_path, clip_src, monkeypatch):
    import anthropic
    db, s, w, sent = bot_env
    client = FakeClient(payload={"beats": []})
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: client)
    jid = _job(db, s, tmp_path, clip_src)
    asyncio.run(w.run(db.cut_job(jid)))
    ok, _ = w.decide_ai(db.cut_job(jid), yes=True)
    asyncio.run(w.run(db.cut_job(jid)))
    job = db.cut_job(jid)
    assert ok and job["status"] == "done" and len(client.calls) == 1
    assert db.ai_spent() > 0 and "AI-анализ: фактически" in sent[-1][0]


def test_ai_blocked_when_estimate_exceeds_balance(bot_env, tmp_path, clip_src, monkeypatch):
    import anthropic
    db, s, w, sent = bot_env
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: pytest.fail("платный вызов!"))
    db.add_topup(0.001, "2026-09-01")
    jid = _job(db, s, tmp_path, clip_src)
    asyncio.run(w.run(db.cut_job(jid)))
    assert "Не запускаю" in sent[-1][0] and len(sent[-1][1][0]) == 1     # только бесплатная кнопка
    ok, why = w.decide_ai(db.cut_job(jid), yes=True)
    assert not ok and "Недостаточно" in why


def test_ai_hidden_without_key(bot_env, monkeypatch):
    db, s, w, sent = bot_env
    assert w.ai_allowed(777) and not w.ai_allowed(5)          # чужим — нет (деньги владельца)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert not w.ai_allowed(777)


def test_balance_summary_and_admin_cost_report(bot_env, monkeypatch, caplog):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from reuploader.bot import balance as bal
    db, s, w, sent = bot_env
    db.add_topup(10, "2026-09-01")
    db.add_topup(5, "2026-09-10")
    db.add_ai_usage(777, 1, {"model": "m", "input_tokens": 1, "output_tokens": 1, "cache_write": 0,
                             "cache_read": 0, "usd": 1.25})
    sm = w.balance.summary()
    assert sm["topped"] == 15 and sm["spent"] == 1.25 and sm["remaining"] == 13.75 and sm["source"] == "бот"

    secret = "sk-ant-admin01-СЕКРЕТ"
    seen = {}

    async def cost(req):
        seen["key"] = req.headers.get("x-api-key")
        page = req.query.get("page")
        if not page:
            return web.json_response({"data": [{"results": [{"amount": "250.5"}, {"amount": "100"}]}],
                                      "has_more": True, "next_page": "p2"})
        return web.json_response({"data": [{"results": [{"amount": "49.5"}]}], "has_more": False})

    async def go():
        app = web.Application(); app.router.add_get("/cost", cost)
        srv = TestServer(app); await srv.start_server()
        monkeypatch.setattr(bal, "COST_URL", str(srv.make_url("/cost")))
        monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", secret)
        with caplog.at_level(logging.DEBUG):
            await w.balance.refresh_admin(force=True)
        await srv.close()
    asyncio.run(go())
    sm = w.balance.summary()
    assert seen["key"] == secret and sm["spent"] == 4.0 and sm["source"] == "Anthropic Console"
    assert secret not in caplog.text and secret not in json.dumps(sm, ensure_ascii=False)

    async def warn():
        db.add_ai_usage(777, 1, {"model": "m", "input_tokens": 0, "output_tokens": 0, "cache_write": 0,
                                 "cache_read": 0, "usd": 0})
        db.set_meta("admin_spent", 12)       # остаток 3 < порога 5
        msgs = []

        async def notify(t):
            msgs.append(t)
        await w.balance.check_low(notify)
        await w.balance.check_low(notify)    # второй раз не дублируем
        return msgs
    assert len(asyncio.run(warn())) == 1
