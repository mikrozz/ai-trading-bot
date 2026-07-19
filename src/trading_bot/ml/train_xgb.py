"""XGBoost baseline: direction classification + walk-forward validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score
from xgboost import XGBClassifier

from trading_bot.features.engineering import (
    FEATURE_COLUMNS,
    attach_orderbook_features,
    build_feature_frame,
)
from trading_bot.logging_setup import get_logger

log = get_logger(__name__)

TARGET_COL = "target_direction_5"
# фичи без явной утечки будущего (targets исключены)
MODEL_FEATURES = [c for c in FEATURE_COLUMNS if not c.startswith("target_")]


@dataclass
class FoldMetrics:
    fold: int
    train_rows: int
    test_rows: int
    accuracy: float
    f1: float
    long_ratio: float


@dataclass
class WalkForwardResult:
    folds: list[FoldMetrics] = field(default_factory=list)
    feature_names: list[str] = field(default_factory=list)
    model_path: str | None = None

    @property
    def mean_accuracy(self) -> float:
        if not self.folds:
            return 0.0
        return float(np.mean([f.accuracy for f in self.folds]))

    @property
    def mean_f1(self) -> float:
        if not self.folds:
            return 0.0
        return float(np.mean([f.f1 for f in self.folds]))


def prepare_xy(
    klines: pd.DataFrame,
    books: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    enriched = attach_orderbook_features(klines, books)
    feats = build_feature_frame(enriched)
    cols = [c for c in MODEL_FEATURES if c in feats.columns]
    data = feats.dropna(subset=cols + [TARGET_COL]).copy()
    # последняя точка с NaN target уже отброшена dropna
    x = data[cols]
    y = data[TARGET_COL].astype(int)
    return x, y, cols


def train_walk_forward(
    klines: pd.DataFrame,
    *,
    books: pd.DataFrame | None = None,
    n_folds: int = 5,
    min_train_rows: int = 500,
    model_out: Path | None = None,
    random_state: int = 42,
) -> tuple[XGBClassifier, WalkForwardResult]:
    x, y, cols = prepare_xy(klines, books=books)
    if len(x) < min_train_rows:
        raise ValueError(f"Недостаточно строк после фич: {len(x)} < {min_train_rows}")

    n = len(x)
    # expanding window: fold i test = chunk i
    fold_size = n // (n_folds + 1)
    if fold_size < 50:
        raise ValueError(f"Слишком маленький fold_size={fold_size}, нужно больше истории")

    result = WalkForwardResult(feature_names=cols)
    last_model: XGBClassifier | None = None

    for fold in range(n_folds):
        train_end = fold_size * (fold + 1)
        test_end = fold_size * (fold + 2)
        if fold == n_folds - 1:
            test_end = n
        x_train, y_train = x.iloc[:train_end], y.iloc[:train_end]
        x_test, y_test = x.iloc[train_end:test_end], y.iloc[train_end:test_end]
        if len(x_test) == 0 or len(x_train) < min_train_rows // 2:
            continue

        model = XGBClassifier(
            n_estimators=120,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=random_state,
            n_jobs=2,
        )
        model.fit(x_train, y_train)
        pred = model.predict(x_test)
        metrics = FoldMetrics(
            fold=fold,
            train_rows=len(x_train),
            test_rows=len(x_test),
            accuracy=float(accuracy_score(y_test, pred)),
            f1=float(f1_score(y_test, pred, zero_division=0.0)),
            long_ratio=float(np.mean(pred)),
        )
        result.folds.append(metrics)
        last_model = model
        log.info(
            "wf_fold",
            fold=fold,
            accuracy=metrics.accuracy,
            f1=metrics.f1,
            train_rows=metrics.train_rows,
            test_rows=metrics.test_rows,
        )

    if last_model is None:
        raise RuntimeError("Walk-forward не построил ни одной модели")

    # финальная модель на всём доступном ряду (для paper)
    final = XGBClassifier(
        n_estimators=120,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=random_state,
        n_jobs=2,
    )
    final.fit(x, y)

    if model_out is not None:
        model_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": final,
            "features": cols,
            "target": TARGET_COL,
            "wf_mean_accuracy": result.mean_accuracy,
            "wf_mean_f1": result.mean_f1,
        }
        joblib.dump(payload, model_out)
        result.model_path = str(model_out)
        log.info("model_saved", path=str(model_out))

    return final, result


def load_model(path: Path) -> dict:
    return joblib.load(path)
