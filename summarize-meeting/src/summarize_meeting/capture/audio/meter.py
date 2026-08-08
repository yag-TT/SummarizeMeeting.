from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike


def normalized_rms(samples: ArrayLike, floor_db: float = -60.0) -> float:
    values = np.asarray(samples, dtype=np.float32)
    if values.size == 0:
        return 0.0
    finite = np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=-1.0)
    rms = float(np.sqrt(np.mean(np.square(finite, dtype=np.float64))))
    dbfs = 20.0 * math.log10(max(rms, 1e-12))
    return max(0.0, min(1.0, (dbfs - floor_db) / -floor_db))
