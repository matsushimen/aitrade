"""Validated, Decimal-only research trading conditions."""
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class TradingConfig:
    pair: str = 'btc_jpy'
    timeframe: str = '5min'
    starting_cash_jpy: Decimal = Decimal('10000')
    max_position_jpy: Decimal = Decimal('5000')
    trade_risk_jpy: Decimal = Decimal('100')
    daily_loss_jpy: Decimal = Decimal('200')
    stop_fraction: Decimal = Decimal('0.02')
    fee_bps: Decimal = Decimal('15')
    spread_bps: Decimal = Decimal('0')
    slippage_bps: Decimal = Decimal('5')
    fast_window: int = 12
    slow_window: int = 36
    max_feed_age_seconds: int = 90

    def __post_init__(self):
        if self.pair != 'btc_jpy' or self.timeframe != '5min':
            raise ValueError('Only BTC/JPY spot, long-only, 5min is supported.')
        for key in ('starting_cash_jpy', 'max_position_jpy', 'trade_risk_jpy', 'daily_loss_jpy', 'stop_fraction', 'fee_bps', 'spread_bps', 'slippage_bps'):
            value = getattr(self, key)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f'{key} must be a finite nonnegative decimal string.')
        if min(self.starting_cash_jpy, self.max_position_jpy, self.trade_risk_jpy, self.daily_loss_jpy) <= 0:
            raise ValueError('Capital and risk limits must be positive.')
        if self.max_position_jpy > self.starting_cash_jpy:
            raise ValueError('Position cap must not exceed starting capital.')
        if not 0 < self.stop_fraction < 1 or max(self.fee_bps, self.slippage_bps, self.spread_bps) >= 10000:
            raise ValueError('Invalid stop or cost.')
        if any(type(v) is not int for v in (self.fast_window, self.slow_window, self.max_feed_age_seconds)) or not 1 <= self.fast_window < self.slow_window or self.max_feed_age_seconds <= 0:
            raise ValueError('Invalid window or feed-age setting.')

    def to_dict(self):
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}

    @classmethod
    def load(cls, path: Path | None):
        if path is None:
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding='utf-8')))

    @classmethod
    def from_dict(cls, values):
        values = dict(values) if isinstance(values, dict) else values
        defaults = asdict(cls())
        if not isinstance(values, dict) or values.keys() - defaults.keys():
            raise ValueError('Unknown trading configuration fields.')
        for key, value in values.items():
            if isinstance(defaults[key], Decimal):
                if not isinstance(value, str):
                    raise ValueError(f'{key} must be a decimal string.')
                try:
                    values[key] = Decimal(value)
                except InvalidOperation as exc:
                    raise ValueError(f'Invalid decimal: {key}') from exc
        return cls(**values)
