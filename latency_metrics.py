"""Record voice stage durations without including speech or credentials."""
from collections import deque
from dataclasses import dataclass, field
import math

@dataclass
class LatencyWindow:
    capacity: int = 500
    samples: deque = field(default_factory=deque)

    def record(self, milliseconds):
        if not math.isfinite(milliseconds) or milliseconds<0:
            raise ValueError('Latency must be finite and nonnegative')
        self.samples.append(float(milliseconds))
        while len(self.samples)>self.capacity:
            self.samples.popleft()

    def summary(self):
        data=sorted(self.samples)
        if not data:
            return {'count':0,'median_ms':None,'p95_ms':None,'worst_ms':None}
        n=len(data)
        median=(data[(n-1)//2]+data[n//2])/2
        return {'count':n,'median_ms':median,'p95_ms':data[math.ceil(.95*n)-1],'worst_ms':data[-1]}
