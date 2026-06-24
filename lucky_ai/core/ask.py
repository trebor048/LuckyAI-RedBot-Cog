from typing import List, Optional, Tuple


ASK_CONTEXT_MAX_CHARS = 60000
ASK_CONTEXT_CONTENT_LIMIT = 250


def parse_ask_flags(question: Optional[str]) -> Tuple[Optional[str], bool, Optional[int]]:
    """Parse inline flags for `lask` and return cleaned question, debug mode, and optional context count."""
    tokens = question.strip().split() if question and question.strip() else []
    debug_context = False
    context_count = None

    remaining = []
    for token in tokens:
        if token.lower() == "--with-context":
            debug_context = True
            continue
        if context_count is None and not remaining and token.isdigit():
            context_count = int(token)
            continue
        remaining.append(token)

    cleaned = " ".join(remaining).strip() or None
    return cleaned, debug_context, context_count


def build_ask_context(
    messages: List[dict],
    question: Optional[str],
    bot_user_id: Optional[str],
    *,
    explicit_context: bool,
    max_chars: int = ASK_CONTEXT_MAX_CHARS,
) -> List[str]:
    """Build chronological ask context while preserving the most recent messages within the prompt budget."""
    ordered = sorted(messages or [], key=lambda message: message.get("timestamp") or 0)
    question_lower = (question or "").lower()
    question_words = {word for word in question_lower.split() if len(word) > 3}
    lines = []

    for message in ordered:
        if bot_user_id and str(message.get("author_id")) == str(bot_user_id):
            continue
        content = (message.get("content") or "").strip()
        if not content:
            continue
        if not explicit_context and question_words:
            content_words = {word for word in content.lower().split() if len(word) > 3}
            if not question_words.intersection(content_words):
                continue
        name = message.get("author_tag") or message.get("author_id") or "Someone"
        lines.append(f"{name}: {content[:ASK_CONTEXT_CONTENT_LIMIT]}")

    if not explicit_context:
        return lines[-5:]

    selected = []
    total_chars = 0
    for line in reversed(lines):
        added_chars = len(line) + (1 if selected else 0)
        if selected and total_chars + added_chars > max_chars:
            break
        selected.append(line)
        total_chars += added_chars
    return list(reversed(selected))


def find_image_attachments(attachments: List[object]) -> List[object]:
    """Return attachment objects that look like images based on content-type or extension."""
    image_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    result = []
    for attach in (attachments or []):
        content_type = getattr(attach, "content_type", None)
        filename = getattr(attach, "filename", None)
        if (content_type and str(content_type).startswith("image/")) or (
            filename and str(filename).lower().endswith(image_exts)
        ):
            result.append(attach)
    return result
