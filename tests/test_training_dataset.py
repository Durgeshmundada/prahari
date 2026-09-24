from scripts.export_training_dataset import assign_run_splits


def test_training_split_is_deterministic_and_run_scoped() -> None:
    runs = {f"experiment-{index:03}" for index in range(10)}
    first = assign_run_splits(runs)
    assert first == assign_run_splits(runs)
    assert set(first.values()) == {"train", "validation", "test"}
    assert sum(value == "train" for value in first.values()) == 7
