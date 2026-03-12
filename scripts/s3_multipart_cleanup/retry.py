"""
Exponential backoff retry for AWS throttling (SlowDown, Throttling, etc.).
"""
import logging
import random
import time
from functools import wraps
from typing import Callable

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
) -> Callable:
    """Decorate a function to retry on AWS throttling errors with exponential backoff."""

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except ClientError as e:
                    code = e.response.get('Error', {}).get('Code', '')
                    if code in [
                        'SlowDown', 'Throttling', 'RequestLimitExceeded',
                        'ProvisionedThroughputExceededException',
                    ]:
                        last_exception = e
                        if attempt < max_retries:
                            delay = min(base_delay * (exponential_base ** attempt), max_delay)
                            delay *= 0.5 + random.random() * 0.5
                            logger.warning(
                                "Throttled, retrying in %.1fs (attempt %d/%d)",
                                delay, attempt + 1, max_retries,
                            )
                            time.sleep(delay)
                            continue
                    raise
                except Exception:
                    raise
            if last_exception:
                raise last_exception
        return wrapper
    return decorator
