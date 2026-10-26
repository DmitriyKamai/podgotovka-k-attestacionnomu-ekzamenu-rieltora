import os
import re
from typing import Dict, List, Tuple

from rank_bm25 import BM25Okapi

from config import DATA_DIR

TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
HEAD_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
PROGRAM_RE = re.compile(r"^\s*(\d+)\.\s*")
ARTICLE_RE = re.compile(
    r"(?m)^[ \t]*Статья[ \t\r\n]+(\d+)"
    r"(?:"
    r"[ \t]*-[ \t]*(\d+)[ \t]*\."
    r"|[ \t\r\n]+(\d{1,2})[ \t]*\n?[ \t]*\."
    r"|[ \t]*\."
    r")"
)
PARA_RE = re.compile(r"(?m)^\s*(\d{1,3})\.\s+")
CITED_ART_RE = re.compile(
    r"(?:ст\.|стать[яиюей]{1,3}|статьёй)\s*(\d+(?:-\d+)?)", re.IGNORECASE
)

STOP = {
    "и", "в", "во", "на", "по", "с", "со", "как", "что", "это", "для", "или",
    "не", "а", "о", "об", "от", "до", "при", "из", "за", "то", "же", "ли", "бы",
    "the", "of", "and", "к", "ко", "у", "над", "под", "про", "чем", "кто",
    "какой", "какие", "каких", "каком", "какая", "который", "которые",
    "если", "где", "когда", "нужно", "нужны", "можно", "ли", "есть",
}

# грубый стеммер: убираем частые русские окончания, чтобы «дарения» ≈ «дарение»
SUFFIXES = [
    "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ией",
    "ей", "ой", "ый", "ий", "ая", "яя", "ые", "ие", "ов", "ев", "ам", "ям",
    "ах", "ях", "ом", "ем", "а", "я", "ы", "и", "у", "ю", "е", "о", "ь",
]


def _stem(token: str) -> str:
    if len(token) <= 4:
        return token
    for suf in SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[:-len(suf)]
    return token


def _program_key(q: Dict[str, str]):
    m = PROGRAM_RE.match(q["title"])
    return (0, int(m.group(1))) if m else (1, 0)


def tokenize(text: str) -> List[str]:
    return [_stem(t) for t in TOKEN_RE.findall(text.lower())]


def _terms(text: str) -> set:
    return {t for t in tokenize(text) if t not in STOP and len(t) > 1}


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    for ch in ("\xa0", "\u202f", "\u2009", "\u2007"):
        text = text.replace(ch, " ")
    return text


def _chunk(text: str, size: int = 1100, overlap: int = 150) -> List[str]:
    text = re.sub(r"\r\n?", "\n", text)
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in parts:
        if len(buf) + len(p) + 2 <= size:
            buf = (buf + "\n\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= size:
                buf = p
            else:
                for i in range(0, len(p), size - overlap):
                    chunks.append(p[i:i + size])
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def _split_articles(text: str) -> Dict[str, str]:
    matches = list(ARTICLE_RE.finditer(text))
    out: Dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start():end].strip()
        suffix = m.group(2) or m.group(3)
        num = f"{m.group(1)}-{suffix}" if suffix else m.group(1)
        out.setdefault(num, body[:9000])
    return out


def _split_paragraphs(text: str) -> Dict[str, str]:
    matches = [m for m in PARA_RE.finditer(text) if 0 < int(m.group(1)) <= 400]
    out: Dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start():end].strip()
        out.setdefault(m.group(1), body[:5000])
    return out


class KnowledgeBase:
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self.docs: List[Dict] = []
        self.bm25 = None
        self.questions: List[Dict[str, str]] = []
        self.answers_path: str | None = None
        self.raw: Dict[str, str] = {}
        self.articles: Dict[str, Dict[str, str]] = {}
        self.paragraphs: Dict[str, Dict[str, str]] = {}
        self._by_key: Dict[Tuple[str, int], Dict] = {}
        self._title_terms: List[set] = []
        self._head_terms: List[set] = []
        self._load_questions()
        self._load_docs()
        self._build_index()

    # ---------- загрузка ----------

    def _find_answers(self):
        candidates = [
            os.path.join(self.data_dir, "answers.md"),
            os.path.join(self.data_dir, "Ответы_частые_вопросы.md"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        if os.path.isdir(self.data_dir):
            for root, _, files in os.walk(self.data_dir):
                for name in files:
                    if not name.lower().endswith(".md"):
                        continue
                    p = os.path.join(root, name)
                    try:
                        t = _read(p)
                    except Exception:
                        continue
                    if "## " in t:
                        return p
        return None

    def _load_questions(self):
        path = self._find_answers()
        if not path:
            print(f"[KB] answers file not found in: {self.data_dir}", flush=True)
            print(f"[KB] cwd={os.getcwd()}", flush=True)
            return
        print(f"[KB] answers: {path}", flush=True)
        self.answers_path = path
        text = _read(path)
        marker = "# ПРОВЕРЯЕМЫЕ ПУНКТЫ"
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
        matches = list(HEAD_RE.finditer(text))
        for i, m in enumerate(matches):
            title = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip().strip("-").strip()
            if body:
                self.questions.append({"title": title, "body": body})
        self.questions.sort(key=_program_key)
        print("[KB] order: program numbers (unnumbered at end)", flush=True)
        print(f"[KB] questions: {len(self.questions)}", flush=True)

    def _load_docs(self):
        print(f"[KB] data_dir: {self.data_dir}", flush=True)
        print(f"[KB] exists: {os.path.isdir(self.data_dir)} cwd={os.getcwd()}", flush=True)
        if not os.path.isdir(self.data_dir):
            print(f"[KB] data dir not found: {self.data_dir}", flush=True)
            return
        answers_abs = os.path.abspath(self.answers_path) if self.answers_path else None
        for root, _, files in os.walk(self.data_dir):
            for name in files:
                if not name.lower().endswith((".txt", ".md")):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, self.data_dir)
                text = _read(path)
                self.raw[rel] = text
                kind = "answers" if answers_abs and os.path.abspath(path) == answers_abs else "npa"
                if kind == "npa" and rel.lower().endswith(".txt"):
                    if ARTICLE_RE.search(text):
                        self.articles[rel] = _split_articles(text)
                    else:
                        self.paragraphs[rel] = _split_paragraphs(text)
                for i, ch in enumerate(_chunk(text)):
                    doc = {"source": rel, "text": ch, "idx": i, "kind": kind}
                    self.docs.append(doc)
                    self._by_key[(rel, i)] = doc
        print(f"[KB] chunks: {len(self.docs)}", flush=True)
        print(
            f"[KB] articles: {sum(len(v) for v in self.articles.values())} "
            f"in {len(self.articles)} files; "
            f"paragraphs: {sum(len(v) for v in self.paragraphs.values())} "
            f"in {len(self.paragraphs)} files",
            flush=True,
        )

    def _build_index(self):
        if self.docs:
            self.bm25 = BM25Okapi([tokenize(d["text"]) for d in self.docs])
        if self.questions:
            self._title_terms = [_terms(q["title"]) for q in self.questions]
            self._head_terms = [_terms(q["body"][:900]) for q in self.questions]

    # ---------- поиск ----------

    def search(self, query: str, k: int = 8) -> List[Dict]:
        if not self.bm25 or not self.docs:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked[:k]:
            if scores[i] > 0:
                out.append(self.docs[i])
        if not out:
            out = [self.docs[i] for i in ranked[:k]]
        return out

    def match_question(self, query: str) -> Dict[str, str] | None:
        """Находит вопрос Программы, к которому относится запрос."""
        qt = _terms(query)
        if not qt or not self.questions:
            return None
        scored = []
        for i in range(len(self.questions)):
            t = self._title_terms[i]
            all_terms = t | self._head_terms[i]
            common = qt & all_terms
            if len(common) < 2:
                continue
            title_ratio = len(qt & t) / len(qt)
            all_ratio = len(common) / len(qt)
            scored.append((all_ratio + 0.6 * title_ratio, i))
        if not scored:
            return None
        scored.sort(reverse=True)
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        qt_title = len(qt & self._title_terms[best]) / len(qt)
        if qt_title < 0.2 and best_score < 0.6:
            return None
        if best_score < second + 0.02 and best_score < 0.75:
            return None
        q = dict(self.questions[best])
        m = PROGRAM_RE.match(q["title"])
        q["num"] = m.group(1) if m else ""
        return q

    def _source_rank(self, hint: str):
        hint_terms = _terms(hint)

        def key(src: str):
            name_terms = _terms(src.replace("+", " "))
            return -len(name_terms & hint_terms)

        return key

    def get_article(self, num: str, hint: str = "", limit: int = 2) -> List[Tuple[str, str]]:
        """Возвращает полный текст статьи по номеру, сначала из наиболее релевантного НПА."""
        found: List[Tuple[str, str]] = []
        sources = sorted(self.articles, key=self._source_rank(hint))
        for src in sources:
            text = self.articles[src].get(num)
            if text:
                found.append((f"{src}, ст. {num}", text))
            if len(found) >= limit:
                break
        return found

    def get_paragraph(self, num: str, hint: str = "", limit: int = 2) -> List[Tuple[str, str]]:
        found: List[Tuple[str, str]] = []
        sources = sorted(self.paragraphs, key=self._source_rank(hint))
        for src in sources:
            text = self.paragraphs[src].get(num)
            if text:
                found.append((f"{src}, п. {num}", text))
            if len(found) >= limit:
                break
        return found

    def check(self) -> List[str]:
        """Проверка полноты базы ответов. Возвращает список замечаний."""
        issues: List[str] = []
        if not self.questions:
            return ["Вопросы не загружены"]
        nums: Dict[str, int] = {}
        for q in self.questions:
            title, body = q["title"], q["body"].strip()
            m = PROGRAM_RE.match(title)
            if m:
                nums[m.group(1)] = nums.get(m.group(1), 0) + 1
            if not body:
                issues.append(f"пустой ответ: {title[:70]}")
                continue
            if len(body) < 400:
                issues.append(f"короткий ответ ({len(body)}): {title[:70]}")
            if not re.search(r"ст\.|стать|пункт|п\.\s*\d|Закон|Кодекс|Инструкц", body, re.I):
                issues.append(f"нет ссылки на норму: {title[:70]}")
        for num, count in nums.items():
            if count > 1:
                issues.append(f"дубль номера Программы {num} ({count})")
        for q in self.questions:
            cited = set(CITED_ART_RE.findall(q["title"] + " " + q["body"]))
            for num in cited:
                if not self.get_article(num, hint=q["title"], limit=1):
                    issues.append(f"нет текста ст. {num} — {q['title'][:60]}")
        return issues

    def _expand(self, chunks: List[Dict], radius: int = 1) -> List[Dict]:
        out: List[Dict] = []
        seen = set()
        for c in chunks:
            for i in range(max(0, c["idx"] - radius), c["idx"] + radius + 1):
                key = (c["source"], i)
                if key in seen or key not in self._by_key:
                    continue
                seen.add(key)
                out.append(self._by_key[key])
        return out

    def build_context(self, query: str, budget: int = 18000) -> str:
        """Эталонный ответ по Программе + полные статьи + окрестности найденных фрагментов."""
        parts: List[str] = []
        used = 0
        seen_blocks = set()

        def add(label: str, text: str):
            nonlocal used
            text = text.strip()
            if not text:
                return
            key = (label, text[:80])
            if key in seen_blocks:
                return
            block = f"[{label}]\n{text}"
            if used + len(block) > budget:
                return
            seen_blocks.add(key)
            parts.append(block)
            used += len(block) + 5

        matched = self.match_question(query)
        hint = (matched["title"] + " " + query) if matched else query
        if matched:
            label = "ЭТАЛОННЫЙ ОТВЕТ ПО ПРОГРАММЕ"
            if matched.get("num"):
                label += f" — п. {matched['num']}"
            add(f"{label}: {matched['title']}", matched["body"])

        # статьи, на которые ссылается эталон и сам запрос
        article_nums: List[str] = []
        if matched:
            article_nums += CITED_ART_RE.findall(matched["body"])
        article_nums += CITED_ART_RE.findall(query)
        for num in dict.fromkeys(article_nums):
            for label, text in self.get_article(num, hint=hint):
                add(label, text)

        # фрагменты НПА с соседями (при найденном эталоне ответы не дублируем)
        chunks = self.search(query, k=8)
        for d in self._expand(chunks, radius=1):
            if matched and d["kind"] == "answers":
                continue
            add(d["source"], d["text"])

        if not parts:
            for d in self.search(query, k=6):
                add(d["source"], d["text"])
        return "\n\n---\n\n".join(parts)
