"""Фильтр по теме: «Эльза» находит Frozen, «по» из Кунг-фу панды не ловит всё подряд."""
from reuploader.pipeline import rank
from reuploader.topics import detect, expand, matches


def v(title, tags=(), desc="", vid="x", views=1):
    return {"id": vid, "title": title, "tags": list(tags), "description": desc, "view_count": views}


def test_expand_knows_franchises_both_languages():
    t = expand("Эльза")
    assert {"эльза", "elsa", "frozen", "холодное сердце"} <= set(t)
    assert "анна" not in t and "anna" not in t          # короткие имена не раскрываются — слишком общие
    assert {"rick and morty", "рик и морти"} <= set(expand("Рик и Морти"))
    assert expand("  ") == []
    assert set(expand("моя тема, другая")) == {"моя тема", "другая"}   # неизвестное — как есть


def test_matches_title_tags_description_and_hashtags():
    terms = expand("холодное сердце")
    assert matches(v("Elsa builds an ice castle"), terms)
    assert matches(v("Funny moment", tags=["Frozen 2"]), terms)
    assert matches(v("Смешной момент", desc="#frozenedit #disney"), terms)
    assert not matches(v("SpongeBob and Patrick"), terms)
    rm = expand("рик и морти")
    assert matches(v("#rickandmorty best scene"), rm) and matches(v("Рик и Морти: лучшая серия"), rm)


def test_short_words_dont_match_everything():
    assert not matches(v("Прогулка по парку"), expand("кунг фу панда"))
    assert matches(v("Kung Fu Panda funny"), expand("кунг фу панда"))


def test_rank_filters_by_topic_and_detect_counts():
    vids = [v("Elsa sings", vid="a", views=5), v("Rick and Morty", vid="b", views=9), v("Frozen fail", vid="c", views=3)]
    got = rank(vids, sort_by="views", topic_terms=expand("эльза"))
    assert [x["id"] for x in got] == ["a", "c"]
    assert rank(vids, sort_by="views", topic_terms=()) and len(rank(vids, sort_by="views")) == 3
    found = dict(detect(vids))
    assert found["холодное сердце"] == 2 and found["рик и морти"] == 1
