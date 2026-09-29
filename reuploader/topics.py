"""Фильтр «про что ролик»: пользователь пишет тему как удобно («Эльза», «холодное сердце»,
«Рик и Морти»), бот берёт только ролики, у которых тема есть в названии, тегах или описании.

Несколько вариантов — через запятую («эльза, frozen, анна»). Для известных мультфильмов
английские и русские варианты названий и имён героев добавляются сами (SYNONYMS).
"""
import re

# ключ — любой из вариантов (в нижнем регистре); значение — все варианты этой темы
_GROUPS = [
    ["холодное сердце", "frozen", "эльза", "elsa", "анна", "anna", "олаф", "olaf", "кристофф", "kristoff", "arendelle",
     "эренделл"],
    ["рик и морти", "rick and morty", "rick & morty", "rickandmorty", "рик", "морти", "rick sanchez", "morty smith"],
    ["губка боб", "губка боб квадратные штаны", "spongebob", "sponge bob", "спанч боб", "патрик", "patrick star",
     "сквидвард", "squidward", "мистер крабс", "mr krabs", "mr. krabs", "планктон", "plankton", "сэнди", "sandy cheeks",
     "bikini bottom", "бикини боттом"],
    ["том и джерри", "tom and jerry", "tom & jerry", "tomandjerry"],
    ["шрек", "shrek", "осёл", "фиона", "fiona"],
    ["симпсоны", "the simpsons", "simpsons", "гомер", "homer simpson", "барт", "bart simpson"],
    ["маша и медведь", "masha and the bear", "mashaandthebear"],
    ["свинка пеппа", "peppa pig", "peppa"],
    ["смешарики", "smeshariki", "kikoriki"],
    ["фиксики", "fixiki"],
    ["барбоскины", "barboskiny"],
    ["гравити фолз", "gravity falls", "диппер", "dipper", "мейбл", "mabel", "билл шифр", "bill cipher"],
    ["миньоны", "minions", "minion", "гадкий я", "despicable me", "грю", "gru"],
    ["тачки", "cars", "молния маккуин", "lightning mcqueen", "мэтр", "mater"],
    ["история игрушек", "toy story", "вуди", "woody", "базз", "buzz lightyear"],
    ["моана", "моана", "moana", "мауи", "maui"],
    ["кунг-фу панда", "кунг фу панда", "kung fu panda", "по", "po"],
    ["головоломка", "inside out", "радость", "joy", "печаль", "sadness", "тревожность", "anxiety"],
    ["гта", "gta", "gta 5", "gta v", "grand theft auto"],
    ["майнкрафт", "minecraft"],
    ["роблокс", "roblox"],
    ["скибиди", "skibidi", "skibidi toilet"],
    ["удивительный цифровой цирк", "amazing digital circus", "digital circus", "помни", "pomni"],
    ["леди баг", "miraculous", "ladybug", "супер-кот", "cat noir"],
    ["наруто", "naruto"],
    ["ван пис", "one piece", "луффи", "luffy"],
]
SYNONYMS = {}
for g in _GROUPS:
    for name in g:
        SYNONYMS.setdefault(name, set()).update(g)

# слишком короткие/общие имена ищем только целым словом, а сами по себе в тему не раскрываем
_AMBIGUOUS = {"по", "po", "рик", "анна", "anna", "joy", "cars", "радость", "печаль", "барт", "maui", "мэтр", "mater"}


def _norm(s):
    s = (s or "").lower().replace("ё", "е").replace("#", " ").replace("_", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^\w&\s]", " ", s)).strip()


def expand(topic):
    """«эльза, анна» -> отсортированный список вариантов для поиска (с синонимами)."""
    terms = set()
    for part in re.split(r"[,;\n]|\bили\b|\bor\b", topic or "", flags=re.I):
        p = _norm(part)
        if not p:
            continue
        terms.add(p)
        syn = SYNONYMS.get(p)
        if syn and p not in _AMBIGUOUS:
            terms.update(_norm(x) for x in syn if x not in _AMBIGUOUS)
    return sorted(t for t in terms if t)


def detect(videos, top=12):
    """Какие известные темы встречаются в роликах: [(название, сколько роликов)], по убыванию."""
    found = []
    for g in _GROUPS:
        terms = [_norm(x) for x in g if x not in _AMBIGUOUS]
        n = sum(1 for v in videos if matches(v, terms))
        if n:
            found.append((g[0], n))
    return sorted(found, key=lambda x: -x[1])[:top]


def matches(video, terms):
    """Есть ли хоть один вариант темы в названии, тегах или описании ролика."""
    if not terms:
        return True
    text = " " + _norm(" ".join([video.get("title") or "", video.get("description") or "",
                                 " ".join(video.get("tags") or [])])) + " "
    squashed = text.replace(" ", "")
    for t in terms:
        if f" {t} " in text:
            return True
        if len(t) >= 6 and " " not in t and t in squashed:   # хэштеги: #frozenedit, #rickandmorty
            return True
        if " " in t and t.replace(" ", "") in squashed:
            return True
    return False
