import os
import re
from typing import List, Dict

from rank_bm25 import BM25Okapi

from config import DATA_DIR

TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
HEAD_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
PROGRAM_RE = re.compile(r"^\s*(\d+)\.\s*")


def _program_key(q: Dict[str, str]):
    m = PROGRAM_RE.match(q["title"])
    return (0, int(m.group(1))) if m else (1, 0)


def tokenize(text: str) -> List[str]:
    return TOKEN_RE.findall(text.lower())


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


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


class KnowledgeBase:
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self.docs: List[Dict[str, str]] = []
        self.bm25 = None
        self.questions: List[Dict[str, str]] = []
        self.answers_path: str | None = None
        self._load_docs()
        self._build_index()
        self._load_questions()

    def _load_docs(self):
        print(f"[KB] data_dir: {self.data_dir}", flush=True)
        print(f"[KB] exists: {os.path.isdir(self.data_dir)} cwd={os.getcwd()}", flush=True)
        if not os.path.isdir(self.data_dir):
            print(f"[KB] data dir not found: {self.data_dir}", flush=True)
            return
        try:
            print("[KB] dir:", sorted(os.listdir(self.data_dir))[:20], flush=True)
        except Exception as e:
            print("[KB] cannot list dir:", e, flush=True)
        for root, _, files in os.walk(self.data_dir):
            for name in files:
                if not name.lower().endswith((".txt", ".md")):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, self.data_dir)
                text = _read(path)
                for ch in _chunk(text):
                    self.docs.append({"source": rel, "text": ch})
        print(f"[KB] chunks: {len(self.docs)}", flush=True)

    def _build_index(self):
        if not self.docs:
            return
        corpus = [tokenize(d["text"]) for d in self.docs]
        self.bm25 = BM25Okapi(corpus)

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
            try:
                print("[KB] dir:", os.listdir(self.data_dir), flush=True)
            except Exception as e:
                print("[KB] cannot list dir:", e, flush=True)
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
        if not self.questions:
            print("[KB] no '## ' headings parsed; first 300 chars:", flush=True)
            print(repr(text[:300]), flush=True)

    def search(self, query: str, k: int = 6) -> List[Dict[str, str]]:
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
