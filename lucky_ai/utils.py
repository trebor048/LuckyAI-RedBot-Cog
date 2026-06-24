import hashlib
import re
import asyncio


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
    "- Use their own words against them — quote or reference what they actually said.\n"
    "- NO clichés (dumpster fire, trainwreck, yikes, main character, etc).\n"
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


def sanitize_output(text):
    if not text or not isinstance(text, str):
        return ""
    text = text.replace("@everyone", "@\u200beveryone")
    text = text.replace("@here", "@\u200bhere")
    text = re.sub(r"<#(\d{17,19})>", lambda m: f"<#\u200b{m.group(1)}>", text)
    text = re.sub(r"<@!?&?\d{17,19}>", lambda m: m.group(0).replace("@", "@\u200b"), text)
    return text


def generate_content_hash(content):
    return hashlib.sha256((content or "").encode()).hexdigest()[:16]


async def set_shared_api_key(client, provider: str, key: str) -> None:
    if key:
        await client.set_shared_api_tokens(provider, api_key=key)
        return
    remove = getattr(client, "remove_shared_api_tokens", None)
    if callable(remove):
        result = remove(provider, "api_key")
        if asyncio.iscoroutine(result):
            await result
        return
    await client.set_shared_api_tokens(provider, api_key="")


TLDR_SYSTEM_PROMPT = (
    "You are a sharp, observational summarizer. Given Discord chat messages, produce a TL;DR summary "
    "of the conversation's vibe and actions.\n\n"
    "Rules:\n"
    "- Summarize the overall vibe, key events, and general flow — do NOT quote or repeat messages verbatim\n"
    "- Describe what people talked about and did, not their exact words\n"
    "- Paraphrase freely — capture the essence, not a transcript\n"
    "- Keep it to ONE tight paragraph, 3-6 sentences\n"
    '- Start with "TL;DR:"\n'
    "- If the chat is chaotic, shitposty, or toxic, use that language honestly\n"
    "- Mention users by name when they drove a topic or moment\n"
    '- Do NOT describe how the conversation "ended" or what state things were "left in" — just cover what happened\n'
    "- No markdown, no bullet points, no formatting"
)

GREENTEXT_SYSTEM_PROMPT = (
    "You are a 4chan-style /b/tard summarizing Discord chat in greentext format.\n\n"
    "Rules:\n"
    '- Every line MUST start with ">"\n'
    '- Use 4chan slang, greentext conventions ("be me", "mfw", etc.)\n'
    "- Be brutally honest, crass, and funny — channel 4chan humor\n"
    "- Format like a classic greentext story: setup → events → punchline\n"
    '- Can include fake namefagging (e.g. ">be Eaglee, tripping balls")\n'
    "- Keep between 8 and 25 lines total\n"
    '- End with a classic closer like ">mfw ..." or similar\n'
    "- Reference actual events from the messages — don't just make everything up"
)

ASK_SYSTEM_PROMPT = (
    "You are a chaotic, sharp-tongued Discord regular who talks like a real person in a group chat. "
    "No preamble, no 'As an AI...', no formatting, no markdown, no emojis. Just raw text like you're "
    "a normal user typing in chat. Be sarcastic, witty, and match the room's energy. Roast bad takes, "
    "engage with good ones. Swear if it fits. Never be corporate or polite. Sound human."
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


def normalize_string_iterable(values):
    normalized = []
    seen = set()
    for value in values or []:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def format_messages_for_roast(messages, content_limit=200, max_total_chars=12000):
    if not messages:
        return "No messages available."
    lines = []
    sorted_msgs = sorted(messages, key=lambda x: x.get("timestamp") or 0)
    total_chars = 0
    for msg in sorted_msgs:
        author = msg.get("author_tag") or msg.get("author_id") or "Unknown"
        content = (msg.get("content") or "")[:content_limit]
        line = f"[{author}]: {content}"
        if lines and total_chars + len(line) + 1 > max_total_chars:
            lines.append("[...truncated]")
            break
        lines.append(line)
        total_chars += len(line) + 1
    return "\n".join(lines)

def format_messages_for_tldr(messages, content_limit=200, max_total_chars=12000):
    """Format messages for TLDR/greentext summarization."""
    if not messages:
        return ""
    sorted_msgs = []
    for m in messages:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        sorted_msgs.append(m)
    sorted_msgs.sort(key=lambda x: x.get("timestamp") or 0)
    lines = []
    total_chars = 0
    for m in sorted_msgs:
        name = m.get("author_tag") or m.get("author_id") or "Unknown"
        content = (m.get("content") or "").strip()[:content_limit]
        if not content:
            continue
        line = f"[{name}]: {content}"
        if lines and total_chars + len(line) + 1 > max_total_chars:
            lines.append("[...truncated]")
            break
        lines.append(line)
        total_chars += len(line) + 1
    return "\n".join(lines)
