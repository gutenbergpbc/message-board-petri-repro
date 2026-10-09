"""Deterministic tool-result interventions; never estimate or recalibrate budgets."""

from inspect_ai.model import ModelUsage


def consumed_tokens(usage: ModelUsage | None, *, required: bool = False) -> int:
    """True input (including cache) plus output; reasoning is already in output."""
    if usage is None:
        if required:
            raise ValueError("Token-budget condition requires provider-reported target usage")
        return 0
    total = (
        usage.input_tokens
        + (usage.input_tokens_cache_read or 0)
        + (usage.input_tokens_cache_write or 0)
        + usage.output_tokens
    )
    if total < 0 or (usage.total_tokens and usage.total_tokens != total):
        raise ValueError("Inconsistent provider-reported token usage")
    return total


def budget_notice(result: str, *, allowance: int | None, used: int) -> str:
    if allowance is None:
        return result
    if type(allowance) is not int or allowance < 1 or used < 0:
        raise ValueError("Budget allowance must be a positive integer and usage nonnegative")
    remaining = max(0, allowance - used)
    return f"{result}\n\nToken budget remaining: {remaining} / {allowance} tokens."


def turn_notice(result: str, *, used: int, maximum: int) -> str:
    """Report actual target generation count, not transcript-message count."""
    if maximum < 1 or not 0 <= used <= maximum:
        raise ValueError("Invalid target turn count")
    return (f"{result}\n\nTurn limit warning: Turn {used} / Maximum {maximum} "
            f"({100 * used / maximum:g}% used).")
