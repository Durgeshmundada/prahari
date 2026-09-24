from scripts.check_fault_detection import _prediction_is_active


def test_normal_prediction_is_not_an_active_fault() -> None:
    assert _prediction_is_active({"likely_incident_type": "normal"}) is False


def test_degradation_prediction_is_an_active_fault() -> None:
    assert _prediction_is_active({"likely_incident_type": "tunnel_degradation"}) is True
