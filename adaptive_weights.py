"""
CASI — Adaptive Weighting Engine
Ported directly from casi_adaptive_weights.py.
Bayesian recalibration of component weights from Delphi priors
based on observed failure patterns.
"""

import numpy as np

DELPHI_WEIGHTS = np.array([0.22, 0.18, 0.17, 0.16, 0.14, 0.13])
COMPONENT_NAMES = ['A', 'B', 'C', 'D', 'E', 'F']


class AdaptiveWeightEngine:
    def __init__(self, damping=0.6, bounds=(0.05, 0.40), warm_up=5):
        self.damping = damping
        self.bounds = bounds
        self.warm_up = warm_up
        self.weights = DELPHI_WEIGHTS.copy() / DELPHI_WEIGHTS.sum()
        self.release_count = 0
        self.history = []

    def update(self, component_values, n_failures):
        self.release_count += 1
        v = np.array(component_values, dtype=float)
        degradation = 100 - v

        if n_failures > 0 and degradation.sum() > 0:
            raw_signal = degradation * n_failures
            signal_weights = raw_signal / raw_signal.sum()
        else:
            signal_weights = self.weights

        delphi_norm = DELPHI_WEIGHTS / DELPHI_WEIGHTS.sum()

        if self.release_count <= self.warm_up:
            # Warm-up: keep pure Delphi weights so CASI == ASI for the first
            # `warm_up` sprints.  Adaptation has not yet accumulated enough
            # signal to be meaningful, so returning Delphi is both correct and
            # avoids confusing users with unexplained score divergence.
            new_w = delphi_norm.copy()
        else:
            # Post warm-up: blend previous weights with the observed signal
            new_w = self.damping * self.weights + (1 - self.damping) * signal_weights
            new_w = np.clip(new_w, self.bounds[0], self.bounds[1])
            new_w = new_w / new_w.sum()

        self.history.append({
            'release': self.release_count,
            'weights': new_w.copy(),
            'n_failures': n_failures,
        })
        self.weights = new_w
        return self.weights

    def weight_shift(self):
        delphi = DELPHI_WEIGHTS / DELPHI_WEIGHTS.sum()
        return dict(zip(COMPONENT_NAMES, self.weights - delphi))

    def summary(self):
        delphi = DELPHI_WEIGHTS / DELPHI_WEIGHTS.sum()
        print('Component   Delphi    Adapted   Shift')
        for name, dw, aw in zip(COMPONENT_NAMES, delphi, self.weights):
            arrow = '▲' if aw - dw > 0.02 else ('▼' if aw - dw < -0.02 else '≈')
            print(f'  {name:<12} {dw:.3f}   {aw:.3f}   {arrow} {aw - dw:+.3f}')
