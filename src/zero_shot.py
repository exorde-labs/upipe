from exorde_data import Classification, Translation

class TooBigError(Exception):
    pass

from opentelemetry import trace
from opentelemetry.trace.status import Status, StatusCode

def zero_shot(
    item: Translation, lab_configuration, max_depth=None, depth=0
) -> Classification:
    """
    Zero_shot classification has been vaulted out.
    """
    return Classification(label="", score=float(0))
