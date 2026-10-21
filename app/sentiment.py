"""Headline tagging. FreeLLMAPI first; a small Claude model only if it fails."""
from sqlalchemy import select

from .config import get_settings
from .db import NewsItem, log, session
from .llm import LLMError, claude_chat, extract_json, free_chat

SYSTEM = ("You tag financial headlines for a forex and metals desk. Reply with a JSON array only, no other text. "
          "XAUUSD is gold and XAGUSD is silver, both priced in US dollars. EURUSD rising means the euro strengthens against the dollar.")


def _prompt(titles: list[str], symbols: list[str]) -> str:
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    example = ",".join(f'"{s}":0' for s in symbols)
    return (f"Symbols: {', '.join(symbols)}\n"
            "For each headline give its likely effect on each symbol's price: 1 bullish, -1 bearish, 0 none or unclear. "
            f'Reply like [{{"i":0,{example}}}, ...] with one object per headline.\n\nHeadlines:\n{lines}')


def _valid(obj, n: int, symbols: list[str]) -> dict[int, dict]:
    out = {}
    if isinstance(obj, dict):  # some models wrap the list: {"tags": [...]} or return a single object
        inner = next((v for v in obj.values() if isinstance(v, list)), None)
        obj = inner if inner is not None else [obj]
    if not isinstance(obj, list):
        raise LLMError("tag reply was not a list")
    for row in obj:
        if not isinstance(row, dict) or not isinstance(row.get("i"), int) or not (0 <= row["i"] < n):
            continue
        tags = {}
        for s in symbols:
            v = row.get(s, 0)
            tags[s] = v if v in (-1, 0, 1) else 0
        out[row["i"]] = tags
    if not out:
        raise LLMError("tag reply had no usable rows")
    return out


def tag_untagged(limit: int = 45, batch: int = 15) -> int:
    symbols = get_settings().symbol_list
    done = 0
    with session() as s:
        items = list(s.scalars(select(NewsItem).where(NewsItem.tags.is_(None)).order_by(NewsItem.published.desc()).limit(limit)))
        for k in range(0, len(items), batch):
            chunk = items[k:k + batch]
            titles = [i.title for i in chunk]
            prompt = _prompt(titles, symbols)
            by = "freellmapi"
            try:
                tags = _valid(extract_json(free_chat(SYSTEM, prompt)), len(chunk), symbols)
            except LLMError as e:
                log(f"FreeLLMAPI tagging failed ({e}); trying Claude fallback", "warn")
                try:
                    by = "claude"
                    tags = _valid(extract_json(claude_chat(SYSTEM, prompt, model=get_settings().claude_fallback_model)), len(chunk), symbols)
                except LLMError as e2:
                    log(f"Headline tagging skipped: {e2}", "warn")
                    break
            for idx, t in tags.items():
                chunk[idx].tags = t
                chunk[idx].tagged_by = by
                done += 1
            s.commit()
    return done
