"""Token counting and cost computation."""

from __future__ import annotations

from typing import Optional

from ..config import Config
from ..models import TokenUsage


class TokenCounter:
    """Computes token costs based on model pricing."""

    def __init__(self, config: Config) -> None:
        """Initialize token counter.

        Args:
            config: Application configuration with model pricing.
        """
        self._pricing = config.model_pricing

    def compute_cost(self, model: str, usage: TokenUsage) -> float:
        """Compute cost in USD for token usage.

        Args:
            model: Model name (normalized).
            usage: TokenUsage data.

        Returns:
            Cost in USD.
        """
        pricing = self._pricing.get(model)
        if not pricing:
            # Default pricing for unknown models
            pricing = {"input": 1.0, "output": 3.0}

        input_cost = (usage.input_tokens / 1_000_000) * pricing["input"]
        output_cost = (usage.output_tokens / 1_000_000) * pricing["output"]

        # Cache reads are typically cheaper (10% of input)
        cache_cost = (usage.cached_input_tokens / 1_000_000) * pricing["input"] * 0.1

        # Cache writes are typically same as input
        cache_write_cost = (usage.reasoning_output_tokens / 1_000_000) * pricing["input"]

        return input_cost + output_cost + cache_cost + cache_write_cost

    def get_pricing(self, model: str) -> Optional[dict]:
        """Get pricing for a model.

        Args:
            model: Model name.

        Returns:
            Pricing dict or None.
        """
        return self._pricing.get(model)

    def format_cost(self, cost: float) -> str:
        """Format cost for display.

        Args:
            cost: Cost in USD.

        Returns:
            Formatted string.
        """
        if cost < 0.01:
            return f"${cost:.4f}"
        elif cost < 1.0:
            return f"${cost:.3f}"
        else:
            return f"${cost:.2f}"

    def format_tokens(self, count: int) -> str:
        """Format token count for display.

        Args:
            count: Token count.

        Returns:
            Formatted string (e.g., "1.2K", "3.4M").
        """
        if count < 1000:
            return str(count)
        elif count < 1_000_000:
            return f"{count / 1000:.1f}K"
        else:
            return f"{count / 1_000_000:.2f}M"
