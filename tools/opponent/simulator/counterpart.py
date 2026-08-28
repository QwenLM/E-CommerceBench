import math
import random
import numpy as np
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class CounterpartAction:
    decision: str  # "Offer", "Accept", "Reject"
    price: Optional[float] = None
    strategic_cue: Optional[str] = None
    sentiment_cue: Optional[str] = None


class CounterpartKernel:
    """
    Implements the environment-simulated counterpart policy as specified in
    arXiv:2605.13909v1, Section 3.2 and Appendix B.
    """

    PRESETS = {
        "Type-instrumental": {
            "rho": (0.0, -0.25, -0.75),
            "xi": (0.40, 0.0, -0.50),
            "lambda_2": (0.30, 0.50, 1.00),
        },
        "High-reactivity": {
            "rho": (0.0, -0.75, -1.50),
            "xi": (0.40, 0.0, -0.75),
            "lambda_2": (0.45, 0.90, 1.80),
        },
        "Moderate-stochastic": {
            "rho": (0.0, -0.50, -1.10),
            "xi": (0.35, 0.0, -0.60),
            "lambda_2": (0.35, 0.70, 1.40),
        },
        "Hardball": {
            "rho": (-0.25, -1.25, -2.25),
            "xi": (0.0, -0.50, -1.20),
            "lambda_2": (0.60, 1.40, 2.60),
        },
    }

    FAMILY_TO_PRESET = {
        "Candid": "Type-instrumental",
        "Taciturn": "Type-instrumental",
        "Expressive": "High-reactivity",
        "Strategic": "High-reactivity",
        "Stochastic": "Moderate-stochastic",
        "Adversarial": "Hardball",
    }

    def __init__(
        self,
        family: str,
        role: str,  # "buyer" or "seller"
        r_b: float,
        kappa_b: float,
        eta_b: str,
        d_0: float,  # Episode-level opening harshness
        seed: Optional[int] = None,
        K: int = 10,
        p_min: float = 0.0,
        p_max: float = 100.0,
        alpha: float = 6.0,
        beta: float = 1.0,
        gamma: float = 2.0,
        phi_0: float = -4.5,
        phi_delta: float = 30.0,
        phi_t: float = 1.5,
        k_walk: Optional[int] = None,
        lambda_0: float = 0.12,
        lambda_1: float = 0.28,
        lambda_3: float = 0.10,
        lambda_4: float = 0.10,
        tau_rigid: float = 0.10,
        tau_conc: float = 0.10,
        tau_dead: float = 0.80,
        alpha_p: float = 2.0,
        alpha_c: float = 2.0,
        beta_c: float = 1.0,
        b_c: float = 1.0,
        b_h: float = 0.5,
        b_p: float = 1.0,
        mu_s: float = 1.0,
        tau_s: float = 0.5,
        sigma_s: float = 0.75,
    ):
        self.family = family
        self.role = role
        self.r_b = r_b
        self.kappa_b = kappa_b
        self.eta_b = eta_b
        self.d_0 = d_0
        self.K = K
        self.p_min = p_min
        self.p_max = p_max
        self.R = max(1.0, p_max - p_min)

        # Hyperparameters
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self.phi_0 = phi_0
        self.phi_delta = phi_delta
        self.phi_t = phi_t
        self.k_walk = k_walk if k_walk is not None else math.ceil(K / 2)
        self.lambda_0 = lambda_0
        self.lambda_1 = lambda_1
        self.lambda_3 = lambda_3
        self.lambda_4 = lambda_4
        self.tau_rigid = tau_rigid
        self.tau_conc = tau_conc
        self.tau_dead = tau_dead
        self.alpha_p = alpha_p
        self.alpha_c = alpha_c
        self.beta_c = beta_c
        self.b_c = b_c
        self.b_h = b_h
        self.b_p = b_p
        self.mu_s = mu_s
        self.tau_s = tau_s
        self.sigma_s = sigma_s

        # Local RNGs for deterministic per-episode behavior
        self._rng = random.Random(seed) if seed is not None else random
        self._np_rng = np.random.default_rng(seed) if seed is not None else None

        # Family-specific presets
        preset_name = self.FAMILY_TO_PRESET.get(family, "Type-instrumental")
        preset = self.PRESETS[preset_name]

        stance_idx = {"conciliatory": 0, "neutral": 1, "aggressive": 2}[eta_b]
        self.rho_f = preset["rho"][stance_idx]
        self.xi_f = preset["xi"][stance_idx]
        self.lambda_2_f = preset["lambda_2"][stance_idx]

        # State
        self.history_a: List[float] = []
        self.history_b: List[float] = []
        self.last_p_b = None

    def _sigmoid(self, x: float) -> float:
        return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))

    def _get_role_normalized_favorability(self, p: float) -> float:
        return (
            (p - self.r_b) / self.R
            if self.role == "seller"
            else (self.r_b - p) / self.R
        )

    def get_action(self, k: int, p_a: Optional[float]) -> CounterpartAction:
        if p_a is not None:
            self.history_a.append(p_a)

        # 1. Response logic (Accept/Reject/Offer)
        if p_a is not None:
            decision = self._decide_response(k, p_a)

            if decision == "Accept":
                cue_sent = self._generate_sentiment_cue()
                return CounterpartAction(
                    decision="Accept", strategic_cue="Concede", sentiment_cue=cue_sent
                )

            if decision == "Reject":
                cue_sent = self._generate_sentiment_cue()
                return CounterpartAction(
                    decision="Reject", strategic_cue="Pressure", sentiment_cue=cue_sent
                )

            # decision == "Offer": acceptance model declined.
            # Generate counter-offer, but ensure rationality:
            # seller never counters below agent's bid (no information leak).
            # Counter slightly above agent's bid to signal "I want more".
            p_b = (
                self._generate_opening_offer()
                if self.last_p_b is None
                else self._generate_concession_offer(k)
            )
            if self.role == "seller" and p_a is not None and p_b < p_a:
                margin = 0.03 * self.R
                p_b = p_a + margin
                if self.last_p_b is not None:
                    p_b = min(p_b, self.last_p_b)
            elif self.role == "buyer" and p_a is not None and p_b > p_a:
                margin = 0.03 * self.R
                p_b = p_a - margin
                if self.last_p_b is not None:
                    p_b = max(p_b, self.last_p_b)

            self.history_b.append(p_b)
            strat_cue = self._generate_strategic_cue(k, p_b)
            sent_cue = self._generate_sentiment_cue()
            self.last_p_b = p_b
            return CounterpartAction(
                decision="Offer",
                price=p_b,
                strategic_cue=strat_cue,
                sentiment_cue=sent_cue,
            )

        # 2. No agent offer (kernel opens) — generate opening offer
        p_b = (
            self._generate_opening_offer()
            if self.last_p_b is None
            else self._generate_concession_offer(k)
        )
        self.history_b.append(p_b)
        strat_cue = self._generate_strategic_cue(k, p_b)
        sent_cue = self._generate_sentiment_cue()
        self.last_p_b = p_b
        return CounterpartAction(
            decision="Offer", price=p_b, strategic_cue=strat_cue, sentiment_cue=sent_cue
        )

    def _decide_response(self, k: int, p_a: float) -> str:
        delta_bar = self._get_role_normalized_favorability(p_a)

        # Acceptance Model
        if delta_bar >= 0:
            tilde_bar_D_k = 1 - math.sqrt(k / self.K)
            g_theta = (
                self.alpha * delta_bar
                + self.beta * self.kappa_b
                - self.gamma * tilde_bar_D_k
                + self.rho_f * self._calc_history_feature("speed")
                + self.xi_f * self._calc_history_feature("rigidity")
            )
            if self._rng.random() < self._sigmoid(g_theta):
                return "Accept"

        # Walk-away Model
        if k >= self.k_walk and delta_bar < 0:
            tau_w_k = (
                (k - self.k_walk) / (self.K - self.k_walk)
                if self.K > self.k_walk
                else 1.0
            )
            score_w = self.phi_0 + self.phi_delta * (-delta_bar) + self.phi_t * tau_w_k
            if self._rng.random() < self._sigmoid(score_w):
                return "Reject"

        return "Offer"

    def _generate_concession_offer(self, k: int) -> float:
        mag = self._calc_history_feature("magnitude")
        tilde_lambda_b = (
            self.lambda_0
            + self.lambda_1 * self.kappa_b
            - self.lambda_2_f * mag
            - (self.lambda_3 if self.eta_b == "aggressive" else 0)
            + (self.lambda_4 if self.eta_b == "conciliatory" else 0)
        )
        lambda_b = min(1.0, max(0.0, tilde_lambda_b))

        sigma_p = {
            "Type-instrumental": 0.01,
            "Moderate-stochastic": 0.08,
            "High-reactivity": 0.03,
            "Hardball": 0.01,
        }.get(self.FAMILY_TO_PRESET.get(self.family, ""), 0.01)
        epsilon = (
            self._np_rng.normal(0, sigma_p * self.R)
            if self._np_rng
            else np.random.normal(0, sigma_p * self.R)
        )

        p_candidate = self.last_p_b - lambda_b * (self.last_p_b - self.r_b) + epsilon
        return (
            min(self.last_p_b, max(self.r_b, p_candidate))
            if self.role == "seller"
            else max(self.last_p_b, min(self.r_b, p_candidate))
        )

    def _generate_opening_offer(self) -> float:
        omega_k, omega_eta, omega_eta_p = 0.3, 0.15, 0.15
        phi = np.clip(
            1
            - omega_k * self.kappa_b
            + (omega_eta if self.eta_b == "aggressive" else 0)
            - (omega_eta_p if self.eta_b == "conciliatory" else 0),
            0.5,
            1.5,
        )

        slack = (
            (self.p_max - self.r_b)
            if self.role == "seller"
            else (self.r_b - self.p_min)
        )
        p_0 = self.r_b + (1 if self.role == "seller" else -1) * self.d_0 * phi * slack
        p_0 += (
            self._np_rng.normal(0, 0.02 * self.R)
            if self._np_rng
            else np.random.normal(0, 0.02 * self.R)
        )
        if self.role == "seller":
            return min(self.p_max, max(self.r_b, p_0))
        else:
            return max(self.p_min, min(self.r_b, p_0))

    def _calc_history_feature(self, feature: str) -> float:
        # 3-round window J_k = {j : max(2, k-3) <= j <= k-1}
        if len(self.history_a) < 2:
            return 0.0
        s_A = 1 if self.role == "seller" else -1

        deltas = []
        for i in range(max(1, len(self.history_a) - 3), len(self.history_a)):
            deltas.append(s_A * (self.history_a[i] - self.history_a[i - 1]))

        if feature == "magnitude":
            return np.mean([max(0, d / self.R) for d in deltas])
        if feature == "speed":
            return np.mean([d / self.R for d in deltas])
        if feature == "rigidity":
            return 1.0 if max(0, deltas[-1] / self.R) < self.tau_rigid else 0.0
        return 0.0

    def _generate_strategic_cue(self, k: int, p_new: float) -> str:
        if self.family in ["Taciturn", "Strategic"]:
            return "Hold"
        if self.family == "Adversarial":
            return "Pressure"

        # Softmax over logits (Eq. 15)
        c_mag = (
            min(
                1.0, abs(p_new - self.last_p_b) / (abs(self.last_p_b - self.r_b) + 1e-5)
            )
            if self.last_p_b is not None
            else 0.0
        )
        tilde_D_k = math.sqrt(k / self.K)
        b = {
            "conciliatory": [self.b_c, 0, -self.b_c],
            "neutral": [0, self.b_h, 0],
            "aggressive": [-self.b_p, 0, self.b_p],
        }[self.eta_b]

        logits = [
            b[0] + self.alpha_c * (c_mag - self.tau_conc),
            b[1],
            b[2] + self.alpha_p * (tilde_D_k - self.tau_dead) - self.beta_c * c_mag,
        ]

        if self.family == "Stochastic":
            logits = [l / 2.5 for l in logits]  # T_stoch = 2.5

        exp_l = [math.exp(l - max(logits)) for l in logits]
        probs = [e / sum(exp_l) for e in exp_l]
        return self._rng.choices(["Concede", "Hold", "Pressure"], weights=probs, k=1)[0]

    def _generate_sentiment_cue(self) -> str:
        if self.family in ["Taciturn", "Strategic"]:
            return "neutral"
        if self.family == "Adversarial":
            return "negative"

        mu = {"conciliatory": self.mu_s, "neutral": 0.0, "aggressive": -self.mu_s}[
            self.eta_b
        ]
        sigma = 2.0 if self.family == "Stochastic" else self.sigma_s
        z = (
            self._np_rng.normal(mu, sigma)
            if self._np_rng
            else np.random.normal(mu, sigma)
        )
        if z > self.tau_s:
            return "positive"
        if z < -self.tau_s:
            return "negative"
        return "neutral"
