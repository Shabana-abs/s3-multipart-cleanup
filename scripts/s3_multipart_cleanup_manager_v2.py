#!/usr/bin/env python3
"""
S3 Multipart Upload Cleanup Manager v2.1 — entrypoint.

Adds lifecycle rule to abort incomplete multipart uploads after N days.
Single account (--profile) or all accounts (--profiles-file).

Usage:
  python s3_multipart_cleanup_manager_v2.py --dry-run --exceptions-file config/exceptions.txt
  python s3_multipart_cleanup_manager_v2.py --profiles-file config/profiles.txt --apply

See scripts/s3_multipart_cleanup/ for the package (models, manager, cli).
"""
import logging
import os
import sys
from datetime import datetime

# Ensure script directory is on path so "s3_multipart_cleanup" package is found
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f's3_multipart_cleanup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'),
    ],
)

from s3_multipart_cleanup import main

if __name__ == '__main__':
    main()
