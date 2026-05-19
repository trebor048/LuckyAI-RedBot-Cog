import time
import hashlib

from .ai_service import PROVIDER_ORDER, FALLBACK_DEFAULT_MODELS


DEFAULT_PERSONALITIES = {
    "clinical": "Cold, dispassionate dissection. Use precise psychological terminology. Make them feel like a case study in failure.",
    "disappointed": "You're not angry - you're profoundly disappointed. Channel a parent who expected better. Emphasis on wasted potential.",
    "sarcastic": "Vicious sarcasm and mockery. Dripping with irony. Every line should read like it has an eye-roll attached.",
    "blunt": "Brutal, unfiltered truth. Say what everyone thinks but is too polite to say. No sugar-coating. Just damage.",
    "analytical": "Systematically dismantle them. Expose contradictions, hypocrisy, self-deception. Use logic as a weapon.",
    "default": "All-purpose devastation. Attack character, choices, and insecurities. Balanced mix of cruelty and specificity.",
}

BASE_ROAST_PROMPT = (
    "You are a blunt, hateful roaster. Destroy them with brutal, unfiltered truth.\n"
    "Read their messages and attack what you find. Be personal. Be specific.\n"
    "\n"
    "RULES:\n"
    "- Expose their flaws, their pathetic choices, their wasted potential.\n"
    "- Make them feel genuinely hated. No mercy. No softening.\n"
    "- Short, punchy sentences. One kill shot per sentence.\n"
    "- Use their own words against them \u2014 quote or reference what they actually said.\n"
    "- NO clich\u00e9s (dumpster fire, trainwreck, yikes, main character, etc).\n"
    '- NO meta commentary ("I would roast you but..."). Just roast.\n'
    "- End with the meanest line. The one that stings longest.\n"
    "\n"
    "Keep it under 150 words. Every word must cut."
)

COLORS = {
    "ERROR": 0xFF6B6B,
    "SUCCESS": 0x51CF66,
    "INFO": 0x4DABF7,
    "WARNING": 0xFFD43B,
    "ROAST": 0xFF6B6B,
    "SETTINGS": 0x0099FF,
}


class AskError(Exception):
    def __init__(self, code, message, should_log=True, internal_detail=None):
        super().__init__(message)
        self.code = code
        self.should_log = should_log
        self.internal_detail = internal_detail

    @staticmethod
    def timeout():
        return AskError("TIMEOUT", "AI timed out. Try again.")

    @staticmethod
    def rate_limit(retry_after=None):
        msg = f"Rate limit hit. Wait ~{retry_after}s." if retry_after else "Rate limit hit. Try again later."
        return AskError("RATE_LIMIT", msg, should_log=False)

    @staticmethod
    def auth():
        return AskError("AUTH", "API key issue. Tell an admin.", internal_detail="AUTH_ERROR")

    @staticmethod
    def empty():
        return AskError("EMPTY", "Got nothing. Try rephrasing.", should_log=False)

    @staticmethod
    def context_too_long():
        return AskError("CONTEXT_TOO_LONG", "Conversation too long. Try again.")

    @staticmethod
    def unknown(detail=None):
        return AskError("UNKNOWN", "Something failed. Try again.", internal_detail=detail or "UNKNOWN_ERROR")


def sanitize_input(text):
    if not text or not isinstance(text, str):
        return ""
    sanitized = "".join(ch for ch in text if ch >= " " or ch in "\n\r\t")
    max_len = 50000
    return sanitized[:max_len] if len(sanitized) > max_len else sanitized


def sanitize_output(text):
    if not text or not isinstance(text, str):
        return ""
    text = text.replace("@everyone", "@\u200beveryone")
    text = text.replace("@here", "@\u200bhere")
    import re
    text = re.sub(r"<@!?&?\d{17,19}>", lambda m: m.group(0).replace("@", "@\u200b"), text)
    return text


def generate_content_hash(content):
    return hashlib.sha256((content or "").encode()).hexdigest()[:16]


def format_messages_for_ai(messages, author_tag_key="author_tag"):
    if not messages:
        return "No messages available."
    lines = []
    for msg in messages:
        author = msg.get(author_tag_key) or msg.get("author_id") or "Unknown"
        content = msg.get("content") or ""
        lines.append(f"{author}: {content}")
    return "\n".join(lines)


PROVIDER_LABELS = {
    "nvidia": "NVIDIA",
    "groq": "Groq",
    "moonshot": "Moonshot",
    "zai": "Z-AI",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
}


TLDR_SYSTEM_PROMPT = (
    "You are a sharp, observational summarizer. Given Discord chat messages, produce a TL;DR summary "
    "of the conversation's vibe and actions.\n\n"
    "Rules:\n"
    "- Summarize the overall vibe, key events, and general flow \u2014 do NOT quote or repeat messages verbatim\n"
    "- Describe what people talked about and did, not their exact words\n"
    "- Paraphrase freely \u2014 capture the essence, not a transcript\n"
    "- Keep it to ONE tight paragraph, 3-6 sentences\n"
    '- Start with "TL;DR:"\n'
    "- If the chat is chaotic, shitposty, or toxic, use that language honestly\n"
    "- Mention users by name when they drove a topic or moment\n"
    "- Do NOT describe how the conversation \"ended\" or what state things were \"left in\" \u2014 just cover what happened\n"
    "- No markdown, no bullet points, no formatting"
)

GREENTEXT_SYSTEM_PROMPT = (
    "You are a 4chan-style /b/tard summarizing Discord chat in greentext format.\n\n"
    "Rules:\n"
    '- Every line MUST start with ">"\n'
    '- Use 4chan slang, greentext conventions ("be me", "mfw", etc.)\n'
    "- Be brutally honest, crass, and funny \u2014 channel 4chan humor\n"
    "- Format like a classic greentext story: setup \u2192 events \u2192 punchline\n"
    '- Can include fake namefagging (e.g. ">be Eaglee, tripping balls")\n'
    "- Keep between 8 and 25 lines total\n"
    '- End with a classic closer like ">mfw ..." or similar\n'
    "- Reference actual events from the messages \u2014 don't just make everything up"
)

ASK_SYSTEM_PROMPT = (
    "You are a chaotic, witty Discord chat bot. The user asked a question \u2014 answer it directly without "
    "hesitation or preambles. Be sarcastic, casual, and match the energy of a group chat. Don't be a "
    "pushover or give boring corporate answers. If the question is edgy, engage with it. If it's dumb, "
    "roast it gently. Reference the chat context if relevant."
)

DEBATE_SYSTEM_PROMPT = (
    "You are a brutally honest debate judge in a Discord chat.\n\n"
    "TASK:\n"
    "1. Identify the topic/question being discussed\n"
    "2. Identify 2 distinct sides based on the messages\n"
    "3. Summarize each side's argument (2-3 sentences each)\n"
    "4. Judge who wins and WHY they're right\n"
    "5. Roast why the loser is wrong/weak\n\n"
    "SCORING RUBRIC:\n"
    "- Logic (1-5): Sound reasoning vs. fallacies\n"
    "- Evidence (1-3): References facts vs. vibes\n"
    "- Clarity (1-2): Clear argument vs. rambling\n\n"
    "FORMAT:\n"
    "Topic: [one line]\n"
    "Side A: @[username] \u2014\u2014 [argument summary]\n"
    "Side B: @[username] \u2014\u2014 [argument summary]\n"
    "Winner: A or B\n"
    "Verdict: [2-3 sentences on why winner is right]\n"
    "Loser Take: [1-2 sentences roast of bad argument]\n"
    "Score: A: [logic+evidence+clarity]/10 | B: [total]/10\n\n"
    "RULES:\n"
    '- Be direct. Someone is WRONG. Say it.\n'
    '- No "both sides have valid points" \u2014 pick a winner\n'
    "- Witty, roast-style insults for the loser\n"
    "- If consensus reached, judge the quality of getting there"
)

HOT_TAKE_PROMPT = """You are a brutally honest, funny observer of online chat rooms.
Your job is to write a single "hot take" that roasts the GENERAL VIBE of this conversation.

Rules:
- NEVER target a specific user by name
- Roast the room/conversation/energy
- Be edgy, funny, bantery — not cringe
- Self-aware humor is great
- Can reference topics discussed if funny
- Keep it to 2-4 sentences max
- NO meta-commentary — just deliver the take
- Sound like you're actually in the chat, not a robot describing it

Conversation:
{conversation}

Write the hot take:"""

DEFAULT_SETTINGS = {
    "model": "nvidia/qwen/qwen3.5-122b-a10b",
    "temperature": 1.0,
    "max_tokens": 4096,
    "top_p": 0.9,
    "top_k": 40,
    "frequency_penalty": 0.4,
    "presence_penalty": 0.2,
    "promptKey": "blunt",
    "messageFetchMode": "random",
    "randomMode": False,
    "enabled": True,
    "admin_role": None,
    "typing_enabled": True,
    "sync_channels": [],
}


def format_messages_for_roast(messages):
    if not messages:
        return "No messages available."
    lines = []
    for msg in messages:
        author = msg.get("author_tag") or msg.get("author_id") or "Unknown"
        content = msg.get("content") or ""
        lines.append(f"[{author}]: {content}")
    return "\n".join(lines)


def format_messages_for_tldr(messages, style="normal"):
    """Format messages for TLDR/greentext summarization."""
    if not messages:
        return ""
    sorted_msgs = sorted(
        [m for m in messages if m.get("content", "").strip()],
        key=lambda x: x.get("timestamp", 0)
    )
    lines = []
    for m in sorted_msgs:
        name = m.get("author_tag") or m.get("author_id") or "Unknown"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{name}]: {content}")
    return "\n".join(lines)


def parse_debate_response(text):
    import re
    def extract(label):
        regex = re.compile(rf"{label}:\s*(.+)", re.IGNORECASE | re.DOTALL)
        match = regex.search(text)
        return match.group(1).strip() if match else None
    
    result = {
        "topic": extract("Topic"),
        "sideA": extract("Side A"),
        "sideB": extract("Side B"),
        "winner": extract("Winner"),
        "verdict": extract("Verdict"),
        "loserTake": extract("Loser Take"),
        "score": extract("Score"),
    }
    return result


def make_cooldown_message(remaining_sec, reset_at=None):
    remaining_ms = remaining_sec * 1000
    if remaining_ms <= 3600000:
        if reset_at:
            utc = reset_at.strftime("%H:%M") if hasattr(reset_at, "strftime") else str(reset_at)
            return f"Cooldown active - resets at {utc} UTC"
        return f"Cooldown active - wait {remaining_sec}s"
    hours = max(1, remaining_sec // 3600)
    return f"Cooldown active - resets in ~{hours} hours"


def build_debate_context(messages, max_messages=50, max_content_len=300):
    """
    Build a formatted context string from chat messages for debate analysis.
    
    Args:
        messages: List of message dicts with author_tag/content/timestamp
        max_messages: Maximum number of messages to include
        max_content_len: Maximum characters per message content
    
    Returns:
        Formatted string with author: content lines
    """
    if not messages:
        return ""
    
    # Sort by timestamp, take most recent
    sorted_msgs = sorted(
        [m for m in messages if m.get("content", "").strip()],
        key=lambda x: x.get("timestamp", 0)
    )
    
    lines = []
    for m in sorted_msgs[-max_messages:]:
        name = m.get("author_tag") or m.get("author_id", "Someone")
        content = (m.get("content") or "").strip()[:max_content_len]
        if content:
            lines.append(f"{name}: {content}")
    
    return "\n".join(lines)
