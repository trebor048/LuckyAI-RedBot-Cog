import difflib
from typing import Dict, Iterable, List, Optional


def find_suggestions(
    typed: str,
    candidates: Dict[str, str],
    *,
    limit: int = 2,
    cutoff: float = 0.6,
) -> List[str]:
    """Return unique display values for the closest normalized candidate names."""
    normalized = " ".join((typed or "").lower().split())
    if not normalized:
        return []
    matches = difflib.get_close_matches(normalized, candidates, n=max(limit * 3, limit), cutoff=cutoff)
    suggestions = []
    for match in matches:
        display = candidates[match]
        if display not in suggestions:
            suggestions.append(display)
        if len(suggestions) >= limit:
            break
    return suggestions


def suggest_choice(typed: str, choices: Iterable[str], *, cutoff: float = 0.5) -> Optional[str]:
    """Return the closest valid choice for a mistyped command argument."""
    normalized_choices = [choice.lower() for choice in choices]
    matches = difflib.get_close_matches((typed or "").lower(), normalized_choices, n=1, cutoff=cutoff)
    return matches[0] if matches else None
