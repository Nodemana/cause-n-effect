"""Per-run token budget. A call that would pass the limit is not started."""


class TokenBudget:
    def __init__(self, limit):
        self.limit = int(limit)
        if self.limit < 0:
            raise ValueError("token budget must be zero or positive")
        self.used = 0

    def estimate(self, prompt, max_tokens):
        return _tokens(prompt) + int(max_tokens)

    def can_spend(self, estimate):
        return self.used + int(estimate) <= self.limit

    def charge(self, prompt_tokens, completion_tokens):
        self.used += int(prompt_tokens) + int(completion_tokens)


def _tokens(text):
    return max(1, (len(text or "") + 3) // 4)
