"""Поиск цитаты с сохранением точных границ в исходном тексте."""
import re
import unicodedata
from difflib import SequenceMatcher

TWINS = str.maketrans(dict(zip("aceopxykmthb", "асеорхукмтнв")))
PROTECTED = {"не", "без", "нет", "ни", "только", "если", "кроме", "обязательно", "желательно", "приветствуется"}


def normalized(text: str) -> tuple[str, list[tuple[int, int]]]:
    characters, spans = [], []
    for index, char in enumerate(text):
        for c in unicodedata.normalize("NFKC", char).lower().replace("ё", "е").translate(TWINS):
            if c.isalnum():
                characters.append(c)
                spans.append((index, index + 1))
            elif characters and characters[-1] != " ":
                characters.append(" ")
                spans.append((index, index + 1))
    if characters and characters[-1] == " ":
        characters.pop()
        spans.pop()
    return "".join(characters), spans


def protected_values(text: str) -> tuple[set[str], set[str]]:
    numbers = {
        re.sub(r"\s", "", m).replace(",", ".")
        for m in re.findall(r"(?<!\w)[+-]?\d+(?:[ \u00a0]\d{3})*", unicodedata.normalize("NFKC", text).replace("−", "-"))
    }
    words = set(normalized(text)[0].split())
    return numbers, words & PROTECTED


def match_quote(quote: str, source: str) -> tuple[str, str]:
    position = source.find(quote) if quote else -1
    while position >= 0:
        end = position + len(quote)
        cuts_start = position and source[position - 1].isalnum() and quote[0].isalnum()
        cuts_start = cuts_start or (position and source[position - 1] in "+-−" and quote[0].isdigit())
        cuts_end = end < len(source) and source[end].isalnum() and quote[-1].isalnum()
        if not cuts_start and not cuts_end:
            return quote, "exact"
        position = source.find(quote, position + 1)
    needle, _ = normalized(quote)
    haystack, spans = normalized(source)
    if not needle or not haystack:
        raise ValueError("Пустая цитата или источник")
    protected = protected_values(quote)

    def candidate(start, end):
        if start < 0 or end > len(haystack) or start >= end:
            return None
        # Не подбираем обрезанные слова (в том числе части чисел и отрицаний).
        if start and haystack[start - 1].isalnum() and haystack[start].isalnum():
            return None
        if end < len(haystack) and haystack[end - 1].isalnum() and haystack[end].isalnum():
            return None
        begin = spans[start][0]
        if begin and source[begin - 1] in "+-−" and source[begin].isdigit():
            begin -= 1
        text = source[begin:spans[end - 1][1]]
        if protected_values(text) != protected:
            return None
        return text

    position = haystack.find(needle)
    while position >= 0:
        text = candidate(position, position + len(needle))
        if text is not None:
            return text, "normalized"
        position = haystack.find(needle, position + 1)
    if len(needle) < 8 or len(needle.split()) < 2:
        raise ValueError("Цитата не найдена в источнике")
    # Якоря совпадающих блоков ограничивают поиск небольшим числом окон.
    matcher = SequenceMatcher(None, needle, haystack, autojunk=False)
    starts = {max(0, b.b - b.a) for b in matcher.get_matching_blocks() if b.size}
    word_starts = [m.start() for m in re.finditer(r"\S+", haystack)]
    word_ends = [m.end() for m in re.finditer(r"\S+", haystack)]
    best_score, best = 0.85, None
    ambiguous = False
    for anchor in starts:
        for start in [p for p in word_starts if abs(p - anchor) <= max(5, len(needle) // 6)]:
            for end in [p for p in word_ends if abs(p - start - len(needle)) <= max(5, len(needle) // 6)]:
                text = candidate(start, end)
                if text is None:
                    continue
                score = SequenceMatcher(None, needle, haystack[start:end], autojunk=False).ratio()
                if score > best_score:
                    best_score, best, ambiguous = score, text, False
                elif best is not None and score == best_score and normalized(text)[0] != normalized(best)[0]:
                    ambiguous = True
    if best is None or ambiguous:
        raise ValueError("Нет однозначной близкой цитаты с теми же числами и условиями")
    return best, "fuzzy"
