import argparse
import shutil
from pathlib import Path

from .config import load_config
from .state import History

WORK_DIR = Path("work")


def _fmt_views(n):
    return "?" if n is None else f"{n:,}".replace(",", " ")


def _describe(v):
    parts = [f"{_fmt_views(v['view_count'])} просм."]
    if v.get("published"):
        parts.append(f"вышел {v['published'][:16].replace('T', ' ')} UTC, {v['age_days']:.0f} дн. назад")
    if v.get("duration"):
        parts.append(f"{int(v['duration']) // 60}:{int(v['duration']) % 60:02d}")
    if v.get("views_per_day") is not None:
        parts.append(f"~{_fmt_views(v['views_per_day'])}/день")
    return ", ".join(parts)


def run_job(job, dry_run=False, source_override=None, keep_files=False):
    from .pipeline import build_text, pick, prepare
    from .source import enrich
    from .uploader import channel_title, upload, youtube_client

    print(f"== Задача {job['name']} ==")
    history = History(job["name"])
    sources = [source_override] if source_override else job["sources"]
    print("  источники: " + ", ".join(sources))
    youtube = None
    if not dry_run or Path(job["token"]).exists():
        youtube = youtube_client(job["token"])
        print(f"  целевой канал: {channel_title(youtube)}")
    picked = pick(
        sources, history, count=job["per_run"], scan_limit=job["scan_limit"],
        min_views=job["min_views"], strategy=job.get("strategy", "top"),
        sort_by=job.get("sort_by", "views"), max_age_days=job.get("max_age_days", 0),
        min_duration=job.get("min_duration", 0), max_duration=job.get("max_duration", 0),
        enrich=(lambda vs: enrich(vs, youtube)) if youtube else None,
    )
    if not picked:
        print("  нечего заливать — всё подходящее уже перезалито")
        return

    work = WORK_DIR / job["name"]
    for v in picked:
        print(f"  -> {v['title']!r} ({_describe(v)}) {v['url']}")
        print("    скачивание и уникализация...")
        src, out, meta = prepare(v["url"], work, job["effects"])
        title, description, tags = build_text(meta, job)

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
    ls.add_argument("--sort", choices=["views", "per_day", "new"], default="views",
                    help="views — всего просмотров, per_day — просмотров в день, new — новые")
    ls.add_argument("--days", type=int, default=0, help="только ролики не старше N дней")
    ls.add_argument("--token", help="токен канала: даты через YouTube API (быстро и для всех роликов)")

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
        from .pipeline import rank
        from .source import enrich, list_shorts

        videos = list_shorts(args.channel, args.scan_limit)
        if args.token:
            from .uploader import youtube_client

            enrich(videos, youtube_client(args.token))
        else:
            print("(даты через yt-dlp — медленно, только первые 40 роликов; с --token быстрее)")
            enrich(videos, None, limit=40)
        if args.sort == "new":
            videos = [v for v in videos if not args.days or (v.get("age_days") or 1e9) <= args.days]
            videos.sort(key=lambda v: v.get("published") or "", reverse=True)
        else:
            videos = rank(videos, sort_by=args.sort, max_age_days=args.days)
        for i, v in enumerate(videos[: args.n], 1):
            print(f"{i:>3}. {v['url']}  {v['title']}\n     {_describe(v)}")
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
