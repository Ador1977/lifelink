"""
ai_engine.py
------------
AI components for the Blood Donation Platform:
1. Urgency Detection Model - predicts how urgent a blood request is
   (Critical / High / Medium / Low) using a trained ML classifier
   (scikit-learn RandomForest) built on synthetic-but-realistic
   medical triage data.
2. Donor Health Screening Model - checks whether a donor is eligible
   to donate blood based on uploaded health report values, using a
   trained classifier plus safe medical threshold rules.

Models are persisted to disk with joblib after first training, so
subsequent app starts load instantly without retraining.
"""

import os
import logging
import numpy as np
from sklearn.ensemble import RandomForestClassifier, GradientBoostingRegressor
from sklearn.model_selection import cross_val_score
from sklearn.metrics import classification_report, r2_score
import random
import joblib

logger = logging.getLogger(__name__)

random.seed(42)
np.random.seed(42)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "instance", "models")

# ---------------------------------------------------------------------------
# 1. URGENCY DETECTION MODEL
# ---------------------------------------------------------------------------
# Features: [units_needed, patient_condition_score, hours_until_needed,
#            age_risk_factor, hospital_type(0=govt,1=private),
#            requester_history_count]
# Label: 0=Low, 1=Medium, 2=High, 3=Critical

URGENCY_LABELS = {0: "Low", 1: "Medium", 2: "High", 3: "Critical"}
URGENCY_FEATURE_NAMES = [
    "units_needed", "condition_score", "hours_needed",
    "age_risk", "hospital_type", "requester_history"
]
URGENCY_CONFIDENCE_THRESHOLD = 60.0


def _generate_urgency_training_data(n=2000):
    X, y = [], []
    for _ in range(n):
        units = random.randint(1, 10)
        condition = random.randint(1, 5)
        hours_needed = random.choice([1, 2, 4, 6, 12, 24, 48, 72, 120])
        age_risk = random.randint(1, 3)
        hospital_type = random.choice([0, 1])
        history = random.randint(0, 5)

        score = 0
        score += condition * 2.2
        score += units * 0.6
        score += age_risk * 1.3
        score += (48 - min(hours_needed, 48)) / 8.0
        score += hospital_type * 0.5
        score += history * 0.3

        noise = random.gauss(0, 0.8)
        score += noise

        if score >= 18:
            label = 3
        elif score >= 13:
            label = 2
        elif score >= 8:
            label = 1
        else:
            label = 0

        X.append([units, condition, hours_needed, age_risk, hospital_type, history])
        y.append(label)
    return np.array(X), np.array(y)


class UrgencyDetector:
    MODEL_PATH = os.path.join(MODELS_DIR, "urgency_model.joblib")

    def __init__(self):
        self.model = None
        self.cv_accuracy = None
        self.class_report = None
        self._load_or_train()

    def _load_or_train(self):
        if os.path.exists(self.MODEL_PATH):
            try:
                data = joblib.load(self.MODEL_PATH)
                self.model = data["model"]
                self.cv_accuracy = data.get("cv_accuracy")
                self.class_report = data.get("class_report")
                logger.info("Loaded urgency model from disk.")
                return
            except Exception:
                logger.warning("Failed to load urgency model, retraining.")

        X, y = _generate_urgency_training_data()
        self.model = RandomForestClassifier(n_estimators=150, max_depth=10, random_state=42)
        self.model.fit(X, y)

        scores = cross_val_score(self.model, X, y, cv=5, scoring="accuracy")
        self.cv_accuracy = round(float(scores.mean()) * 100, 2)
        self.class_report = classification_report(
            y, self.model.predict(X), target_names=list(URGENCY_LABELS.values()),
            output_dict=True
        )

        os.makedirs(MODELS_DIR, exist_ok=True)
        joblib.dump({
            "model": self.model,
            "cv_accuracy": self.cv_accuracy,
            "class_report": self.class_report,
        }, self.MODEL_PATH)
        logger.info("Trained and saved urgency model.")

    def predict(self, units_needed, condition_score, hours_needed, age_risk,
                hospital_type=0, requester_history=0):
        X = np.array([[units_needed, condition_score, hours_needed,
                        age_risk, hospital_type, requester_history]])
        pred = int(self.model.predict(X)[0])
        proba = self.model.predict_proba(X)[0]
        confidence = round(float(max(proba)) * 100, 1)

        if confidence < URGENCY_CONFIDENCE_THRESHOLD:
            level = "Review"
            level_code = -1
        else:
            level = URGENCY_LABELS[pred]
            level_code = pred

        return {
            "level": level,
            "level_code": level_code,
            "confidence": confidence,
            "features": dict(zip(URGENCY_FEATURE_NAMES, X[0].tolist())),
        }

    def model_info(self):
        return {
            "name": "Urgency Detection (RandomForest)",
            "features": URGENCY_FEATURE_NAMES,
            "n_estimators": self.model.n_estimators,
            "cv_accuracy": self.cv_accuracy,
            "class_report": self.class_report,
            "label_map": URGENCY_LABELS,
        }


# ---------------------------------------------------------------------------
# 2. DONOR HEALTH SCREENING MODEL
# ---------------------------------------------------------------------------
# Features: [hemoglobin, systolic_bp, diastolic_bp, weight_kg,
#            pulse_bpm, age, gender(0=M,1=F)]
# Label: 1 = Eligible, 0 = Not Eligible

HEMOGLOBIN_THRESHOLDS = {"M": 13.0, "F": 12.5}
HEALTH_FEATURE_NAMES = [
    "hemoglobin", "systolic", "diastolic", "weight", "pulse", "age", "gender"
]
HEALTH_CONFIDENCE_THRESHOLD = 60.0


def _generate_health_training_data(n=2000):
    X, y = [], []
    for _ in range(n):
        gender = random.choice([0, 1])
        hemoglobin = round(random.uniform(8.0, 18.0), 1)
        systolic = random.randint(85, 190)
        diastolic = random.randint(50, 110)
        weight = random.randint(38, 100)
        pulse = random.randint(45, 115)
        age = random.randint(15, 70)

        hgb_min = HEMOGLOBIN_THRESHOLDS["F" if gender else "M"]
        eligible = 1
        if hemoglobin < hgb_min:
            eligible = 0
        if not (100 <= systolic <= 180):
            eligible = 0
        if not (60 <= diastolic <= 100):
            eligible = 0
        if weight < 50:
            eligible = 0
        if not (50 <= pulse <= 100):
            eligible = 0
        if not (18 <= age <= 65):
            eligible = 0

        X.append([hemoglobin, systolic, diastolic, weight, pulse, age, gender])
        y.append(eligible)
    return np.array(X), np.array(y)


class HealthScreener:
    MODEL_PATH = os.path.join(MODELS_DIR, "health_model.joblib")

    def __init__(self):
        self.model = None
        self.cv_accuracy = None
        self.class_report = None
        self._load_or_train()

    def _load_or_train(self):
        if os.path.exists(self.MODEL_PATH):
            try:
                data = joblib.load(self.MODEL_PATH)
                self.model = data["model"]
                self.cv_accuracy = data.get("cv_accuracy")
                self.class_report = data.get("class_report")
                logger.info("Loaded health screening model from disk.")
                return
            except Exception:
                logger.warning("Failed to load health model, retraining.")

        X, y = _generate_health_training_data()
        self.model = RandomForestClassifier(n_estimators=150, max_depth=10, random_state=42)
        self.model.fit(X, y)

        scores = cross_val_score(self.model, X, y, cv=5, scoring="accuracy")
        self.cv_accuracy = round(float(scores.mean()) * 100, 2)
        self.class_report = classification_report(
            y, self.model.predict(X), target_names=["Not Eligible", "Eligible"],
            output_dict=True
        )

        os.makedirs(MODELS_DIR, exist_ok=True)
        joblib.dump({
            "model": self.model,
            "cv_accuracy": self.cv_accuracy,
            "class_report": self.class_report,
        }, self.MODEL_PATH)
        logger.info("Trained and saved health screening model.")

    def screen(self, hemoglobin, systolic, diastolic, weight, pulse, age, gender=0):
        X = np.array([[hemoglobin, systolic, diastolic, weight, pulse, age, gender]])
        pred = int(self.model.predict(X)[0])
        proba = self.model.predict_proba(X)[0]
        confidence = round(float(max(proba)) * 100, 1)

        hgb_min = HEMOGLOBIN_THRESHOLDS["F" if gender else "M"]
        gender_label = "Female" if gender else "Male"

        reasons = []
        if hemoglobin < hgb_min:
            reasons.append(
                f"Low hemoglobin ({hemoglobin} g/dL, minimum for {gender_label} is {hgb_min} g/dL)"
            )
        if not (100 <= systolic <= 180):
            reasons.append(f"Systolic blood pressure out of safe range ({systolic} mmHg, must be 100-180)")
        if not (60 <= diastolic <= 100):
            reasons.append(f"Diastolic blood pressure out of safe range ({diastolic} mmHg, must be 60-100)")
        if weight < 50:
            reasons.append(f"Body weight below minimum ({weight} kg, minimum is 50 kg)")
        if not (50 <= pulse <= 100):
            reasons.append(f"Pulse rate out of normal range ({pulse} bpm, must be 50-100)")
        if not (18 <= age <= 65):
            reasons.append(f"Age outside eligible range ({age} years, must be 18-65)")

        eligible = bool(pred) and len(reasons) == 0

        if confidence < HEALTH_CONFIDENCE_THRESHOLD:
            eligible = False
            reasons.insert(0, "AI confidence is low — manual medical review recommended.")

        return {
            "eligible": eligible,
            "confidence": confidence,
            "reasons": reasons if reasons else ["All health parameters are within safe donation range."],
            "gender_used": gender_label,
            "hgb_threshold": hgb_min,
        }

    def model_info(self):
        return {
            "name": "Donor Health Screening (RandomForest)",
            "features": HEALTH_FEATURE_NAMES,
            "n_estimators": self.model.n_estimators,
            "cv_accuracy": self.cv_accuracy,
            "class_report": self.class_report,
            "label_map": {0: "Not Eligible", 1: "Eligible"},
        }


# ---------------------------------------------------------------------------
# 3. DONOR RECOMMENDATION MODEL
# ---------------------------------------------------------------------------
# Features (all normalized 0-1, higher is better for the request):
#   [compat, distance, availability, eligibility, reliability,
#    experience, recency, contact]
# Label: a 0-1 "donation suitability" score learned from synthetic-but-
# realistic donor-response records. A GradientBoostingRegressor approximates
# the relationship between donor attributes and how suitable that donor is
# for a given blood request. This is the "AI" half of the recommendation
# system; the other half (hard safety filters + urgency-aware proximity
# boost) lives in recommendation_engine.py as deterministic rule-based logic.

RECOMMENDER_FEATURE_NAMES = [
    "compat", "distance", "availability", "eligibility",
    "reliability", "experience", "recency", "contact",
]

# Contribution weights used to synthesize the training labels. Distance is
# intentionally a *modest* part of the model: its real influence on ranking
# is applied by the rule-based urgency boost in recommendation_engine.py,
# so proximity matters more for Critical/High requests than for Low ones.
RECOMMENDER_WEIGHTS = {
    "compat": 0.30,        # blood-group compatibility / exactness
    "reliability": 0.25,   # historical responsiveness of the donor
    "eligibility": 0.10,   # health-screening result
    "distance": 0.10,      # how close the donor is to the patient
    "experience": 0.10,    # number of prior donations
    "availability": 0.05,  # donor availability
    "recency": 0.05,       # how long since the last donation
    "contact": 0.05,       # reachability via a preferred contact method
}

# Bump this whenever the feature set / weight scheme changes, so cached
# model files from older versions are retrained automatically.
RECOMMENDER_WEIGHTS_VERSION = 2


def _clip01(value):
    return max(0.0, min(1.0, float(value)))


def _generate_recommender_training_data(n=3000):
    X, y = [], []
    for _ in range(n):
        compat = random.choice([0.85, 1.0])
        distance = round(random.uniform(0.0, 1.0), 3)
        availability = 1.0
        eligibility = random.choice([0.6, 1.0])
        reliability = round(random.uniform(0.2, 1.0), 3)
        experience = round(random.uniform(0.0, 1.0), 3)
        recency = round(random.uniform(0.0, 1.0), 3)
        contact = random.choice([0.5, 1.0])

        score = 0.0
        for name, weight in RECOMMENDER_WEIGHTS.items():
            score += weight * locals()[name]
        label = _clip01(score + random.gauss(0, 0.04))

        X.append([compat, distance, availability, eligibility,
                  reliability, experience, recency, contact])
        y.append(label)
    return np.array(X), np.array(y)


class DonorRecommender:
    MODEL_PATH = os.path.join(MODELS_DIR, "recommender_model.joblib")

    def __init__(self):
        self.model = None
        self.r2 = None
        self.feature_importances = None
        self._load_or_train()

    def _load_or_train(self):
        if os.path.exists(self.MODEL_PATH):
            try:
                data = joblib.load(self.MODEL_PATH)
                if data.get("weights_version") != RECOMMENDER_WEIGHTS_VERSION:
                    raise ValueError("stale recommender weights")
                self.model = data["model"]
                self.r2 = data.get("r2")
                self.feature_importances = data.get("feature_importances")
                logger.info("Loaded donor recommender model from disk.")
                return
            except Exception:
                logger.warning("Failed to load recommender model, retraining.")

        X, y = _generate_recommender_training_data()
        split = int(len(X) * 0.8)
        self.model = GradientBoostingRegressor(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.9, random_state=42,
        )
        self.model.fit(X[:split], y[:split])

        pred = self.model.predict(X[split:])
        self.r2 = round(float(r2_score(y[split:], pred)), 4)
        self.feature_importances = dict(zip(
            RECOMMENDER_FEATURE_NAMES,
            [round(float(v), 4) for v in self.model.feature_importances_],
        ))

        os.makedirs(MODELS_DIR, exist_ok=True)
        joblib.dump({
            "model": self.model,
            "r2": self.r2,
            "feature_importances": self.feature_importances,
            "weights_version": RECOMMENDER_WEIGHTS_VERSION,
        }, self.MODEL_PATH)
        logger.info("Trained and saved donor recommender model.")

    def predict(self, features):
        """features: dict with every RECOMMENDER_FEATURE_NAMES key as a 0-1
        float. Returns the predicted donation suitability in 0-1."""
        row = [float(features.get(name, 0.0)) for name in RECOMMENDER_FEATURE_NAMES]
        pred = float(self.model.predict(np.array([row]))[0])
        return _clip01(pred)

    def model_info(self):
        return {
            "name": "Donor Recommendation (GradientBoostingRegressor)",
            "features": RECOMMENDER_FEATURE_NAMES,
            "n_estimators": self.model.n_estimators,
            "r2": self.r2,
            "feature_importances": self.feature_importances,
            "weights": RECOMMENDER_WEIGHTS,
            "label": "Donation suitability score (0-1, higher = better match)",
        }


# Singleton instances (loaded from disk or trained once)
urgency_detector = UrgencyDetector()
health_screener = HealthScreener()
donor_recommender = DonorRecommender()
