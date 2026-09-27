import argparse
import shutil
from pathlib import Path

from .config import load_config
from .state import History

WORK_DIR = Path("work")


def _fmt_views(n):
    return "?" if n is None else f"{n:,}".replace(",", " ")


def _pick(job, history, source_override=None):
    """Самые просматриваемые ещё не перезалитые шортсы со всех источников задачи."""
    from .source import list_shorts

    sources = [source_override] if source_override else job["sources"]
    pool = []
    for src in sources:
        print(f"  сканирую {src}")
        pool += list_shorts(src, job["scan_limit"])
    pool.sort(key=lambda v: v["view_count"] or 0, reverse=True)
    fresh = [
        v for v in pool
        if v["id"] not in history and (v["view_count"] or 0) >= job["min_views"]
    ]
    return fresh[: job["per_run"]]


def _render_text(template, meta):
    return template.format(title=meta["title"], description=meta["description"]).strip()


def run_job(job, dry_run=False, source_override=None, keep_files=False):
    from .effects import apply_effects
    from .source import download
    from .uploader import channel_title, upload, youtube_client

    print(f"== Задача {job['name']} ==")
    history = History(job["name"])
    picked = _pick(job, history, source_override)
    if not picked:
        print("  нечего заливать — всё топовое уже перезалито")
        return

    youtube = None
    if not dry_run:
        youtube = youtube_client(job["token"])
        print(f"  целевой канал: {channel_title(youtube)}")

    work = WORK_DIR / job["name"]
    for v in picked:
        print(f"  -> {v['title']!r} ({_fmt_views(v['view_count'])} просмотров) {v['url']}")
        src, meta = download(v["url"], work)
        out = work / f"{meta['id']}.out.mp4"
        print("    уникализация...")
        apply_effects(src, out, job["effects"])

        title = _render_text(job["title_template"], meta) or meta["title"]
        description = _render_text(job["description_template"], meta)
        tags = (meta["tags"] if job["keep_tags"] else []) + list(job["extra_tags"])

        if dry_run:
            print(f"    [dry-run] готово: {out}")
            continue

        new_id = upload(
            youtube, out, title, description, tags,
            job["privacy"], job["category_id"], job["made_for_kids"],
        )
        history.add(meta["id"], new_id, title)
        print(f"    залито: https://www.youtube.com/shorts/{new_id}")
        if not keep_files:
            src.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    if not dry_run and not keep_files:
        shutil.rmtree(work, ignore_errors=True)


def main():
    p = argparse.ArgumentParser(prog="reuploader", description="Перезалив своих Shorts на другой канал")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="привязать целевой канал (создать токен)")
    a.add_argument("--token", required=True, help="куда сохранить токен, напр. tokens/main.json")
    a.add_argument("--client-secret", default=None)
    a.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")

    ls = sub.add_parser("top", help="показать топ шортсов канала по просмотрам")
    ls.add_argument("channel")
    ls.add_argument("-n", type=int, default=15)
    ls.add_argument("--scan-limit", type=int, default=200)

    r = sub.add_parser("run", help="перезалить топ-видео для задач из конфига")
    r.add_argument("--job", action="append", help="только эти задачи (можно несколько раз)")
    r.add_argument("--source", help="взять видео с этого канала вместо sources из конфига")
    r.add_argument("--dry-run", action="store_true", help="скачать и обработать, но не заливать")
    r.add_argument("--keep", action="store_true", help="не удалять файлы после заливки")

    fx = sub.add_parser("process", help="только применить эффекты к локальному файлу")
    fx.add_argument("input")
    fx.add_argument("output")
    fx.add_argument("--job", help="взять эффекты из этой задачи (иначе из defaults)")

    args = p.parse_args()

    if args.cmd == "top":
        from .source import list_shorts

        for i, v in enumerate(list_shorts(args.channel, args.scan_limit)[: args.n], 1):
            print(f"{i:>3}. {_fmt_views(v['view_count']):>12}  {v['url']}  {v['title']}")
        return

    if args.cmd == "auth":
        from .uploader import authorize, channel_title, youtube_client

        secret = args.client_secret
        if not secret:
            try:
                secret = load_config(args.config)["defaults"].get("client_secret")
            except SystemExit:
                pass
        secret = secret or "client_secret.json"
        authorize(secret, args.token, console=args.no_browser)
        print(f"Токен сохранён в {args.token}, канал: {channel_title(youtube_client(args.token))}")
        return

    cfg = load_config(args.config)

    if args.cmd == "process":
        from .effects import apply_effects

        effects = cfg["defaults"].get("effects", {})
        if args.job:
            effects = next(j for j in cfg["jobs"] if j["name"] == args.job)["effects"]
        apply_effects(args.input, args.output, effects)
        print(f"Готово: {args.output}")
        return

    jobs = cfg["jobs"]
    if args.job:
        jobs = [j for j in jobs if j["name"] in args.job]
        if not jobs:
            raise SystemExit(f"Нет задач с именами {args.job}")
    for job in jobs:
        try:
            run_job(job, dry_run=args.dry_run, source_override=args.source, keep_files=args.keep)
        except Exception as e:  # одна упавшая задача не должна валить остальные
            print(f"  ОШИБКА в задаче {job['name']}: {e}")
