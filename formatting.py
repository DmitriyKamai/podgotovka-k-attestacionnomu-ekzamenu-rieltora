import html
import re

CODE_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
BOLD2_RE = re.compile(r"__([^_\n]+?)__")
ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
HEAD_RE = re.compile(r"(?m)^#{1,6}\s*(.+?)\s*$")
BULLET_RE = re.compile(r"(?m)^\s*[-*]\s+")
TAG_RE = re.compile(r"<(/?)(b|i|code|a)(\s[^>]*)?>")


def md_to_html(text: str) -> str:
    """Превращает Markdown от модели в безопасный HTML для Telegram."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = html.escape(text, quote=False)

    codes: list[str] = []

    def _code(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00CODE{len(codes) - 1}\x00"

    text = CODE_RE.sub(_code, text)

    def _link(m: re.Match) -> str:
        return f'<a href="{m.group(2)}">{m.group(1)}</a>'

    text = LINK_RE.sub(_link, text)
    text = BOLD_RE.sub(r"<b>\1</b>", text)
    text = BOLD2_RE.sub(r"<b>\1</b>", text)
    text = BULLET_RE.sub("• ", text)
    text = ITALIC_RE.sub(r"<i>\1</i>", text)
    text = HEAD_RE.sub(r"<b>\1</b>", text)

    for i, code in enumerate(codes):
        text = text.replace(
            f"\x00CODE{i}\x00", f"<code>{html.escape(code, quote=False)}</code>"
        )

    # Пустая строка между смысловыми разделами 2)–4), даже если модель её забыла.
    text = re.sub(r"\n(?=[2-9]\)\s)", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _open_tags(chunk: str) -> list[tuple[str, str]]:
    stack: list[tuple[str, str]] = []
    for m in TAG_RE.finditer(chunk):
        closing, name = m.group(1), m.group(2)
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
        else:
            stack.append((name, m.group(0)))
    return stack


def _safe_cut(line: str, limit: int) -> int:
    in_tag = False
    last_space = -1
    for i, ch in enumerate(line[:limit]):
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif ch == " " and not in_tag:
            last_space = i
    return last_space if last_space > limit // 2 else limit


def split_html(text: str, limit: int = 3800) -> list[str]:
    """Режет HTML на части, не разрывая теги и закрывая их в каждом сообщении."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        while len(line) > limit:
            cut = _safe_cut(line, limit)
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        buf = line
    if buf:
        chunks.append(buf)

    out: list[str] = []
    carry: list[tuple[str, str]] = []
    for chunk in chunks:
        if carry:
            chunk = "".join(tag for _, tag in carry) + chunk
        opens = _open_tags(chunk)
        if opens:
            chunk += "".join(f"</{name}>" for name, _ in reversed(opens))
        out.append(chunk)
        carry = opens
    return out
