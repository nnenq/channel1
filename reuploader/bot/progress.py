"""Прогресс длинных задач для панели: пишем в базу не чаще раза в секунду (и всегда — при смене этапа)."""
import time


class Reporter:
    def __init__(self, save, lo=0.0, hi=1.0, min_dt=1.0, clock=time.monotonic):
        self.save = save            # save(stage, доля 0..1)
        self.lo, self.hi = lo, hi
        self.min_dt, self.clock = min_dt, clock
        self.last_t, self.last_stage, self.last_frac = 0.0, None, -1.0

    def __call__(self, stage, frac):
        frac = self.lo + (self.hi - self.lo) * max(0.0, min(1.0, float(frac)))
        frac = max(frac, self.last_frac)             # проценты не идут назад
        now = self.clock()
        if stage != self.last_stage or now - self.last_t >= self.min_dt or frac >= self.hi:
            self.last_t, self.last_stage, self.last_frac = now, stage, frac
            self.save(stage, round(frac, 3))

    def sub(self, lo, hi):
        """Под-этап: доля 0..1 внутри этапа -> [lo, hi] общего прогресса."""
        return lambda stage, f: self(stage, (lo + (hi - lo) * f - self.lo) / ((self.hi - self.lo) or 1))
