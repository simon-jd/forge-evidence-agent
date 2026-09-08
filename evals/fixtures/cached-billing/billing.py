from decimal import Decimal

def cost(input_tokens, cached_tokens, output_tokens, input_rate, cache_rate, output_rate):
    million = Decimal("1000000")
    return (Decimal(input_tokens) * input_rate + Decimal(cached_tokens) * cache_rate
            + Decimal(output_tokens) * output_rate) / million
