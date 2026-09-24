from scripts.train_degradation_model import metrics


def test_binary_metrics_are_computed_without_division_errors() -> None:
    result = metrics([0, 0, 1, 1], [0.1, 0.4, 0.6, 0.9], 0.5)
    assert result == {
        "accuracy": 1.0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "false_positive_rate": 0.0,
    }
